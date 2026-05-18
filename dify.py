import json

import httpx

from config import DIFY_API_BASE, DIFY_API_KEY


async def chat(
    query: str,
    user: str,
    conversation_id: str = "",
    api_key: str | None = None,
) -> tuple[str, str]:
    """调用 Dify chat-messages（streaming）。返回 (answer, conversation_id)。

    api_key 不传时用 .env 里默认的 DIFY_API_KEY。多业务时由 main 传入对应应用的 key。
    """
    api_key = api_key or DIFY_API_KEY
    if not api_key:
        raise RuntimeError("缺少 Dify API Key")

    answer_parts: list[str] = []
    new_conv_id = conversation_id

    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming",
        "user": user,
        "conversation_id": conversation_id,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    print(
        f"[dify] -> POST | conv_id={conversation_id or '(new)'} | "
        f"query_len={len(query)}"
    )

    async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
        async with client.stream(
            "POST",
            f"{DIFY_API_BASE}/chat-messages",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                err_body = await resp.aread()
                raise RuntimeError(
                    f"Dify {resp.status_code}: {err_body.decode('utf-8', errors='replace')}"
                )
            msg_chunks = 0
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event = data.get("event")
                if event in ("message", "agent_message"):
                    answer_parts.append(data.get("answer", ""))
                    msg_chunks += 1
                if data.get("conversation_id"):
                    new_conv_id = data["conversation_id"]
                if event == "error":
                    raise RuntimeError(
                        f"Dify error: {data.get('code')} {data.get('message')}"
                    )
                if event in ("message_end", "workflow_finished", "agent_message_end"):
                    break

    total_chars = sum(len(p) for p in answer_parts)
    print(f"[dify] <- done | chunks={msg_chunks} | answer_len={total_chars}")
    return "".join(answer_parts), new_conv_id
