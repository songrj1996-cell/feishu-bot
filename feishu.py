import json
import re
import time

import httpx

from config import FEISHU_APP_ID, FEISHU_APP_SECRET

FEISHU_BASE = "https://open.feishu.cn/open-apis"

_token_cache: dict = {"value": "", "expires_at": 0.0}


async def get_tenant_access_token() -> str:
    now = time.time()
    if _token_cache["value"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["value"]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
        _token_cache["value"] = data["tenant_access_token"]
        _token_cache["expires_at"] = now + data["expire"]
        return _token_cache["value"]


async def reply_text(message_id: str, text: str) -> None:
    token = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"[feishu] reply failed: {data}")


# 飞书单条文本约 30K 字符上限，留余量
LONG_TEXT_LIMIT = 25000


def _split_text(text: str, limit: int = LONG_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    separators = ("\n## ", "\n# ", "\n\n", "\n", "")
    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        cut_at = limit
        window = remaining[:limit]
        for sep in separators:
            if not sep:
                break
            idx = window.rfind(sep)
            if idx > 0:
                cut_at = idx
                break
        chunks.append(remaining[:cut_at].rstrip())
        remaining = remaining[cut_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


async def reply_long_text(message_id: str, text: str) -> None:
    chunks = _split_text(text)
    if len(chunks) == 1:
        await reply_text(message_id, chunks[0])
        return

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        await reply_text(message_id, f"（{i}/{total}）\n{chunk}")


def _normalize_for_feishu_md(text: str) -> str:
    # 飞书 Lark MD 不支持 # 标题，转成 **粗体**
    return re.sub(r"^(#{1,6})\s+(.+?)\s*$", r"**\2**", text, flags=re.MULTILINE)


async def _reply_card(message_id: str, markdown_content: str) -> None:
    token = await get_tenant_access_token()
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "markdown", "content": _normalize_for_feishu_md(markdown_content)}
        ],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"[feishu] reply card failed: {data}")


async def reply_markdown(message_id: str, text: str) -> None:
    chunks = _split_text(text)
    if len(chunks) == 1:
        await _reply_card(message_id, chunks[0])
        return

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        await _reply_card(message_id, f"（{i}/{total}）\n\n{chunk}")
