import asyncio
import re
from urllib.parse import parse_qs, urlparse

import httpx

from feishu import FEISHU_BASE, get_tenant_access_token

# 飞书限流错误码（这几个出现时应该退避重试，不是真的写失败）
_RATE_LIMIT_CODES = {90217, 99991400, 99991401, 99991408, 1310213}

# 匹配飞书/Lark 云文档 URL：sheets / wiki / docx
FEISHU_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9.\-]+\.(?:feishu\.cn|larksuite\.com)/(sheets|wiki|docx|docs|base)/([a-zA-Z0-9]+)[^\s]*"
)


def parse_feishu_link(text: str) -> dict | None:
    m = FEISHU_URL_RE.search(text)
    if not m:
        return None

    full_url = m.group(0)
    doc_type = m.group(1)
    token = m.group(2)

    sheet_id = None
    try:
        qs = parse_qs(urlparse(full_url).query)
        sheet_id = (qs.get("sheet") or [None])[0]
    except Exception:
        pass

    return {"type": doc_type, "token": token, "sheet_id": sheet_id, "url": full_url}


async def resolve_wiki_node(wiki_token: str) -> dict:
    """Wiki 节点 → 底层对象（sheet/docx/...）。"""
    access = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{FEISHU_BASE}/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {access}"},
            params={"token": wiki_token},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"解析 wiki 节点失败: {data}")
        node = data["data"]["node"]
        return {"obj_type": node["obj_type"], "obj_token": node["obj_token"]}


async def list_sheets(spreadsheet_token: str) -> list[dict]:
    access = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{FEISHU_BASE}/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
            headers={"Authorization": f"Bearer {access}"},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 sheets 列表失败: {data}")
        return data["data"]["sheets"]


async def get_sheet_meta(spreadsheet_token: str, sheet_id: str) -> dict:
    access = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{FEISHU_BASE}/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/{sheet_id}",
            headers={"Authorization": f"Bearer {access}"},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 sheet 元信息失败: {data}")
        return data["data"]["sheet"]


def col_to_letter(col_idx: int) -> str:
    """1-based 列号 → 字母（1=A, 27=AA）。"""
    result = ""
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        result = chr(65 + rem) + result
    return result


async def insert_columns(
    spreadsheet_token: str, sheet_id: str, start_index: int, count: int = 1
) -> None:
    """在 sheet 的 start_index（0-based）位置插入 count 列。失败抛 RuntimeError。"""
    access = await get_tenant_access_token()
    body = {
        "dimension": {
            "sheetId": sheet_id,
            "majorDimension": "COLUMNS",
            "startIndex": start_index,
            "endIndex": start_index + count,
        }
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/insert_dimension_range",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"插入列失败: {result}")


async def write_cell(
    spreadsheet_token: str,
    sheet_id: str,
    cell_range: str,
    value,
    max_retries: int = 4,
) -> bool:
    """写一个单元格区域。cell_range 形如 'A1' 或 'A1:A1'。失败返回 False（不抛异常）。

    飞书要求 range 必须是闭区间（"A1:A1"）；如果传进来是单格（"A1"），自动补成 "A1:A1"。
    遇到限流错误码（90217 等）会指数退避重试，避免行级并发把飞书打到限流。
    """
    if ":" not in cell_range:
        cell_range = f"{cell_range}:{cell_range}"
    body = {
        "valueRange": {
            "range": f"{sheet_id}!{cell_range}",
            "values": [[value]],
        }
    }

    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(max_retries):
            access = await get_tenant_access_token()
            try:
                resp = await client.put(
                    f"{FEISHU_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                    headers={
                        "Authorization": f"Bearer {access}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=body,
                )
                result = resp.json()
                code = result.get("code", 0)
                if code == 0:
                    return True
                if code in _RATE_LIMIT_CODES:
                    wait = min(2 ** attempt, 8)
                    print(
                        f"[feishu_sheets] write_cell rate-limited (code={code}); "
                        f"retry in {wait}s ({attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                # 非限流类业务错误：不重试，直接报失败
                print(f"[feishu_sheets] write_cell failed: {result}")
                return False
            except Exception as e:
                wait = min(2 ** attempt, 8)
                print(f"[feishu_sheets] write_cell exc: {e}; retry in {wait}s")
                await asyncio.sleep(wait)
    print(f"[feishu_sheets] write_cell retry exhausted ({cell_range})")
    return False


async def fetch_raw_rows(link_info: dict) -> tuple[str, str, str, list[list]]:
    """读取链接对应的 sheet，返回 (spreadsheet_token, sheet_id, sheet_title, rows)。

    rows 是原始的 list[list]，第 0 行是表头。/反馈打标 这种需要逐行处理 + 写回的
    场景用这个，不要走 fetch_as_markdown（那个是为 LLM 输入准备的）。
    """
    doc_type = link_info["type"]
    token = link_info["token"]
    sheet_id_q = link_info.get("sheet_id")

    if doc_type == "wiki":
        resolved = await resolve_wiki_node(token)
        if resolved["obj_type"] != "sheet":
            raise RuntimeError(
                f"Wiki 节点是 {resolved['obj_type']}，不是表格"
            )
        token = resolved["obj_token"]
        doc_type = "sheets"

    if doc_type != "sheets":
        raise RuntimeError(f"暂不支持的链接类型：{doc_type}（需要飞书表格）")

    sheets = await list_sheets(token)
    if not sheets:
        raise RuntimeError("表格里没有 sheet")

    chosen = None
    if sheet_id_q:
        chosen = next((s for s in sheets if s["sheet_id"] == sheet_id_q), None)
    chosen = chosen or sheets[0]

    sid = chosen["sheet_id"]
    title = chosen.get("title", sid)
    rows = await read_sheet_values(token, sid)
    return token, sid, title, rows


async def read_sheet_values(
    spreadsheet_token: str, sheet_id: str, batch_rows: int = 200
) -> list[list]:
    """读整个 sheet。按行分批避免飞书 10MB/次的硬限制。"""
    meta = await get_sheet_meta(spreadsheet_token, sheet_id)
    grid = meta.get("grid_properties", {}) or {}
    row_count = int(grid.get("row_count") or 0)
    col_count = int(grid.get("column_count") or 0)

    if row_count <= 0 or col_count <= 0:
        return []

    end_col = col_to_letter(col_count)
    access = await get_tenant_access_token()
    all_values: list[list] = []

    async with httpx.AsyncClient(timeout=60) as client:
        start = 1
        cur_batch = batch_rows
        while start <= row_count:
            end = min(start + cur_batch - 1, row_count)
            range_str = f"{sheet_id}!A{start}:{end_col}{end}"
            resp = await client.get(
                f"{FEISHU_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}",
                headers={"Authorization": f"Bearer {access}"},
            )
            data = resp.json()

            if data.get("code") == 90221:
                # 单批仍超 10MB，缩半再试当前批
                if cur_batch <= 5:
                    raise RuntimeError(
                        f"读取表格失败：第 {start} 行起内容过大，无法继续分批"
                    )
                cur_batch = max(5, cur_batch // 2)
                continue

            if data.get("code") != 0:
                raise RuntimeError(f"读取表格失败 (行 {start}-{end}): {data}")

            chunk = data["data"]["valueRange"]["values"] or []
            all_values.extend(chunk)
            start = end + 1

    while all_values and all(_is_blank(c) for c in all_values[-1]):
        all_values.pop()
    return all_values


def _is_blank(cell) -> bool:
    if cell is None or cell == "":
        return True
    if isinstance(cell, list) and not cell:
        return True
    return False


def _format_cell(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, list):
        # 富文本 cell：[{"text": "...", "type": "text"}, ...]
        parts = []
        for item in cell:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(cell)


def values_to_markdown(values: list[list]) -> str:
    if not values:
        return ""

    rows = [[_format_cell(c) for c in row] for row in values]
    max_len = max(len(row) for row in rows)
    rows = [row + [""] * (max_len - len(row)) for row in rows]

    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []

    def escape(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", "<br>")

    md = "| " + " | ".join(escape(c) for c in header) + " |\n"
    md += "| " + " | ".join(["---"] * max_len) + " |\n"
    for row in body:
        md += "| " + " | ".join(escape(c) for c in row) + " |\n"
    return md


async def fetch_as_markdown(link_info: dict) -> tuple[str, list[str]]:
    """读取链接对应的内容并转为 markdown，同时返回所读取的 sheet 名列表。"""
    doc_type = link_info["type"]
    token = link_info["token"]
    sheet_id = link_info.get("sheet_id")

    if doc_type == "wiki":
        resolved = await resolve_wiki_node(token)
        obj_type = resolved["obj_type"]
        token = resolved["obj_token"]
        if obj_type in ("sheet", "bitable"):
            doc_type = "sheets"
        elif obj_type in ("docx", "doc"):
            doc_type = "docx"
        else:
            raise RuntimeError(f"Wiki 节点类型 {obj_type} 暂不支持")

    if doc_type == "sheets":
        sheets = await list_sheets(token)
        if not sheets:
            raise RuntimeError("表格里没有 sheet")

        if sheet_id:
            filtered = [s for s in sheets if s["sheet_id"] == sheet_id]
            sheets_to_read = filtered or sheets[:1]
        else:
            # 默认只读第一个 sheet，避免多 sheet 工作簿被全部拉下来
            sheets_to_read = sheets[:1]

        parts = []
        sheet_names: list[str] = []
        for sheet in sheets_to_read:
            sid = sheet["sheet_id"]
            title = sheet.get("title", sid)
            sheet_names.append(title)
            values = await read_sheet_values(token, sid)
            md = values_to_markdown(values)
            row_count = max(0, len(values) - 1)  # 不含表头
            col_count = len(values[0]) if values else 0
            meta = (
                "<metadata>\n"
                f"sheet 名: {title}\n"
                f"总回复数: {row_count} 份（已读取，不含表头行）\n"
                f"列数: {col_count}\n"
                f"处理要求: 必须分析全部 {row_count} 份回复。如果你判断某些回复"
                f"需要排除（例如空答、乱码、明显测试数据），必须在报告开头明确列出：\n"
                f"  1) 被排除的回复对应的玩家 ID（discord 或 whatsapp）\n"
                f"  2) 排除该回复的具体原因\n"
                f"未在排除列表中明确列出的，全部默认为有效数据。"
                "\n</metadata>\n"
            )
            parts.append(f"{meta}\n## {title}\n\n{md}".rstrip())

        if len(sheets) > 1 and not sheet_id:
            others = [s.get("title", s["sheet_id"]) for s in sheets[1:]]
            print(
                f"[sheets] 工作簿有多个 sheet，默认只读第一个 "
                f"'{sheets_to_read[0].get('title')}'；其它未读：{others}"
            )

        return "\n\n".join(parts), sheet_names

    raise RuntimeError(f"暂不支持的链接类型：{doc_type}（目前只读取飞书表格/wiki 表格）")
