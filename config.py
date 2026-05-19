import os
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

DIFY_API_BASE = os.getenv("DIFY_API_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")

# 各业务对应的 Dify Chatbot。新增功能：1) 在 Dify 建新 Chatbot；
# 2) .env 加新的 KEY；3) 在 DIFY_APPS 加一项；4) 在 COMMAND_TO_APP 加映射。
DIFY_APPS: dict[str, dict] = {
    "analyze": {
        "name": "调研分析",
        "api_key": DIFY_API_KEY,
    },
    # 示例（未来扩展时取消注释）：
    # "code": {"name": "代码助手", "api_key": os.getenv("DIFY_API_KEY_CODE", "")},
    # "translate": {"name": "翻译助手", "api_key": os.getenv("DIFY_API_KEY_TRANSLATE", "")},
}

# 用户输入的指令 → Dify 应用 id
COMMAND_TO_APP: dict[str, str] = {
    "/调研分析": "analyze",
    # "/code": "code",
    # "/translate": "translate",
}

def assert_ready() -> None:
    missing = [
        name
        for name, val in {
            "FEISHU_APP_ID": FEISHU_APP_ID,
            "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
            "DIFY_API_KEY": DIFY_API_KEY,
        }.items()
        if not val
    ]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}（检查 .env）")
