"""调研分析的"分析方案"数据结构 + 解析 + 卡片渲染 + 用户意图判断。

planner Dify 应用输出一段 JSON（用 ```json 围栏包），描述这份问卷怎么分析：
- columns: 每列的角色（id / profile_dim / single_choice / multi_choice / scale / open_text / ignore）
- parts:   报告章节划分（每个 part 内同时含客观题和主观题，**不**按题型割裂）
- cross_tabs: 画像 × 题目的交叉分析建议
- open_questions: planner 自己拿不准、想问用户的事

bot 拿到 plan 后渲染成飞书卡片让用户确认。用户回 "OK" 进入计算；回别的 → 喂回 planner 出新 plan 再确认。
"""

from __future__ import annotations

import json
import re
from typing import Any

VALID_ROLES = {
    "id",
    "profile_dim",
    "single_choice",
    "multi_choice",
    "scale",
    "open_text",
    "ignore",
}


# ============================================================================
# Plan 解析
# ============================================================================


def parse_plan_from_llm(
    answer: str, header_count: int
) -> tuple[dict | None, str | None]:
    """从 LLM 回复抽 JSON plan。返回 (plan, error_msg)。

    LLM 输出会有各种"脏"格式：```json 围栏、单引号、尾随逗号、夹杂解释文字等。
    顺序：抽块 → sanitize → json.loads → schema 校验。任一失败返回 (None, 简短原因)。
    """
    raw = _extract_json_block(answer)
    if raw is None:
        return None, "no JSON block found in LLM output"

    cleaned = _sanitize_json(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return None, f"json decode failed: {e}"

    if not isinstance(data, dict):
        return None, "top-level JSON is not an object"

    err = _validate_plan(data, header_count)
    if err:
        return None, err
    return data, None


def _extract_json_block(text: str) -> str | None:
    """优先抓 ```json...``` 围栏；没有就抓首个平衡的 {...}。"""
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.DOTALL)
    if fence:
        return fence.group(1).strip()
    # 退化：扫第一个 { ，按花括号深度找匹配
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _sanitize_json(s: str) -> str:
    """处理 LLM 常见的"脏"输出：
    1. 去除 // 行注释（"" 内除外）
    2. 去除 /* ... */ 块注释
    3. 移除 array / object 末尾多余的逗号 `,]` `,}`
    4. 不替换单引号——容易误伤字符串内容；让 json.loads 抛错由上层重试更安全。
    """
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    # 行注释：避免把字符串里的 // 也吃掉，简单做法是逐行扫
    out_lines = []
    for line in s.splitlines():
        in_str = False
        escape = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    s = "\n".join(out_lines)
    # 末尾逗号
    s = re.sub(r",(\s*[\]\}])", r"\1", s)
    return s


def _validate_plan(data: dict, header_count: int) -> str | None:
    """schema 校验。返回 None=通过，否则返回错误描述。"""
    cols = data.get("columns")
    if not isinstance(cols, list) or not cols:
        return "columns missing or empty"

    seen_indexes: set[int] = set()
    for c in cols:
        if not isinstance(c, dict):
            return "columns contains non-object element"
        idx = c.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= header_count:
            return f"column index out of range: {idx}"
        if idx in seen_indexes:
            return f"duplicate column index: {idx}"
        seen_indexes.add(idx)
        role = c.get("role")
        if role not in VALID_ROLES:
            return f"invalid role: {role}"
        if role == "multi_choice" and not c.get("delimiter"):
            # 允许 LLM 不给 delimiter，下游会启发式识别；不报错
            pass
        if role == "scale":
            if not isinstance(c.get("min"), (int, float)):
                return f"scale column {idx} missing min"
            if not isinstance(c.get("max"), (int, float)):
                return f"scale column {idx} missing max"
        # value_aliases 是可选字段；如果给了，结构必须是 {canonical: [aliases...]}
        aliases = c.get("value_aliases")
        if aliases is not None:
            if not isinstance(aliases, dict):
                return f"column {idx} value_aliases must be dict"
            for canon, lst in aliases.items():
                if not isinstance(canon, str):
                    return f"column {idx} value_aliases canonical must be string"
                if not isinstance(lst, list) or any(
                    not isinstance(a, str) for a in lst
                ):
                    return f"column {idx} value_aliases[{canon}] must be list of strings"

    parts = data.get("parts")
    if not isinstance(parts, list) or not parts:
        return "parts missing or empty"
    part_indexes_seen: set[int] = set()
    # id / ignore 不参与章节统计，允许不在任何 part 里；其他角色必须恰好归到一个 part
    must_be_in_part = {
        c["index"] for c in cols if c["role"] not in ("id", "ignore")
    }
    for p in parts:
        if not isinstance(p, dict):
            return "parts contains non-object element"
        if not isinstance(p.get("name"), str) or not p["name"].strip():
            return "part name missing"
        ci = p.get("column_indexes")
        if not isinstance(ci, list):
            return f"part {p.get('name')!r} missing column_indexes"
        for idx in ci:
            if idx not in seen_indexes:
                return f"part references unknown column index: {idx}"
            if idx in part_indexes_seen:
                return f"column index {idx} appears in multiple parts"
            part_indexes_seen.add(idx)
    # 必须分章节的列（即 profile_dim / single_choice / multi_choice / scale / open_text）
    # 必须恰好出现在某个 part 里
    missing = must_be_in_part - part_indexes_seen
    if missing:
        return f"stats columns not in any part: {sorted(missing)}"
    extra = part_indexes_seen - seen_indexes
    if extra:
        return f"part references columns not in columns list: {sorted(extra)}"

    cross_tabs = data.get("cross_tabs", [])
    if cross_tabs and not isinstance(cross_tabs, list):
        return "cross_tabs must be list"
    profile_dims = {c["index"] for c in cols if c["role"] == "profile_dim"}
    for ct in cross_tabs:
        if not isinstance(ct, dict):
            return "cross_tabs contains non-object"
        pi = ct.get("profile_index")
        qi = ct.get("question_index")
        if pi not in profile_dims:
            return f"cross_tab.profile_index {pi} is not a profile_dim"
        if qi not in seen_indexes or qi == pi:
            return f"cross_tab.question_index {qi} invalid"

    open_qs = data.get("open_questions", [])
    if open_qs and not isinstance(open_qs, list):
        return "open_questions must be list"

    return None


# ============================================================================
# Plan 渲染（飞书卡片）
# ============================================================================


_CHINESE_ORDINALS = {
    1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
    6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
    11: "十一", 12: "十二", 13: "十三", 14: "十四", 15: "十五",
}


_ROLE_LABELS: dict[str, tuple[str, str]] = {
    "id": ("🆔", "用户ID（不参与统计）"),
    "profile_dim": ("📊", "画像维度"),
    "single_choice": ("✅", "单选题"),
    "multi_choice": ("☑️", "多选题"),
    "scale": ("🔢", "量表题"),
    "open_text": ("💬", "开放题"),
    "ignore": ("⏸️", "忽略"),
}


def render_plan_for_user(plan: dict, headers: list[str]) -> str:
    """渲染 plan 成飞书卡片 markdown。"""
    cols_by_role: dict[str, list[dict]] = {}
    for c in plan["columns"]:
        cols_by_role.setdefault(c["role"], []).append(c)

    lines: list[str] = ["📋 **我的分析方案，请你确认**", ""]

    # 报告结构
    lines.append("📑 **报告结构**")
    for i, p in enumerate(plan["parts"], 1):
        if i > 1:
            lines.append("")  # 部分之间留空行做视觉分隔
        ordinal = _CHINESE_ORDINALS.get(i, str(i))
        lines.append(f"**第{ordinal}部分：{p['name']}**")
        for j, idx in enumerate(p["column_indexes"], 1):
            col = find_column(plan, idx) or {}
            name = col.get("name") or _short_name(headers, idx)
            # 用 `1、` 而非 `1.`：飞书 MD 会把 `1.` 解析成有序列表并吞掉数字
            lines.append(f"{j}、{name}")
    lines.append("")

    # 列分类
    lines.append("🔖 **列分类**")
    for role in ("id", "profile_dim", "single_choice", "multi_choice", "scale", "open_text", "ignore"):
        cols = cols_by_role.get(role)
        if not cols:
            continue
        emoji, label = _ROLE_LABELS[role]
        lines.append(f"  {emoji} **{label}**")
        for c in cols:
            name = c.get("name") or _short_name(headers, c["index"])
            extra = ""
            if role == "multi_choice" and c.get("delimiter"):
                extra = f"（分隔符: `{c['delimiter']}`）"
            elif role == "scale":
                extra = f"（{c.get('min')}–{c.get('max')}）"
            lines.append(f"    · {name}{extra}")
            # 每列下方展示同义合并（如果有）
            aliases = c.get("value_aliases") or {}
            for canonical, alias_list in aliases.items():
                others = [a for a in alias_list if a.strip() != canonical.strip()]
                if not others:
                    continue
                # 限制长度，避免卡片过长
                preview = others[:5]
                more = f" +{len(others) - 5}" if len(others) > 5 else ""
                lines.append(
                    f"      ↪ 「{canonical}」 ← {', '.join(preview)}{more}"
                )
    lines.append("")

    # 交叉分析
    cross = plan.get("cross_tabs") or []
    if cross:
        lines.append("🔀 **交叉分析**")
        for ct in cross:
            p_col = find_column(plan, ct["profile_index"]) or {}
            q_col = find_column(plan, ct["question_index"]) or {}
            p_name = p_col.get("name") or _short_name(headers, ct["profile_index"])
            q_name = q_col.get("name") or _short_name(headers, ct["question_index"])
            lines.append(f"  · {p_name} × {q_name}")
        lines.append("")
    else:
        lines.append("🔀 **交叉分析**：（无）")
        lines.append("")

    # planner 提的疑问
    open_qs = plan.get("open_questions") or []
    if open_qs:
        lines.append("❓ **我还有几个不确定的地方想问你**")
        for i, q in enumerate(open_qs, 1):
            lines.append(f"  {i}. {q}")
        lines.append("")

    # 信心较低的列
    low_conf = [
        c for c in plan["columns"]
        if c.get("confidence") and c["confidence"].lower() in ("low", "medium")
    ]
    if low_conf:
        lines.append("⚠️ **信心较低的列**（请你确认我猜对没）")
        for c in low_conf:
            name = c.get("name") or _short_name(headers, c["index"])
            emoji, label = _ROLE_LABELS.get(c["role"], ("", c["role"]))
            lines.append(f"  · {name} → {emoji} {label}（信心: {c['confidence']}）")
        lines.append("")

    # 操作提示
    lines.append("---")
    lines.append("👉 确认无误请回复 **OK**。")
    lines.append("👉 否则请直接告诉我要改什么（例：'年龄段是画像不是单选；多选题分隔符是 ；'）。")
    return "\n".join(lines)


def _short_name(headers: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(headers):
        return f"列{idx}"
    name = (headers[idx] or "").strip().replace("\n", " ")
    if not name:
        return f"列{idx}"
    return name if len(name) <= 20 else name[:18] + "…"


# ============================================================================
# 用户意图：是确认还是修订
# ============================================================================


_APPROVAL_TOKENS = {
    "ok",
    "OK",
    "Ok",
    "oK",
    "确认",
    "确定",
    "好的",
    "好",
    "可以",
    "开始",
    "go",
    "Go",
    "yes",
    "Yes",
    "是",
    "对",
    "嗯",
    "行",
    "确认无误",
    "无误",
}


def is_user_approval(text: str) -> bool:
    """判断短消息是否表示确认。

    规则：8 字以内（去除标点和空白后）且整体在 APPROVAL_TOKENS 集合里。
    长文本一律视为修订意见——避免"OK 但是年龄段错了"被当成 OK。
    """
    if not text:
        return False
    stripped = re.sub(r"[\s.,!?。！？，、]+", "", text)
    if len(stripped) > 8:
        return False
    return stripped in _APPROVAL_TOKENS


# ============================================================================
# 工具函数：从 plan 推一些信息
# ============================================================================


def header_count_from_plan(plan: dict) -> int:
    """从 plan 推断原表头列数（用于 parse 重试时校验）。"""
    cols = plan.get("columns") or []
    if not cols:
        return 0
    return max(int(c["index"]) for c in cols) + 1


def find_column(plan: dict, idx: int) -> dict | None:
    for c in plan.get("columns") or []:
        if c.get("index") == idx:
            return c
    return None


# ============================================================================
# 同义合并增强（C 路线）：扫全表 unique 值 → LLM 反查同义 → 写回 plan
# ============================================================================


def parse_aliases_json(
    answer: str,
) -> tuple[dict[str, dict[str, list[str]]] | None, str | None]:
    """从 LLM 回复抽 alias 映射。返回 ({col_idx_str: {canonical: [aliases]}}, error_msg)。

    LLM 输出格式约定：
    ```json
    {
      "<col_index>": {
        "<canonical>": ["alias1", "alias2", ...]
      },
      ...
    }
    ```
    没有同义可合并的列对应值是 `{}`（也允许整个 col 不出现）。
    """
    raw = _extract_json_block(answer)
    if raw is None:
        return None, "no JSON block in alias enrichment output"
    cleaned = _sanitize_json(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return None, f"json decode failed: {e}"
    if not isinstance(data, dict):
        return None, "top-level not object"

    out: dict[str, dict[str, list[str]]] = {}
    for col_key, mapping in data.items():
        if not isinstance(mapping, dict):
            continue
        clean_mapping: dict[str, list[str]] = {}
        for canon, lst in mapping.items():
            if not isinstance(canon, str):
                continue
            if not isinstance(lst, list):
                continue
            aliases = [a for a in lst if isinstance(a, str)]
            if aliases:
                clean_mapping[canon.strip()] = [a.strip() for a in aliases]
        out[str(col_key)] = clean_mapping  # 允许空 dict，表示该列无同义
    return out, None


def apply_aliases_to_plan(
    plan: dict, aliases_data: dict[str, dict[str, list[str]]]
) -> dict:
    """把 alias 映射写回 plan["columns"][i]["value_aliases"]。原地修改并返回 plan。"""
    for c in plan.get("columns") or []:
        idx_key = str(c.get("index"))
        if idx_key in aliases_data:
            mapping = aliases_data[idx_key]
            if mapping:
                c["value_aliases"] = mapping
            else:
                # 显式空 dict → 清掉旧 aliases（如果有）
                c.pop("value_aliases", None)
    return plan
