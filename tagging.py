"""/反馈打标 流程：探路者识别需打标的列 → 插入新列 → 行级并发调干活者 → 写回原表格。

跟 /调研分析 不同的是：
- 用 Dify completion-messages 接口（一次性请求，每行独立）
- 直接写回到原表格（插列 + 写单元格），不出文档
- 上百行处理可能跑 30+ 分钟，所以走自己的进度心跳
"""

import asyncio
import json
import re
import traceback
from typing import Optional

from config import DIFY_APPS, TAGGING_CONCURRENCY
from dify import STOP_SIGNAL, complete as dify_complete
from feishu import reply_text
from feishu_sheets import (
    col_to_letter,
    fetch_raw_rows,
    insert_columns,
    write_cell,
)

# 同表互斥：避免对同一张表并发触发（造成插列重复、覆盖数据）
_in_flight_tokens: set[str] = set()


# === JSON 三级解析（容错 LLM 偶发输出怪格式） ===

# 兜底正则：LLM 有时在 JSON 字符串里写未转义的内嵌双引号（如 `比如"单杀"、"传奇"等`）
# 触发条件：前后都不是 JSON 结构字符；捕获组也不含 , : { } [ ]，避免吞掉真正的边界
_INNER_QUOTE_RE = re.compile(r'(?<=[^\s,:{}\[\]])"([^"\n,:{}\[\]]{1,80})"(?=[^\s,:{}\[\]])')


def _repair_inner_quotes(s: str) -> str:
    return _INNER_QUOTE_RE.sub(r"「\1」", s)


def _regex_extract(text: str) -> Optional[dict]:
    """JSON 完全解析失败时的最后兜底：用正则把 label/translation 抠出来。"""
    text = _repair_inner_quotes(text)
    out: dict = {"single_questions": {}, "overall_summary": {}}
    pat = re.compile(
        r'"col_(\d+)"\s*:\s*\{+\s*'
        r'(?:"label"\s*:\s*"([^"\n]*)"\s*,\s*"translation"\s*:\s*"([^"\n]*)"'
        r'|"translation"\s*:\s*"([^"\n]*)"\s*,\s*"label"\s*:\s*"([^"\n]*)")',
        re.DOTALL,
    )
    for m in pat.finditer(text):
        col = m.group(1)
        if m.group(2) is not None:
            label, trans = m.group(2), m.group(3)
        else:
            label, trans = m.group(5), m.group(4)
        out["single_questions"][f"col_{col}"] = {"label": label, "translation": trans}
    m = re.search(
        r'"overall_summary"\s*:\s*\{[^{}]*?"label"\s*:\s*"([^"\n]*)"',
        text, re.DOTALL,
    )
    if m:
        out["overall_summary"]["label"] = m.group(1)
    if out["single_questions"] or out["overall_summary"]:
        return out
    return None


def _parse_dify_json(raw: str) -> Optional[dict]:
    """LLM 返回值剥 ```json ``` 包裹后做严格 → 修复引号 → 正则三级解析。"""
    cleaned = re.sub(r"`{3}json\s*|`{3}", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_repair_inner_quotes(cleaned))
    except json.JSONDecodeError:
        pass
    return _regex_extract(cleaned)


# === 进度跟踪 + 心跳 ===

class _Progress:
    """行级任务和心跳协程共享的进度计数器。"""

    def __init__(self, total: int):
        self.total = total
        self.success = 0
        self.empty = 0
        self.failed = 0
        self.error = 0

    @property
    def done(self) -> int:
        return self.success + self.empty + self.failed + self.error


async def _heartbeat(message_id: str, progress: _Progress, interval: int = 300) -> None:
    elapsed_min = 0
    try:
        while True:
            await asyncio.sleep(interval)
            elapsed_min += interval // 60
            await reply_text(
                message_id,
                f"在打标了在打标了，别急别急！已处理 {progress.done}/{progress.total} 行"
                f"（已用时 {elapsed_min} 分钟）",
            )
    except asyncio.CancelledError:
        pass


# === 行任务 ===

async def _process_row(
    r_idx: int,
    row_snapshot: list,
    target_cols: list[int],
    final_col_map: dict[int, str],
    spreadsheet_token: str,
    sheet_id: str,
    worker_key: str,
    sem: asyncio.Semaphore,
) -> str:
    """处理一行：返回 SUCCESS / EMPTY / FAILED / ERROR / STOP_SIGNAL。"""
    async with sem:
        log_prefix = f"[tagging 行{r_idx + 1}]"
        try:
            row_data = {
                f"col_{c}": (row_snapshot[c] if c < len(row_snapshot) else "")
                for c in target_cols
            }
            if not any(str(v).strip() for v in row_data.values()):
                return "EMPTY"

            # 调干活者，流式截断 / JSON 解析失败时重试一次
            result = None
            for attempt in range(2):
                raw = await dify_complete(
                    inputs={"survey_batch": json.dumps(row_data, ensure_ascii=False)},
                    query="打标任务",
                    api_key=worker_key,
                    log_prefix=log_prefix,
                )
                if raw == STOP_SIGNAL:
                    return STOP_SIGNAL
                if not raw:
                    continue
                result = _parse_dify_json(raw)
                if result is not None:
                    break
                print(
                    f"{log_prefix} 解析失败 (尝试 {attempt + 1}/2)，"
                    f"末尾 200 字: ...{raw[-200:]}"
                )
            if result is None:
                return "FAILED"

            # 写回：A 列 = overall_summary，每个目标列右边一列 = 标签 + 翻译
            row_no = r_idx + 1
            cells: list[tuple[str, str]] = []
            overall = (result.get("overall_summary") or {}).get("label", "N/A")
            cells.append((f"A{row_no}", overall))
            for col_key, item in (result.get("single_questions") or {}).items():
                m = re.search(r"\d+", str(col_key))
                if not m:
                    continue
                target_letter = final_col_map.get(int(m.group()))
                if not target_letter:
                    continue
                item = item or {}
                display = f"【{item.get('label', 'N/A')}】\n{item.get('translation', '')}"
                cells.append((f"{target_letter}{row_no}", display))

            failed = 0
            for cell_ref, value in cells:
                ok = await write_cell(
                    spreadsheet_token, sheet_id, cell_ref, value
                )
                if not ok:
                    failed += 1
            if failed == len(cells):
                return "FAILED"
            print(
                f"{log_prefix} ✅ 写入 {len(cells) - failed}/{len(cells)} 个单元格"
            )
            return "SUCCESS"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            print(f"{log_prefix} 处理异常: {e}")
            return "ERROR"


# === 主流程 ===

async def run_tagging(link_info: dict, message_id: str, user_id: str) -> None:
    """/反馈打标 入口：被 main.py 通过 asyncio.create_task 调起。"""
    # 1. 解析链接 + 拉数据
    try:
        spreadsheet_token, sheet_id, sheet_title, all_rows = await fetch_raw_rows(
            link_info
        )
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"❌ 读取表格失败：{e}")
        return

    # 同表互斥
    if spreadsheet_token in _in_flight_tokens:
        await reply_text(
            message_id,
            "⚠️ 这张表格已有打标任务在跑，本次触发跳过。等当前任务完成后再试。",
        )
        return
    _in_flight_tokens.add(spreadsheet_token)

    try:
        if not all_rows or len(all_rows) < 2:
            await reply_text(message_id, "⚠️ 表格为空或只有表头。")
            return

        # 数据范围探测：从第 2 行起遇到完全空行就停（飞书空单元格返回 None）
        def _is_blank_row(row: list) -> bool:
            for c in (row or []):
                if c is None or str(c).strip() == "":
                    continue
                return False
            return True

        data_end = len(all_rows)
        for i in range(1, len(all_rows)):
            if _is_blank_row(all_rows[i]):
                data_end = i
                break
        all_rows = all_rows[:data_end]
        total_rows = len(all_rows) - 1
        if total_rows <= 0:
            await reply_text(message_id, "⚠️ 表格里没有数据行。")
            return
        print(f"[tagging] 实际数据行数：{total_rows}")

        # 2. 探路者识别需要打标的列
        cfg = DIFY_APPS["tagging"]
        explorer_key = cfg["explorer_key"]
        worker_key = cfg["worker_key"]

        print(f"[tagging] 🕵️ 表头: {str(all_rows[0])[:300]}")
        ans1 = await dify_complete(
            inputs={"header": str(all_rows[0])},
            query="返回数组如 [6, 8]",
            api_key=explorer_key,
            log_prefix="[tagging 探路者]",
        )
        if ans1 == STOP_SIGNAL:
            await reply_text(
                message_id,
                "🛑 探路者熔断（Dify 400）。常见原因：变量类型为 Short Text "
                "（48 字符上限），去 Dify 后台改成 Paragraph 后重试。",
            )
            return
        target_cols = sorted({int(n) for n in re.findall(r"\d+", ans1 or "")})
        if not target_cols:
            await reply_text(
                message_id,
                f"❌ 探路者未返回有效列号。原始返回：{(ans1 or '(空)')[:200]}",
            )
            return
        print(f"[tagging] 探路者识别列: {target_cols}")

        # 3. 插入新列结构（最左 1 列 + 每个目标列右边 1 列）
        await reply_text(
            message_id,
            f"🏗️ 识别到 {len(target_cols)} 个待打标列，正在插入新列结构...",
        )
        await insert_columns(spreadsheet_token, sheet_id, start_index=0, count=1)
        await asyncio.sleep(0.3)

        # 必须升序插入，每次插入都推动后续位置 → 累计 inserted 计数补偿
        final_col_map: dict[int, str] = {}
        inserted = 0
        for col_idx in sorted(target_cols):
            current_pos = col_idx + 1 + inserted  # +1 for leading 列；再加之前插入数
            await insert_columns(
                spreadsheet_token, sheet_id, start_index=current_pos, count=1
            )
            final_col_map[col_idx] = col_to_letter(current_pos + 1)
            inserted += 1
            await asyncio.sleep(0.3)

        # 4. 写新列表头
        await write_cell(spreadsheet_token, sheet_id, "A1", "📊 总体反馈")
        original_header = all_rows[0] if all_rows else []
        for col_idx, target_letter in final_col_map.items():
            orig = (
                original_header[col_idx]
                if col_idx < len(original_header) and original_header[col_idx]
                else ""
            )
            await write_cell(
                spreadsheet_token, sheet_id, f"{target_letter}1", f"🏷️ {orig}"
            )

        # 5. 行级并发处理（带心跳）
        await reply_text(
            message_id,
            f"🚀 开始处理 {total_rows} 行（并发 {TAGGING_CONCURRENCY}），"
            f"中途每 5 分钟会同步一次进度。",
        )

        progress = _Progress(total=total_rows)
        sem = asyncio.Semaphore(TAGGING_CONCURRENCY)
        hb_task = asyncio.create_task(_heartbeat(message_id, progress))

        try:
            tasks = [
                asyncio.create_task(
                    _process_row(
                        i, all_rows[i], target_cols, final_col_map,
                        spreadsheet_token, sheet_id, worker_key, sem,
                    )
                )
                for i in range(1, len(all_rows))
            ]
            stopped = False
            try:
                for fut in asyncio.as_completed(tasks):
                    try:
                        sig = await fut
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        progress.error += 1
                        continue
                    if sig == STOP_SIGNAL:
                        stopped = True
                        break
                    if sig == "SUCCESS":
                        progress.success += 1
                    elif sig == "EMPTY":
                        progress.empty += 1
                    elif sig == "FAILED":
                        progress.failed += 1
                    else:
                        progress.error += 1
            finally:
                if stopped:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

            if stopped:
                await reply_text(
                    message_id,
                    "🛑 干活者熔断（Dify 400），后续行已停止处理。"
                    "去 Dify 后台把变量类型改成 Paragraph 后重试。",
                )
                return

            await reply_text(
                message_id,
                f"🎉 全部完成！\n"
                f"✅ 成功 {progress.success}\n"
                f"⬜ 空行 {progress.empty}\n"
                f"⚠️ 失败 {progress.failed}\n"
                f"💥 异常 {progress.error}",
            )
            print(
                f"[tagging] DONE | success={progress.success} "
                f"empty={progress.empty} failed={progress.failed} error={progress.error}"
            )
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"❌ 打标流程异常：{e}")
    finally:
        _in_flight_tokens.discard(spreadsheet_token)
