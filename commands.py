from config import COMMAND_TO_APP

HELP_TEXT = (
    "用法：\n"
    "\n"
    "📊 /调研分析 <飞书表格链接>\n"
    "    读取问卷表格，生成调研报告（飞书文档形式回复）\n"
    "\n"
    "💬 直接发文字\n"
    "    延续上一条指令的对话上下文（用于追问）\n"
    "\n"
    "工具指令：\n"
    "/help   查看本帮助\n"
    "/reset  清除当前对话上下文\n"
    "/ping   测试连通"
)

LOCAL_COMMANDS = {"/help", "/reset", "/ping"}
APP_COMMANDS = set(COMMAND_TO_APP.keys())
ALL_COMMANDS = LOCAL_COMMANDS | APP_COMMANDS


def parse_command(text: str) -> tuple[str, str] | None:
    """识别指令。返回 (cmd, arg) 或 None。"""
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ALL_COMMANDS:
        return (cmd, arg)
    return None
