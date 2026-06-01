import os
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

DIFY_API_BASE = os.getenv("DIFY_API_BASE", "https://api.dify.ai/v1").rstrip("/")
# 老变量保留为 dify.chat() 的默认 fallback；新流程下 main.py 都显式传 api_key，
# 这个 var 已经没有强约束，留空也行。
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")
# /调研分析 重构后用两个 chat 应用：
#   planner  → 读表头+样本，输出 plan JSON（哪些是画像/单选/多选/量表/开放题，怎么分 part，怎么交叉）
#   analyst  → 同一会话先按 part 写报告，conv_id 留下来给后续 QA 复用（节省 token）
DIFY_PLANNER_KEY = os.getenv("DIFY_PLANNER_KEY", "")
DIFY_ANALYST_KEY = os.getenv("DIFY_ANALYST_KEY", "")
# /反馈打标（completion-messages 一次性请求，分两个应用：探路者识列 + 干活者打标）
DIFY_LLM1_KEY = os.getenv("DIFY_LLM1_KEY", "")
DIFY_LLM2_KEY = os.getenv("DIFY_LLM2_KEY", "")

# 各业务对应的 Dify 应用。每个 app 的字段形态不同（analyze 拆 planner+analyst，
# tagging 是探路者+干活者），由对应的 _run_xxx 函数自己解读。
DIFY_APPS: dict[str, dict] = {
    "analyze": {
        "name": "调研分析",
        "planner_key": DIFY_PLANNER_KEY,  # 列分类规划
        "analyst_key": DIFY_ANALYST_KEY,  # 首轮写报告 + 后续 QA 共用同一应用同一 conv_id
    },
    "tagging": {
        "name": "反馈打标",
        "explorer_key": DIFY_LLM1_KEY,  # 探路者：识别表头中需要打标的列号
        "worker_key": DIFY_LLM2_KEY,    # 干活者：拿到一行数据 → 返回每列的标签 + 翻译
    },
}

# 用户输入的指令 → Dify 应用 id
COMMAND_TO_APP: dict[str, str] = {
    "/调研分析": "analyze",
    "/反馈打标": "tagging",
}

# /反馈打标 行级并发上限（避免打 Dify 太狠）
TAGGING_CONCURRENCY = int(os.getenv("TAGGING_CONCURRENCY", "5"))

def assert_ready() -> None:
    missing = [
        name
        for name, val in {
            "FEISHU_APP_ID": FEISHU_APP_ID,
            "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
            "DIFY_PLANNER_KEY": DIFY_PLANNER_KEY,
            "DIFY_ANALYST_KEY": DIFY_ANALYST_KEY,
            "DIFY_LLM1_KEY": DIFY_LLM1_KEY,
            "DIFY_LLM2_KEY": DIFY_LLM2_KEY,
        }.items()
        if not val
    ]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}（检查 .env）")
