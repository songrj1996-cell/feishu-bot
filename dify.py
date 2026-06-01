import asyncio
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


# 熔断信号：Dify 返回 400 时，表示请求结构 / 变量类型有问题，重试也救不了，
# 让上层立即停整个批量任务，避免空跑几百次。
STOP_SIGNAL = "STOP_SIGNAL"


async def complete(
    inputs: dict,
    query: str,
    api_key: str,
    user: str = "bot",
    max_retries: int = 3,
    log_prefix: str = "",
) -> str:
    """调用 Dify completion-messages（一次性请求，仍走流式收响应）。

    返回完整的 answer 字符串。400 → 返回 STOP_SIGNAL（上层应该立刻停整批任务）；
    429 / 5xx / 网络错误 → 指数退避重试；重试耗尽返回空串。
    """
    if not api_key:
        raise RuntimeError("缺少 Dify API Key")

    payload = {
        "inputs": inputs,
        "query": query,
        "response_mode": "streaming",
        "user": user,
    }
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    url = f"{DIFY_API_BASE}/completion-messages"

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        for attempt in range(max_retries):
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code == 400:
                        body = await resp.aread()
                        snippet = body.decode("utf-8", errors="replace")[:300]
                        print(f"[dify] {log_prefix} 400 STOP_SIGNAL: {snippet}")
                        return STOP_SIGNAL
                    if resp.status_code == 429 or resp.status_code >= 500:
                        wait = min(2 ** attempt, 8)
                        print(
                            f"[dify] {log_prefix} {resp.status_code}; retry in {wait}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        body = await resp.aread()
                        snippet = body.decode("utf-8", errors="replace")[:200]
                        print(f"[dify] {log_prefix} {resp.status_code} give up: {snippet}")
                        return ""

                    full_ans = ""
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            chunk = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("event") in ("message", "agent_message"):
                            full_ans += chunk.get("answer") or chunk.get("text") or ""
                    return full_ans.strip()
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                wait = min(2 ** attempt, 8)
                print(f"[dify] {log_prefix} network err: {e}; retry in {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                if "client has been closed" in str(e):
                    return ""
                wait = min(2 ** attempt, 8)
                print(f"[dify] {log_prefix} exc: {e}; retry in {wait}s")
                await asyncio.sleep(wait)

    print(f"[dify] {log_prefix} retry exhausted")
    return ""
