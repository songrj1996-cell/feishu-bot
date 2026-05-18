import asyncio
import json
import time
import traceback
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request

from commands import APP_COMMANDS, HELP_TEXT, LOCAL_COMMANDS, parse_command
from config import (
    COMMAND_TO_APP,
    DIFY_APPS,
    FEISHU_ENCRYPT_KEY,
    FEISHU_VERIFICATION_TOKEN,
    PORT,
    assert_ready,
)
from dify import chat as dify_chat
from feishu import reply_markdown, reply_text
from feishu_docs import create_doc_from_markdown
from feishu_sheets import fetch_as_markdown, parse_feishu_link

assert_ready()

app = FastAPI()

# user_id -> {"last_app": str | None, "conversations": {app_id: conversation_id}}
user_state: dict[str, dict] = {}

# 飞书会重试，按 event_id 去重
processed_events: dict[str, float] = {}
DEDUPE_TTL = 300


def _decrypt_if_needed(body: dict) -> dict:
    if "encrypt" not in body:
        return body
    if not FEISHU_ENCRYPT_KEY:
        raise HTTPException(400, "收到加密载荷，但未配置 FEISHU_ENCRYPT_KEY")
    from crypto import AESCipher

    decrypted = AESCipher(FEISHU_ENCRYPT_KEY).decrypt(body["encrypt"])
    return json.loads(decrypted)


def _is_duplicate(event_id: str) -> bool:
    now = time.time()
    for k in list(processed_events.keys()):
        if processed_events[k] + DEDUPE_TTL < now:
            del processed_events[k]
    if event_id in processed_events:
        return True
    processed_events[event_id] = now
    return False


def _get_user_state(user_id: str) -> dict:
    return user_state.setdefault(
        user_id, {"last_app": None, "conversations": {}}
    )


def _set_user_app(user_id: str, app_id: str, conversation_id: str = "") -> None:
    state = _get_user_state(user_id)
    state["last_app"] = app_id
    if conversation_id:
        state["conversations"][app_id] = conversation_id


def _get_conv(user_id: str, app_id: str) -> str:
    return _get_user_state(user_id)["conversations"].get(app_id, "")


@app.get("/")
async def root() -> dict:
    return {"ok": True, "service": "feishu-dify-bot"}


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    raw = await request.json()
    body = _decrypt_if_needed(raw)

    if body.get("type") == "url_verification":
        if body.get("token") != FEISHU_VERIFICATION_TOKEN:
            raise HTTPException(401, "token 不匹配")
        return {"challenge": body.get("challenge")}

    header = body.get("header", {})
    if header.get("token") != FEISHU_VERIFICATION_TOKEN:
        raise HTTPException(401, "token 不匹配")

    event_id = header.get("event_id", "")
    if event_id and _is_duplicate(event_id):
        return {"code": 0}

    if header.get("event_type") == "im.message.receive_v1":
        try:
            await _handle_message(body.get("event", {}))
        except Exception:
            traceback.print_exc()

    return {"code": 0}


async def _handle_message(event: dict) -> None:
    message = event.get("message", {})
    sender = event.get("sender", {})

    if message.get("chat_type") != "p2p":
        return

    message_id = message.get("message_id", "")
    user_id = sender.get("sender_id", {}).get("open_id", "anonymous")

    if message.get("message_type") != "text":
        await reply_text(message_id, "目前仅支持文本消息（链接也是文本）。")
        return

    content = json.loads(message.get("content", "{}"))
    text = content.get("text", "").strip()
    if not text:
        return

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

    # 纯文字 → 走最近一次指令的上下文
    last_app = _get_user_state(user_id)["last_app"]
    if not last_app:
        await reply_text(
            message_id,
            "还没有进行中的对话。先用指令开始一个任务，例如：\n"
            "/调研分析 <表格链接>\n\n"
            "发送 /help 查看所有指令。",
        )
        return

    await reply_text(message_id, "正在思考...")
    asyncio.create_task(
        _continue_conversation(last_app, text, message_id, user_id)
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
            "📊 收到表格链接，正在读取数据并生成调研报告，预计 1-3 分钟。",
        )
        asyncio.create_task(_run_analyze(link, message_id, user_id))
        return

    # 未来扩展点：其他 app_id 在这里加 elif 分支
    await reply_text(message_id, f"指令 {cmd} 还没接入处理逻辑。")


async def _heartbeat(message_id: str, interval: int = 60) -> None:
    minutes = 0
    try:
        while True:
            await asyncio.sleep(interval)
            minutes += 1
            await reply_text(
                message_id, f"⏳ 还在生成中... 已等待 {minutes} 分钟"
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
        sheet_md = await fetch_as_markdown(link)
        if not sheet_md.strip():
            await reply_text(message_id, "表格读取后内容为空，请检查权限或内容。")
            return

        api_key = DIFY_APPS["analyze"]["api_key"]
        # 每次新表格 = 新会话，不污染上下文
        answer, new_conv_id = await _with_heartbeat(
            message_id,
            dify_chat(sheet_md, user_id, conversation_id="", api_key=api_key),
        )
        _set_user_app(user_id, "analyze", new_conv_id)

        if not answer.strip():
            await reply_text(message_id, "Dify 没有返回报告内容。")
            return

        title = f"调研报告 {datetime.now().strftime('%Y-%m-%d %H%M')}"
        try:
            doc_url = await create_doc_from_markdown(
                title, answer, share_with_open_id=user_id
            )
            await reply_text(message_id, f"📄 报告生成完毕：\n{doc_url}")
        except Exception as doc_err:
            traceback.print_exc()
            await reply_text(
                message_id,
                f"⚠️ 飞书文档创建失败（{doc_err}），降级用消息卡片返回报告：",
            )
            await reply_markdown(message_id, answer)
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"处理失败：{e}")


async def _continue_conversation(
    app_id: str, text: str, message_id: str, user_id: str
) -> None:
    try:
        api_key = DIFY_APPS[app_id]["api_key"]
        conv_id = _get_conv(user_id, app_id)
        answer, new_conv_id = await _with_heartbeat(
            message_id,
            dify_chat(text, user_id, conversation_id=conv_id, api_key=api_key),
        )
        if new_conv_id:
            _set_user_app(user_id, app_id, new_conv_id)
        await reply_markdown(message_id, answer or "（Dify 未返回内容）")
    except Exception as e:
        traceback.print_exc()
        await reply_text(message_id, f"调用 Dify 失败：{e}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
