import asyncio
import json
import re
import threading
import time
import traceback
from datetime import datetime

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

import survey_plan
import survey_stats
from commands import APP_COMMANDS, HELP_TEXT, LOCAL_COMMANDS, parse_command
from config import (
    COMMAND_TO_APP,
    DIFY_APPS,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    assert_ready,
)
from dify import chat as dify_chat
from feishu import get_user_name, reply_markdown, reply_text
from feishu_docs import create_doc_from_markdown
from feishu_sheets import fetch_raw_rows, parse_feishu_link, read_sheet_values
from tagging import run_tagging

assert_ready()

# user_id -> {bot_msg_id: {"conv_id", "app_id", "sheet_names", "ts"}}
# 每条 pending 表示一个等用户回答的澄清问题。机器人发卡片时记录这条卡片的
# message_id；用户用飞书"回复"功能针对它回答时，事件里 parent_id 会指向这条
# message_id，从而把回答路由到正确的 Dify 会话上——支持同一用户多个分析并发。
user_state: dict[str, dict[str, dict]] = {}
PENDING_TTL = 24 * 3600

# 飞书会重试，按 event_id 去重
processed_events: dict[str, float] = {}
DEDUPE_TTL = 300


def _is_duplicate(event_id: str) -> bool:
    now = time.time()
    for k in list(processed_events.keys()):
        if processed_events[k] + DEDUPE_TTL < now:
            del processed_events[k]
    if event_id in processed_events:
        return True
    processed_events[event_id] = now
    return False


def _get_pending(user_id: str) -> dict[str, dict]:
    pending = user_state.setdefault(user_id, {})
    now = time.time()
    for k in list(pending.keys()):
        if pending[k]["ts"] + PENDING_TTL < now:
            del pending[k]
    return pending


def _add_pending(
    user_id: str,
    bot_msg_id: str,
    conv_id: str,
    app_id: str,
    sheet_names: list[str],
    **extra,
) -> None:
    """登记一条 pending entry。`extra` 透传到 entry，留给调研分析的 stage 状态机用
    （stage / sheet_token / sheet_id / plan / rows_fed 等字段）。"""
    pending = _get_pending(user_id)
    entry = {
        "conv_id": conv_id,
        "app_id": app_id,
        "sheet_names": sheet_names,
        "ts": time.time(),
    }
    entry.update(extra)
    pending[bot_msg_id] = entry


def _resolve_pending(user_id: str, parent_msg_id: str) -> dict | None:
    """按 parent_msg_id 找到对应会话；找到就 pop 出来。"""
    pending = _get_pending(user_id)
    if parent_msg_id and parent_msg_id in pending:
        return pending.pop(parent_msg_id)
    return None


def _take_single_pending(user_id: str) -> dict | None:
    """没用 reply 时的兜底：用户恰好只有一个 pending → 直接用它。"""
    pending = _get_pending(user_id)
    if len(pending) == 1:
        bot_msg_id = next(iter(pending))
        return pending.pop(bot_msg_id)
    return None


async def _handle_message(event: dict) -> None:
    message = event.get("message", {})
    sender = event.get("sender", {})

    if message.get("chat_type") != "p2p":
        return

    message_id = message.get("message_id", "")
    parent_id = message.get("parent_id", "")
    user_id = sender.get("sender_id", {}).get("open_id", "anonymous")
    msg_type = message.get("message_type", "")
    user_name = await get_user_name(user_id)

    if msg_type != "text":
        print(f"[bot] msg | user={user_name} | type={msg_type} (ignored)")
        await reply_text(message_id, "目前仅支持文本消息（链接也是文本）。")
        return

    content = json.loads(message.get("content", "{}"))
    text = content.get("text", "").strip()
    if not text:
        return

    preview = text if len(text) <= 80 else text[:80] + "…"
    print(
        f"[bot] msg | user={user_name} | reply={'Y' if parent_id else 'N'} "
        f"| text={preview!r}"
    )

    parsed = parse_command(text)

    # 已知指令
    if parsed:
        cmd, arg = parsed
        if cmd in LOCAL_COMMANDS:
            await _handle_local_command(cmd, user_id, message_id)
            return
        if cmd in APP_COMMANDS:
            await _handle_app_command(cmd, arg, user_id, message_id)
            return

    # 以 / 开头但不是已知指令
    if text.startswith("/"):
        await reply_text(
            message_id,
            f"未知指令：{text.split()[0]}\n发送 /help 查看用法。",
        )
        return

    # 没指令但是个飞书链接 → 拒绝
    if parse_feishu_link(text):
        await reply_text(
            message_id,
            "请加指令告诉我用这个表格做什么。例如：\n"
            "/调研分析 <表格链接>\n\n"
            "发送 /help 查看所有指令。",
        )
        return

    # 纯文字 → 按 parent_id 路由到对应的 pending 会话
    entry = _resolve_pending(user_id, parent_id)
    if not entry:
        # 没用「回复」功能。如果用户只有一个 pending，直接兜底用上；否则要求 reply。
        entry = _take_single_pending(user_id)

    if not entry:
        pending_count = len(_get_pending(user_id))
        if pending_count == 0:
            await reply_text(
                message_id,
                "还没有进行中的对话。先用指令开始一个任务，例如：\n"
                "/调研分析 <表格链接>\n\n"
                "发送 /help 查看所有指令。",
            )
        else:
            await reply_text(
                message_id,
                f"你有 {pending_count} 个分析在等回答，请用「回复」功能针对具体的"
                "问题消息回答。",
            )
        return

    await reply_text(message_id, "正在思考...")
    asyncio.create_task(
        _continue_conversation(
            entry["app_id"],
            text,
            message_id,
            user_id,
            entry["conv_id"],
            entry["sheet_names"],
            stage=entry.get("stage"),
            sheet_token=entry.get("sheet_token"),
            sheet_id=entry.get("sheet_id"),
            plan=entry.get("plan"),
            rows_fed=entry.get("rows_fed", False),
        )
    )


async def _handle_local_command(cmd: str, user_id: str, message_id: str) -> None:
    if cmd == "/help":
        await reply_text(message_id, HELP_TEXT)
    elif cmd == "/ping":
        await reply_text(message_id, "pong")
    elif cmd == "/reset":
        user_state.pop(user_id, None)
        await reply_text(message_id, "已清除当前对话上下文。")


async def _handle_app_command(
    cmd: str, arg: str, user_id: str, message_id: str
) -> None:
    app_id = COMMAND_TO_APP[cmd]

    if app_id == "analyze":
        link = parse_feishu_link(arg)
        if not link:
            await reply_text(
                message_id,
                "请在指令后跟上飞书表格链接。例如：\n"
                "/调研分析 https://xxx.feishu.cn/sheets/yyy",
            )
            return
        await reply_text(
            message_id,
            "📊 收到表格，正在读取数据！如果有需要和你确认的问题，我会在 3 分钟内"
            "向你提问；如果没有收到我的提问，意味着我已经开始整理报告了哦~ "
            "之后会每 5 分钟向你叭叭一句来表示我还在分析！",
        )
        asyncio.create_task(_run_analyze(link, message_id, user_id))
        return

    if app_id == "tagging":
        link = parse_feishu_link(arg)
        if not link:
            await reply_text(
                message_id,
                "请在指令后跟上飞书表格链接。例如：\n"
                "/反馈打标 https://xxx.feishu.cn/sheets/yyy",
            )
            return
        await reply_text(
            message_id,
            "🏷️ 收到表格链接，正在分析表头识别需打标的列。"
            "处理过程中会插入新列直接写到原表格上，请确保你对该表有编辑权限。",
        )
        asyncio.create_task(run_tagging(link, message_id, user_id))
        return

    # 未来扩展点：其他 app_id 在这里加 elif 分支
    await reply_text(message_id, f"指令 {cmd} 还没接入处理逻辑。")


REPORT_START_MSG = (
    "📝 开始整理报告，每五分钟会进行一次进度同步，"
    "如果 5 分钟后未收到信息，说明我宕机啦！"
)


async def _heartbeat(message_id: str, interval: int = 300) -> None:
    elapsed_min = 0
    try:
        while True:
            await asyncio.sleep(interval)
            elapsed_min += interval // 60
            await reply_text(
                message_id,
                f"在写了在写了，别急别急！已等待 {elapsed_min} 分钟",
            )
    except asyncio.CancelledError:
        pass


async def _with_heartbeat(message_id: str, coro):
    hb = asyncio.create_task(_heartbeat(message_id))
    try:
        return await coro
    finally:
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass


# ============================================================================
# /调研分析 重构后的流程：planner 出 plan → 用户卡片确认 → Python 算统计 →
#                           analyst 写报告 → 用户对报告追问（同一 conv_id）
# ============================================================================


# 卡片 / 文档里允许出现的最大 rows JSON 字符数。多了就抽样。
_QA_ROWS_MAX_CHARS = 60000


async def _run_analyze(link: dict, message_id: str, user_id: str) -> None:
    """第 1 阶段：读表 → 调 planner → 解析 plan → 全表扫 unique 让 LLM 反查同义 → 发确认卡片 → 登 pending。"""
    try:
        token, sheet_id, title, rows = await fetch_raw_rows(link)
        if len(rows) <= 1:
            await reply_text(message_id, "表格里没有数据行，请检查权限或内容。")
            return

        headers = rows[0]
        sample_md = _build_planner_sample(rows)

        planner_key = DIFY_APPS["analyze"]["planner_key"]
        plan_answer, planner_conv_id = await _with_heartbeat(
            message_id,
            dify_chat(sample_md, user_id, conversation_id="", api_key=planner_key),
        )

        plan, err = survey_plan.parse_plan_from_llm(plan_answer, len(headers))
        if not plan:
            print(f"[analyze] plan parse failed: {err}; retrying once")
            retry_query = (
                f"上次输出无法解析: {err}。请严格按 JSON schema 重新输出，"
                "用 ```json ``` 围栏包起来，不要附加解释文字。"
            )
            plan_answer, planner_conv_id = await dify_chat(
                retry_query, user_id, conversation_id=planner_conv_id,
                api_key=planner_key,
            )
            plan, err = survey_plan.parse_plan_from_llm(
                plan_answer, len(headers)
            )

        if not plan:
            await reply_text(
                message_id,
                f"❌ planner 返回的 JSON 解析失败：{err}\n"
                f"请检查 Dify 后台『调研分析-规划器』的 prompt。\n"
                f"LLM 原始输出（截断 500 字）：\n{plan_answer[:500]}",
            )
            return

        # 全表扫 unique → LLM 反查同义合并（C 路线，准确率优先）
        await reply_text(
            message_id,
            "🔍 已识别字段类型，正在扫全表把「同义但不同写法/语言」的选项归并...",
        )
        plan = await _with_heartbeat(
            message_id,
            _enrich_plan_with_aliases(plan, rows, user_id),
        )

        card_md = survey_plan.render_plan_for_user(plan, headers)
        bot_msg_id = await reply_markdown(message_id, card_md)
        if bot_msg_id:
            _add_pending(
                user_id, bot_msg_id, planner_conv_id, "analyze", [title],
                stage="plan_confirm",
                sheet_token=token, sheet_id=sheet_id, plan=plan,
            )
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"处理失败：{e}")


# === C 路线：扫全表 unique → LLM 反查同义合并 → 写回 plan["columns"][i]["value_aliases"] ===

# 单列 unique 值上限：超过这个数说明该列大概率不是选择题（被错分），跳过该列的同义合并。
_ALIAS_UNIQUE_CAP = 200
# 一次 enrich 调用 query 字符数软上限。超过会按 unique 数量降序裁减列。
_ALIAS_QUERY_MAX_CHARS = 30000


async def _enrich_plan_with_aliases(
    plan: dict, rows: list[list], user_id: str
) -> dict:
    """对 plan 里的 single/multi/profile_dim 列扫全表 unique 值，让 LLM 同义合并。

    失败时返回原 plan，不中断主流程（aliases 是锦上添花，没了就按原值统计）。
    """
    if not rows or len(rows) <= 1:
        return plan
    headers = rows[0]
    body = rows[1:]

    # 收集每列的 unique 值
    target: list[tuple[int, str, str, list[str], dict]] = []
    for c in plan["columns"]:
        if c["role"] not in ("single_choice", "multi_choice", "profile_dim"):
            continue
        idx = c["index"]
        nonblank: list[str] = []
        for row in body:
            if idx < len(row):
                v = survey_stats._format_cell(row[idx]).strip()
                if v:
                    nonblank.append(v)
        if not nonblank:
            continue
        if c["role"] == "multi_choice":
            delim = c.get("delimiter") or survey_stats._guess_delimiter(nonblank)
            unique_set: set[str] = set()
            for v in nonblank:
                for opt in v.split(delim):
                    o = opt.strip()
                    if o:
                        unique_set.add(o)
        else:
            unique_set = set(nonblank)
        if len(unique_set) <= 1:
            continue
        if len(unique_set) > _ALIAS_UNIQUE_CAP:
            print(
                f"[enrich] col {idx} has {len(unique_set)} unique values > "
                f"cap {_ALIAS_UNIQUE_CAP}, skip aliasing"
            )
            continue
        name = c.get("name") or (
            headers[idx] if idx < len(headers) else f"col_{idx}"
        )
        current = c.get("value_aliases") or {}
        target.append((idx, name, c["role"], sorted(unique_set), current))

    if not target:
        return plan

    # 按 unique 数量降序，超 query 上限时丢弃最大的
    target.sort(key=lambda t: -len(t[3]))
    estimated = sum(sum(len(v) + 4 for v in t[3]) + len(t[1]) + 200 for t in target)
    while estimated > _ALIAS_QUERY_MAX_CHARS and len(target) > 1:
        dropped = target.pop(0)
        print(
            f"[enrich] query too long, drop col {dropped[0]} "
            f"({len(dropped[3])} unique values)"
        )
        estimated = sum(
            sum(len(v) + 4 for v in t[3]) + len(t[1]) + 200 for t in target
        )

    query = _build_alias_enrichment_query(target)

    planner_key = DIFY_APPS["analyze"]["planner_key"]
    try:
        answer, _ = await dify_chat(
            query, user_id, conversation_id="", api_key=planner_key
        )
    except Exception as e:
        print(f"[enrich] LLM call failed: {e}; skipping alias enrichment")
        return plan

    parsed, err = survey_plan.parse_aliases_json(answer)
    if not parsed:
        print(
            f"[enrich] alias JSON parse failed: {err}; "
            f"answer head: {answer[:300]}"
        )
        return plan

    plan = survey_plan.apply_aliases_to_plan(plan, parsed)
    print(
        f"[enrich] applied aliases for {sum(1 for v in parsed.values() if v)} columns"
    )
    return plan


def _build_alias_enrichment_query(
    target: list[tuple[int, str, str, list[str], dict]]
) -> str:
    """target: [(col_index, col_name, role, sorted_unique_values, current_aliases)]"""
    parts: list[str] = [
        "请对下面每一列的 unique 取值做「语义同义合并」——同一意思但不同写法/语言的值，归为一个 canonical。",
        "",
        "**输出 JSON**（用 ```json 围栏包起来，不要附加解释文字）：",
        "```json",
        '{',
        '  "<col_index>": {',
        '    "<canonical>": ["alias1", "alias2", ...]',
        '  }',
        '}',
        "```",
        "",
        "**规则**：",
        "1. canonical 选最直观的一个（中文优先，没有中文则保留原文）",
        "2. 每个 alias 必须是该列实际出现过的字符串（不能造新值，不能改写法）",
        "3. canonical 自身可以出现在 aliases 里也可以不出现，下游会兼容",
        "4. 没有同义可合并的列，对应 col_index 的值写 `{}`（不要省略）",
        "5. 大小写差异、首尾空白差异也算同义（例如 `Mythic` 和 `mythic` 是同义）",
        "6. 如果列已有「已知映射」，请在此基础上保留 + 补充：保留已知映射（除非明显错误），把列里其他同义但未被映射的值补充进去",
        "",
        "**各列的 unique 取值如下**：",
        "",
    ]
    for col_idx, name, role, values, current in target:
        parts.append(f"### 列 {col_idx}: {name}（role={role}, {len(values)} 个 unique 值）")
        if current:
            parts.append("已知映射:")
            for canon, aliases in current.items():
                parts.append(f"- 「{canon}」 ← {aliases}")
        parts.append("全部 unique 取值:")
        for v in values:
            parts.append(f"- {v}")
        parts.append("")
    return "\n".join(parts)


# === 标题清洗（保留：_compute_and_write 仍用） ===

def _build_sheet_strip_re(sheet_names: list[str]) -> re.Pattern:
    parts: list[str] = [r"[Ss]heet\s*\d+"]
    for name in sheet_names:
        name = name.strip()
        if name:
            parts.append(re.escape(name))
    pattern = "|".join(parts)
    return re.compile(rf"\s*[-—–:：]?\s*(?:{pattern})\s*[-—–:：]?\s*")


def _strip_sheet_tags(text: str, sheet_names: list[str]) -> str:
    text = _build_sheet_strip_re(sheet_names).sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -—–:：")


def _normalize_report_title(
    answer: str, sheet_names: list[str]
) -> tuple[str, str | None]:
    m = re.search(r"^#\s+(.+?)\s*$", answer, flags=re.MULTILINE)
    if not m:
        return answer, None
    cleaned_title = _strip_sheet_tags(m.group(1), sheet_names)
    if not cleaned_title:
        return answer, None
    new_answer = answer[: m.start(1)] + cleaned_title + answer[m.end(1) :]
    return new_answer, cleaned_title


# === 续聊：stage-first 分支（plan_confirm / qa_ready） ===

async def _continue_conversation(
    app_id: str,
    text: str,
    message_id: str,
    user_id: str,
    conv_id: str,
    sheet_names: list[str],
    *,
    stage: str | None = None,
    sheet_token: str | None = None,
    sheet_id: str | None = None,
    plan: dict | None = None,
    rows_fed: bool = False,
) -> None:
    """根据 entry["stage"] 决定走哪条流程。新流程下 analyze 的所有续聊都靠 stage 路由。"""
    try:
        if app_id == "analyze" and stage == "plan_confirm":
            await _continue_plan_confirm(
                text, message_id, user_id, conv_id,
                sheet_token, sheet_id, plan, sheet_names,
            )
            return
        if app_id == "analyze" and stage == "qa_ready":
            await _handle_qa(
                text, message_id, user_id, conv_id,
                sheet_token, sheet_id, plan, rows_fed, sheet_names,
            )
            return

        # 兜底：未知 stage，告诉用户重新发指令开始
        await reply_text(
            message_id,
            "（这条对话的状态我没找到——可能服务重启过。请重新发 /调研分析 <表格链接> 开始一次新分析。）",
        )
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"调用 Dify 失败：{e}")


async def _continue_plan_confirm(
    text: str,
    message_id: str,
    user_id: str,
    planner_conv_id: str,
    sheet_token: str | None,
    sheet_id: str | None,
    plan: dict | None,
    sheet_names: list[str],
) -> None:
    """plan_confirm 阶段：用户回 OK → 进入计算；否则把意见喂回 planner 出新 plan 再确认。"""
    if not plan or not sheet_token or not sheet_id:
        await reply_text(message_id, "（这次分析的 plan 上下文丢了，请重新发 /调研分析 开始）")
        return

    if survey_plan.is_user_approval(text):
        await _compute_and_write(
            message_id, user_id, sheet_token, sheet_id, plan, sheet_names
        )
        return

    # 修订意见 / 对 open_questions 的回答 → 喂回 planner（带 conv_id）
    planner_key = DIFY_APPS["analyze"]["planner_key"]
    plan_answer, new_planner_conv_id = await _with_heartbeat(
        message_id,
        dify_chat(text, user_id, conversation_id=planner_conv_id, api_key=planner_key),
    )
    new_plan, err = survey_plan.parse_plan_from_llm(
        plan_answer, survey_plan.header_count_from_plan(plan)
    )
    if not new_plan:
        await reply_text(
            message_id,
            f"❌ planner 修订后的 JSON 解析失败：{err}\n"
            f"原始输出（截断 500 字）：\n{plan_answer[:500]}\n\n"
            f"你可以再回复一次澄清你的修改意图。",
        )
        # plan 没变也得让用户继续修订 → 重发原 plan 卡片当锚点
        # 但 conv_id 用最新的，这样下次用户消息能继续在同一会话里改
        bot_msg_id = await reply_markdown(
            message_id, "（沿用上一版 plan，请你重新告诉我具体改什么。）"
        )
        if bot_msg_id:
            _add_pending(
                user_id, bot_msg_id, new_planner_conv_id, "analyze", sheet_names,
                stage="plan_confirm",
                sheet_token=sheet_token, sheet_id=sheet_id, plan=plan,
            )
        return

    # 重新读全表（卡片渲染需要 headers + enrich 需要全表 unique 值）
    rows = await _refresh_rows(sheet_token, sheet_id)
    headers = rows[0] if rows else []

    # 修订后再次跑 alias enrichment（用户的修订意见可能改了角色 / delimiter，需要重算）
    if rows:
        await reply_text(
            message_id,
            "🔍 应用修订后，正在重新扫表把同义选项归并...",
        )
        new_plan = await _with_heartbeat(
            message_id,
            _enrich_plan_with_aliases(new_plan, rows, user_id),
        )

    card_md = survey_plan.render_plan_for_user(new_plan, headers)
    bot_msg_id = await reply_markdown(message_id, card_md)
    if bot_msg_id:
        _add_pending(
            user_id, bot_msg_id, new_planner_conv_id, "analyze", sheet_names,
            stage="plan_confirm",
            sheet_token=sheet_token, sheet_id=sheet_id, plan=new_plan,
        )


async def _refresh_rows(sheet_token: str, sheet_id: str) -> list[list]:
    """重新读全表（headers + body）。失败返回空 list → 上层兜底。"""
    try:
        return await read_sheet_values(sheet_token, sheet_id)
    except Exception:
        traceback.print_exc()
        return []


async def _compute_and_write(
    message_id: str,
    user_id: str,
    sheet_token: str,
    sheet_id: str,
    plan: dict,
    sheet_names: list[str],
) -> None:
    """plan 确认后：Python 算统计 → analyst 首轮写报告 → 飞书文档 → 登 QA pending。"""
    await reply_text(message_id, REPORT_START_MSG)
    rows = await read_sheet_values(sheet_token, sheet_id)
    if len(rows) <= 1:
        await reply_text(message_id, "重新读取表格时发现数据为空，请检查源表是否被改动。")
        return

    stats_md, open_text = survey_stats.compute(rows, plan)
    writer_query = _build_writer_query(stats_md, open_text, plan, rows[0])

    analyst_key = DIFY_APPS["analyze"]["analyst_key"]
    answer, analyst_conv_id = await _with_heartbeat(
        message_id,
        dify_chat(writer_query, user_id, conversation_id="", api_key=analyst_key),
    )
    if not answer.strip():
        await reply_text(message_id, "（analyst 没返回内容）")
        return

    # 数字漂移告警（不阻断，仅记日志）
    drifted = survey_stats.find_numbers_not_in_stats(answer, stats_md)
    if drifted:
        print(f"[stats] WARN report contains numbers not in stats: {drifted[:20]}")

    cleaned, title = _normalize_report_title(answer, sheet_names)
    if not title:
        title = f"调研报告 {datetime.now().strftime('%Y-%m-%d %H%M')}"
    doc_url = await create_doc_from_markdown(title, cleaned, owner_open_id=user_id)
    doc_msg_id = await reply_text(
        message_id,
        f"📄 报告生成完毕：\n{doc_url}\n\n"
        f"💬 对报告有疑问？直接「回复」这条消息提问，我会回到原始数据找答案。",
    )

    # 登 QA pending：conv_id 复用 analyst_conv_id（writer 留下来的会话），
    # rows_fed=False 标记 rows 还没投喂到 Dify 上下文里
    if doc_msg_id and analyst_conv_id:
        _add_pending(
            user_id, doc_msg_id, analyst_conv_id, "analyze", sheet_names,
            stage="qa_ready",
            sheet_token=sheet_token, sheet_id=sheet_id, plan=plan,
            rows_fed=False,
        )


async def _handle_qa(
    question: str,
    message_id: str,
    user_id: str,
    analyst_conv_id: str,
    sheet_token: str | None,
    sheet_id: str | None,
    plan: dict | None,
    rows_fed: bool,
    sheet_names: list[str],
) -> None:
    """报告生成后用户对报告消息追问。复用 writer 的 conv_id 让 Dify 自动带上下文。

    第 1 次 QA 把 rows 投喂到 Dify 历史；之后所有 QA 不再传 rows（节省 token）。
    """
    if not sheet_token or not sheet_id or not plan or not analyst_conv_id:
        await reply_text(message_id, "（这次分析的 QA 上下文丢了，请重新发 /调研分析 开始）")
        return

    if not rows_fed:
        rows = await read_sheet_values(sheet_token, sheet_id)
        rows_block = _format_rows_for_qa(rows, plan)
        qa_query = (
            f"<rows>\n{rows_block}\n</rows>\n\n"
            f"用户问题: {question}"
        )
    else:
        qa_query = question

    analyst_key = DIFY_APPS["analyze"]["analyst_key"]
    answer, new_conv_id = await _with_heartbeat(
        message_id,
        dify_chat(qa_query, user_id, conversation_id=analyst_conv_id, api_key=analyst_key),
    )
    if not answer.strip():
        await reply_text(message_id, "（analyst 没返回内容）")
        return

    reply_msg_id = await reply_markdown(message_id, answer)
    # 同一会话：new_conv_id 应该等于 analyst_conv_id；保险起见用 dify 返回的
    effective_conv_id = new_conv_id or analyst_conv_id
    if reply_msg_id and effective_conv_id:
        _add_pending(
            user_id, reply_msg_id, effective_conv_id, "analyze", sheet_names,
            stage="qa_ready",
            sheet_token=sheet_token, sheet_id=sheet_id, plan=plan,
            rows_fed=True,  # 投喂过一次后永远 True
        )


# === 给 LLM 的查询拼装 ===

def _build_planner_sample(rows: list[list], sample_n: int = 15) -> str:
    """给 planner 的输入：表头 + 前 N 行数据，markdown 表格格式。

    样本量越大，planner 越能看清各列实际取值类型 / 多语言变体；
    但 token 也越多。15 行是经验值——覆盖大部分多语言变体，又不太占 token。
    """
    if not rows:
        return ""
    headers = rows[0]
    sample = rows[1 : 1 + sample_n]

    def esc(s):
        s = "" if s is None else str(s)
        return s.replace("|", "\\|").replace("\n", "<br>")

    md = "| " + " | ".join(esc(h) for h in headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for r in sample:
        cells = [r[i] if i < len(r) else "" for i in range(len(headers))]
        md += "| " + " | ".join(esc(c) for c in cells) + " |\n"

    total_data_rows = max(0, len(rows) - 1)
    return (
        f"<sample>\n"
        f"总数据行数（不含表头）: {total_data_rows}\n"
        f"以下展示表头 + 前 {len(sample)} 行样本：\n\n"
        f"{md}\n"
        f"</sample>\n\n"
        f"请按 JSON schema 输出列分类、part 划分、交叉分析建议、open_questions。"
    )


def _build_writer_query(
    stats_md: str,
    open_text: dict[int, list[str]],
    plan: dict,
    headers: list[str],
) -> str:
    """analyst 首轮的 query：plan + stats + open_text。要求 LLM 按 part 章节写报告，不动数字。"""
    parts_lines = []
    for i, p in enumerate(plan["parts"], 1):
        col_names = []
        for idx in p["column_indexes"]:
            col = next((c for c in plan["columns"] if c["index"] == idx), None)
            name = (col and col.get("name")) or (
                headers[idx] if idx < len(headers) else f"列{idx}"
            )
            role = col["role"] if col else "?"
            col_names.append(f"{name}({role})")
        parts_lines.append(f"  Part {i} {p['name']}: { '; '.join(col_names) }")
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"

    # 开放题：每条原文带玩家 IDs + 画像信息。LLM 用这个做主题归纳 + 引用原话
    open_text_blocks = []
    for col_idx, items in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        name = (col and col.get("name")) or (
            headers[col_idx] if col_idx < len(headers) else f"列{col_idx}"
        )
        rendered_items = []
        for item in items:
            ids = item.get("ids", {})
            profile = item.get("profile", {})
            text = item.get("text", "")
            ids_str = "; ".join(f"{k}={v}" for k, v in ids.items()) or "(无ID)"
            prof_str = (
                "; ".join(f"{k}={v}" for k, v in profile.items()) or "(无画像)"
            )
            rendered_items.append(
                f"- 玩家[{ids_str} | 画像: {prof_str}]:\n  {text}"
            )

        joined = "\n".join(rendered_items)
        # 单题原文超长时截断（保留开头部分；后续 QA 阶段 rows 会传全量给 LLM）
        if len(joined) > 30000:
            kept = []
            cur_len = 0
            for line in rendered_items:
                if cur_len + len(line) > 28000:
                    break
                kept.append(line)
                cur_len += len(line)
            joined = "\n".join(kept)
            joined += (
                f"\n…（共 {len(items)} 条原文，已截取前 {len(kept)} 条；"
                f"完整内容会在用户追问时提供）"
            )
        open_text_blocks.append(
            f"### {name}（列 {col_idx}, 共 {len(items)} 条非空回答）\n{joined}"
        )
    open_text_md = (
        "<open_text>\n" + "\n\n".join(open_text_blocks) + "\n</open_text>"
        if open_text_blocks
        else "<open_text>（本问卷没有开放题）</open_text>"
    )

    return (
        "**任务**：基于以下确定性统计数据撰写完整调研报告。\n\n"
        f"{plan_summary}\n\n"
        f"<stats>\n{stats_md}\n</stats>\n\n"
        f"{open_text_md}\n\n"
        "请按 Dify 应用 prompt 里规定的报告规范撰写。关键提醒：\n"
        "- `<stats>` 里所有数字已算好，严禁修改 / 重算 / 四舍五入\n"
        "- 报告按 plan 里的 parts 顺序分章节，每个 part 内同时综合客观题统计 + 主观题归纳\n"
        "- 主观题：每条原话已附「玩家ID + 画像」，引用时按玩家 ID 优先级（mlbbid > discord > whatsapp）展示\n"
        "- 所有结论必须中文返回"
    )


def _format_rows_for_qa(rows: list[list], plan: dict) -> str:
    """把 rows 序列化成 JSON-line 给 QA 用。每行用列名做 key，便于 LLM 精确定位。

    超过 _QA_ROWS_MAX_CHARS 时按画像维度分层抽样到 100 行（保留分布）。
    """
    if not rows or len(rows) <= 1:
        return "（无数据）"
    headers = rows[0]
    body = rows[1:]
    total = len(body)

    # 列名映射
    col_names = []
    for i, h in enumerate(headers):
        h = (h or "").strip() or f"col_{i}"
        col_names.append(h)

    def row_to_obj(row):
        return {
            col_names[i]: (row[i] if i < len(row) else "")
            for i in range(len(col_names))
        }

    selected = body
    note_prefix = ""
    full_dump = "\n".join(
        json.dumps(row_to_obj(r), ensure_ascii=False) for r in selected
    )
    if len(full_dump) > _QA_ROWS_MAX_CHARS:
        # 按 profile_dim 分层抽样到 100 行
        profile_indexes = [
            c["index"] for c in plan["columns"] if c["role"] == "profile_dim"
        ]
        sampled = _stratified_sample(body, profile_indexes, target=100)
        note_prefix = (
            f"# 注意：原始数据共 {total} 行，超出上下文上限。\n"
            f"# 已按画像维度分层抽样到 {len(sampled)} 行供查询。\n"
            f"# 涉及全量统计的问题请参考 stats 块（已包含全部 {total} 行）。\n\n"
        )
        selected = sampled
        full_dump = "\n".join(
            json.dumps(row_to_obj(r), ensure_ascii=False) for r in selected
        )

    return note_prefix + full_dump


def _stratified_sample(
    body: list[list], profile_indexes: list[int], target: int = 100
) -> list[list]:
    """按 profile_indexes 列的取值组合做分层抽样到 target 行。"""
    if not profile_indexes or len(body) <= target:
        return body[:target]

    def key(row):
        return tuple(
            (row[i] if i < len(row) else "")
            for i in profile_indexes
        )

    buckets: dict[tuple, list[list]] = {}
    for r in body:
        buckets.setdefault(key(r), []).append(r)

    # 按各桶比例分配
    out: list[list] = []
    total = len(body)
    for k, items in buckets.items():
        share = max(1, round(len(items) / total * target))
        out.extend(items[:share])
        if len(out) >= target:
            break
    return out[:target]


# === 长连接事件入口 ===
# lark-oapi 的事件回调是同步函数，但我们的业务逻辑全是 async（要 await 调
# 飞书/Dify HTTP API、跑心跳）。开一个独立的 asyncio 事件循环跑在后台线程，
# 同步回调里只负责把协程调度上去，立刻返回（飞书要求事件 3 秒内 ack）。
_bg_loop = asyncio.new_event_loop()


def _start_bg_loop() -> None:
    asyncio.set_event_loop(_bg_loop)
    _bg_loop.run_forever()


threading.Thread(target=_start_bg_loop, daemon=True).start()


def _on_message_received(data: P2ImMessageReceiveV1) -> None:
    payload = json.loads(lark.JSON.marshal(data))
    event_id = (payload.get("header") or {}).get("event_id", "")
    if event_id and _is_duplicate(event_id):
        return
    event = payload.get("event") or {}
    asyncio.run_coroutine_threadsafe(_safe_handle(event), _bg_loop)


async def _safe_handle(event: dict) -> None:
    try:
        await _handle_message(event)
    except Exception:
        traceback.print_exc()


def main() -> None:
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message_received)
        .build()
    )
    client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    print("[bot] starting long-connection client...")
    client.start()  # 阻塞，直到进程退出


if __name__ == "__main__":
    main()
