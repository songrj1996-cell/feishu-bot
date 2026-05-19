import asyncio
import json
import re
import threading
import time
import traceback
from datetime import datetime

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

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
from feishu_sheets import fetch_as_markdown, parse_feishu_link

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
) -> None:
    pending = _get_pending(user_id)
    pending[bot_msg_id] = {
        "conv_id": conv_id,
        "app_id": app_id,
        "sheet_names": sheet_names,
        "ts": time.time(),
    }


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
            "📊 收到表格链接，正在读取数据。如果有需要确认的问题，会先和你对一下，再生成报告。",
        )
        asyncio.create_task(_run_analyze(link, message_id, user_id))
        return

    # 未来扩展点：其他 app_id 在这里加 elif 分支
    await reply_text(message_id, f"指令 {cmd} 还没接入处理逻辑。")


async def _heartbeat(message_id: str, interval: int = 300) -> None:
    elapsed_min = 0
    try:
        while True:
            await asyncio.sleep(interval)
            elapsed_min += interval // 60
            await reply_text(
                message_id, f"⏳ 还在生成中... 已等待 {elapsed_min} 分钟"
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


async def _run_analyze(link: dict, message_id: str, user_id: str) -> None:
    try:
        sheet_md, sheet_names = await fetch_as_markdown(link)
        if not sheet_md.strip():
            await reply_text(message_id, "表格读取后内容为空，请检查权限或内容。")
            return

        api_key = DIFY_APPS["analyze"]["api_key"]
        # 每次新表格 = 新会话，不污染上下文
        answer, new_conv_id = await _with_heartbeat(
            message_id,
            dify_chat(sheet_md, user_id, conversation_id="", api_key=api_key),
        )

        if not answer.strip():
            await reply_text(message_id, "Dify 没有返回内容。")
            return

        bot_msg_id = await _send_analyze_answer(
            answer, message_id, user_id, sheet_names
        )
        # 卡片（澄清问题）→ 登记 pending，等用户用「回复」回答
        if bot_msg_id and new_conv_id:
            _add_pending(
                user_id, bot_msg_id, new_conv_id, "analyze", sheet_names
            )
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"处理失败：{e}")


def _looks_like_report(answer: str) -> bool:
    # 有 markdown 标题（# / ## / ### ...）= 报告
    if re.search(r"^#{1,6}\s", answer, flags=re.MULTILINE):
        return True
    # 没标题但很长也按报告处理（兜底）
    return len(answer) >= 1500


async def _send_analyze_answer(
    answer: str, message_id: str, user_id: str, sheet_names: list[str]
) -> str | None:
    """发送 Dify 答复给用户。卡片返回 bot_msg_id，文档返回 None。"""
    if _looks_like_report(answer):
        cleaned_answer, title = _normalize_report_title(answer, sheet_names)
        if not title:
            title = f"调研报告 {datetime.now().strftime('%Y-%m-%d %H%M')}"
        doc_url = await create_doc_from_markdown(
            title, cleaned_answer, owner_open_id=user_id
        )
        await reply_text(message_id, f"📄 报告生成完毕：\n{doc_url}")
        return None
    # 短回复 / 对话腔 / 含问号 = 澄清问题，用卡片让用户回答
    return await reply_markdown(message_id, answer)


def _build_sheet_strip_re(sheet_names: list[str]) -> re.Pattern:
    """构造能匹配 'Sheet N' 和具体 sheet 名（含周边连接符空白）的正则。"""
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
    """提取首个 # 标题并去掉 sheet 字样；返回 (清洗后 markdown, 清洗后标题)。

    标题里和 markdown 里都做替换，确保飞书文档名 = 文档内显示标题。
    """
    m = re.search(r"^#\s+(.+?)\s*$", answer, flags=re.MULTILINE)
    if not m:
        return answer, None
    cleaned_title = _strip_sheet_tags(m.group(1), sheet_names)
    if not cleaned_title:
        return answer, None
    new_answer = answer[: m.start(1)] + cleaned_title + answer[m.end(1) :]
    return new_answer, cleaned_title


async def _continue_conversation(
    app_id: str,
    text: str,
    message_id: str,
    user_id: str,
    conv_id: str,
    sheet_names: list[str],
) -> None:
    try:
        api_key = DIFY_APPS[app_id]["api_key"]
        answer, new_conv_id = await _with_heartbeat(
            message_id,
            dify_chat(text, user_id, conversation_id=conv_id, api_key=api_key),
        )

        if not answer.strip():
            await reply_text(message_id, "（Dify 未返回内容）")
            return

        effective_conv_id = new_conv_id or conv_id

        if app_id == "analyze":
            bot_msg_id = await _send_analyze_answer(
                answer, message_id, user_id, sheet_names
            )
        else:
            bot_msg_id = await reply_markdown(message_id, answer)

        # 还在追问（又是卡片）→ 重新登记 pending，让下一轮回答能找到这条会话
        if bot_msg_id and effective_conv_id:
            _add_pending(
                user_id, bot_msg_id, effective_conv_id, app_id, sheet_names
            )
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"调用 Dify 失败：{e}")


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
