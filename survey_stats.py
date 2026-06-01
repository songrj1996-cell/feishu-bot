"""调研问卷的"确定性统计"——给定 rows + plan，纯 stdlib 算频数/占比/量表分布/交叉表。

设计要点：
- 输入：rows（rows[0]=表头，rows[1:]=数据）+ plan（survey_plan 模块定义）
- 输出：
  · stats_markdown：
      1. 顶部 <metadata>
      2. ## 画像维度概览（每个画像维度的样本分布，LLM 直接引用避免数字漂移）
      3. ## Part X 章节，每个客观题（single/multi/scale）下面同时含：
         - 总体频数/占比表
         - 该题 × 每个画像维度的交叉表（行=画像取值，列=各选项的频数+占比）
         - profile_dim 列在 part 里只标注"已在画像概览展示"
      4. open_text 题在 part 里只标"开放题，N 条非空回答 → 见 <open_text> 块"
  · open_text_by_col：{col_index: [{"ids":{...}, "profile":{...}, "text":...}, ...]}
      每条原文绑定该用户的所有 id 列值 + 画像维度值，让 LLM 能按观点聚合 + 引用原话时附 ID 和画像

为什么交叉表内嵌每道题：
  用户要求"每道选择题/打分题再额外给一组按画像分组的对比表"——是固定要求不是可选项。
  Python 自动给所有"客观题 × 画像维度"组合算交叉表，比 LLM 挑选更可靠。
  plan["cross_tabs"] 字段保留兼容但已不被使用。

为什么开放题原文带 ID + 画像：
  用户要求每个观点列出"持此观点 12 人中王者 8 人、星耀 3 人..."，引用原话时附玩家 ID + 画像。
  原文只有 text 时 LLM 没法追溯来自哪个用户什么画像，所以把每条原文连同该行所有 id 列值
  + 所有画像列值一起打包传给 LLM。

为什么支持 value_aliases：
  问卷可能多语言（中文/英文/西语等），同义选项（"王者" / "Mythic" / "Mítica"）原始字符串不同，
  直接频数统计会被算成 3 个独立选项。planner 在 plan JSON 里给出 value_aliases 映射，
  Python 统计时按 canonical 聚合，保证占比正确。

数字漂移防御：分母明确写 "N 份回答中" 而不是 "样本"，writer prompt 强调不重新算。
"""

from __future__ import annotations

import re as _re
import statistics
from collections import Counter
from typing import Any, Callable


# ============================================================================
# 公开入口
# ============================================================================


def compute(
    rows: list[list], plan: dict
) -> tuple[str, dict[int, list[dict]]]:
    """主入口：算出 stats markdown + 开放题原文池。

    rows[0] 是表头，rows[1:] 是数据。
    open_text 返回结构：{col_index: [{"ids":{...}, "profile":{...}, "text":...}, ...]}
    """
    if not rows:
        return "<metadata>总样本: 0</metadata>\n\n（表格为空）", {}

    headers = rows[0]
    body = rows[1:]
    total = len(body)

    cols_by_index: dict[int, dict] = {c["index"]: c for c in plan["columns"]}
    profile_cols = [c for c in plan["columns"] if c["role"] == "profile_dim"]
    id_cols = [c for c in plan["columns"] if c["role"] == "id"]

    md_parts: list[str] = []

    # 顶部 metadata
    blank_count = sum(1 for r in body if all(_is_blank(c) for c in r))
    valid = total - blank_count
    md_parts.append(
        f"<metadata>总样本: {total}, 有效样本: {valid}"
        + (f", 全空被排除: {blank_count} 条" if blank_count else "")
        + "</metadata>"
    )
    md_parts.append("")

    # 画像维度概览（让 LLM 在报告开头直接引用）
    if profile_cols:
        md_parts.append("## 画像维度概览")
        md_parts.append("")
        for p_col in profile_cols:
            section = _render_profile_overview(p_col, headers, body)
            md_parts.append(section)
            md_parts.append("")

    # 按 part 分组渲染
    for i, part in enumerate(plan["parts"], 1):
        md_parts.append(f"## Part {i} {part['name']}")
        md_parts.append("")
        for col_idx in part["column_indexes"]:
            col = cols_by_index.get(col_idx)
            if not col:
                continue
            section = _render_column(col, headers, body, profile_cols)
            if section:
                md_parts.append(section)
                md_parts.append("")

    # 开放题数据池：每条原文带 ids + profile
    open_text: dict[int, list[dict]] = {}
    for c in plan["columns"]:
        if c["role"] == "open_text":
            open_text[c["index"]] = _collect_open_text(
                c["index"], body, headers, id_cols, profile_cols
            )

    return "\n".join(md_parts).rstrip() + "\n", open_text


# ============================================================================
# 画像维度概览
# ============================================================================


def _render_profile_overview(p_col: dict, headers: list[str], body: list[list]) -> str:
    name = p_col.get("name") or _safe_header(headers, p_col["index"])
    aliases = p_col.get("value_aliases")
    norm = _make_normalizer(aliases)
    raw = _column_values(body, p_col["index"])
    nonblank = [norm(v) for v in raw if v.strip()]
    if not nonblank:
        return f"### {name}\n\n（该画像维度无有效数据）"
    counts = Counter(nonblank)
    total = sum(counts.values())
    lines = [f"### {name}"]
    lines.append("")
    lines.append("| 取值 | 频数 | 占比 |")
    lines.append("|---|---|---|")
    for v, n in counts.most_common():
        lines.append(f"| {_md_escape(v)} | {n} | {_pct(n, total)} |")
    lines.append(f"\n（共 {total} 份非空回答）")
    return "\n".join(lines)


# ============================================================================
# 单列渲染（part 内每道题）
# ============================================================================


def _render_column(
    col: dict, headers: list[str], body: list[list], profile_cols: list[dict]
) -> str:
    role = col["role"]
    idx = col["index"]
    name = col.get("name") or _safe_header(headers, idx)
    title = f"### {name}"

    if role in ("id", "ignore"):
        return f"{title}\n\n（{'用户ID' if role == 'id' else '已忽略'}列，不参与统计）"

    if role == "profile_dim":
        return f"{title}\n\n（画像维度，分布表见上方「画像维度概览」章节）"

    raw_values = _column_values(body, idx)
    nonblank_raw = [v for v in raw_values if v.strip()]
    if not nonblank_raw:
        return f"{title}\n\n（该列无有效数据）"

    aliases = col.get("value_aliases")
    norm = _make_normalizer(aliases)

    if role == "single_choice":
        nonblank_norm = [norm(v) for v in nonblank_raw]
        body_md = _render_single_choice(nonblank_norm)
        body_md += _append_cross_tabs(
            col, raw_values, profile_cols, body, headers, single=True
        )
    elif role == "multi_choice":
        delimiter = col.get("delimiter") or _guess_delimiter(nonblank_raw)
        body_md = _render_multi_choice(nonblank_raw, delimiter, norm)
        body_md += _append_cross_tabs(
            col, raw_values, profile_cols, body, headers,
            single=False, delimiter=delimiter,
        )
    elif role == "scale":
        lo = col.get("min")
        hi = col.get("max")
        body_md = _render_scale(nonblank_raw, lo, hi)
        body_md += _append_cross_tabs_scale(
            col, raw_values, profile_cols, body, headers,
        )
    elif role == "open_text":
        body_md = (
            f"（开放题，{len(nonblank_raw)} 条非空回答 → "
            f"见 `<open_text>` 块，每条带玩家 ID 和画像）"
        )
    else:
        body_md = "（未识别的题型）"

    return f"{title}\n\n{body_md}"


def _append_cross_tabs(
    col: dict,
    q_raw: list[str],
    profile_cols: list[dict],
    body: list[list],
    headers: list[str],
    *,
    single: bool,
    delimiter: str = ",",
) -> str:
    """单选/多选题：跟每个 profile_dim 配交叉表。"""
    if not profile_cols:
        return ""
    out = []
    q_norm_fn = _make_normalizer(col.get("value_aliases"))
    for p_col in profile_cols:
        if p_col["index"] == col["index"]:
            continue  # 跟自己交叉没意义
        p_raw = _column_values(body, p_col["index"])
        p_name = p_col.get("name") or _safe_header(headers, p_col["index"])
        p_norm_fn = _make_normalizer(p_col.get("value_aliases"))
        ct_md = _cross_tab_categorical(
            p_raw, q_raw, p_norm=p_norm_fn, q_norm=q_norm_fn,
            single=single, delimiter=delimiter,
        )
        if ct_md:
            out.append(f"\n\n**按「{p_name}」分组**\n\n{ct_md}")
    return "".join(out)


def _append_cross_tabs_scale(
    col: dict,
    q_raw: list[str],
    profile_cols: list[dict],
    body: list[list],
    headers: list[str],
) -> str:
    if not profile_cols:
        return ""
    out = []
    for p_col in profile_cols:
        p_raw = _column_values(body, p_col["index"])
        p_name = p_col.get("name") or _safe_header(headers, p_col["index"])
        p_norm = _make_normalizer(p_col.get("value_aliases"))
        ct_md = _cross_tab_scale(p_raw, q_raw, p_norm=p_norm)
        if ct_md:
            out.append(f"\n\n**按「{p_name}」分组（量表均值对比）**\n\n{ct_md}")
    return "".join(out)


def _render_single_choice(values: list[str]) -> str:
    counts = Counter(values)
    total = sum(counts.values())
    rows: list[str] = []
    rows.append("总体分布：")
    rows.append("")
    rows.append("| 选项 | 频数 | 占比 |")
    rows.append("|---|---|---|")
    for val, n in counts.most_common():
        rows.append(f"| {_md_escape(val)} | {n} | {_pct(n, total)} |")
    rows.append(f"\n（共 {total} 份非空回答）")
    return "\n".join(rows)


def _render_multi_choice(
    values: list[str], delimiter: str, norm: Callable[[str], str]
) -> str:
    counts: Counter = Counter()
    for v in values:
        opts = {norm(x.strip()) for x in v.split(delimiter) if x.strip()}
        for opt in opts:
            counts[opt] += 1
    total_responders = len(values)
    rows: list[str] = []
    rows.append(
        f"总体分布（多选题，分隔符: `{delimiter}`，分母 = {total_responders} 份非空回答）："
    )
    rows.append("")
    rows.append("| 选项 | 选择人数 | 占比 |")
    rows.append("|---|---|---|")
    for val, n in counts.most_common():
        rows.append(f"| {_md_escape(val)} | {n} | {_pct(n, total_responders)} |")
    return "\n".join(rows)


def _render_scale(values: list[str], lo: Any, hi: Any) -> str:
    nums: list[float] = []
    invalid = 0
    for v in values:
        try:
            nums.append(float(v.strip()))
        except (ValueError, TypeError):
            invalid += 1

    if not nums:
        return "（量表题：所有回答均不能转为数字）"

    mean = statistics.mean(nums)
    median = statistics.median(nums)
    stdev = statistics.pstdev(nums) if len(nums) > 1 else 0.0

    rows: list[str] = []
    rows.append(
        f"- 均值: **{mean:.2f}**, 中位数: {median:g}, 标准差: {stdev:.2f}"
    )
    rows.append(f"- 有效数字回答: {len(nums)} 条")
    if invalid:
        rows.append(f"- 非数字回答（已剔除均值计算）: {invalid} 条")

    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        lo_i = int(lo)
        hi_i = int(hi)
        if hi_i - lo_i <= 12 and hi_i > lo_i:
            counts: Counter = Counter()
            for n in nums:
                counts[round(n)] += 1
            rows.append("")
            rows.append("分布：")
            rows.append("| 取值 | 频数 | 占比 |")
            rows.append("|---|---|---|")
            for v in range(lo_i, hi_i + 1):
                c = counts.get(v, 0)
                rows.append(f"| {v} | {c} | {_pct(c, len(nums))} |")
        else:
            rows.append(_histogram_bins(nums, lo_i, hi_i))
    return "\n".join(rows)


def _histogram_bins(nums: list[float], lo: int, hi: int, bins: int = 5) -> str:
    if hi <= lo:
        return ""
    width = (hi - lo) / bins
    counts = [0] * bins
    for n in nums:
        if n < lo:
            counts[0] += 1
            continue
        if n >= hi:
            counts[-1] += 1
            continue
        b = min(int((n - lo) / width), bins - 1)
        counts[b] += 1
    lines = ["", "分布：", "| 区间 | 频数 | 占比 |", "|---|---|---|"]
    for i, c in enumerate(counts):
        a = lo + i * width
        b = a + width
        lines.append(f"| {a:g}–{b:g} | {c} | {_pct(c, len(nums))} |")
    return "\n".join(lines)


# ============================================================================
# 交叉表
# ============================================================================


def _cross_tab_categorical(
    p_raw: list[str],
    q_raw: list[str],
    *,
    p_norm: Callable[[str], str],
    q_norm: Callable[[str], str],
    single: bool,
    delimiter: str = ",",
) -> str:
    """画像 × 类目题。每格 'n (xx%)'，每格 < 5 加 *。

    占比分母 = 该 profile 取值下的回答人数（按行）。
    多选题：行内集合化避免重复同选项。
    """
    pairs = list(zip(p_raw, q_raw))
    pairs = [(p, q) for p, q in pairs if p.strip() and q.strip()]
    if not pairs:
        return "（无有效配对数据）"

    # 应用 normalizer
    pairs_norm = [(p_norm(p), q) for p, q in pairs]

    p_options = list(dict.fromkeys(p for p, _ in pairs_norm))
    if single:
        q_options = list(dict.fromkeys(q_norm(q) for _, q in pairs_norm))
    else:
        q_set: list[str] = []
        seen: set[str] = set()
        for _, q in pairs_norm:
            for o in (x.strip() for x in q.split(delimiter)):
                if not o:
                    continue
                normo = q_norm(o)
                if normo not in seen:
                    seen.add(normo)
                    q_set.append(normo)
        q_options = q_set

    p_totals: Counter = Counter()
    for p, _ in pairs_norm:
        p_totals[p] += 1

    grid: dict[tuple[str, str], int] = {}
    for p, q in pairs_norm:
        if single:
            qn = q_norm(q)
            grid[(p, qn)] = grid.get((p, qn), 0) + 1
        else:
            opts = {q_norm(x.strip()) for x in q.split(delimiter) if x.strip()}
            for o in opts:
                grid[(p, o)] = grid.get((p, o), 0) + 1

    has_low = False
    lines: list[str] = []
    header_cells = [""] + [_md_escape(o) for o in q_options] + ["该画像总计"]
    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("|" + "|".join(["---"] * len(header_cells)) + "|")
    # 按 profile 总计降序
    for p in sorted(p_options, key=lambda x: -p_totals[x]):
        row_cells = [_md_escape(p)]
        denom = p_totals[p]
        for o in q_options:
            n = grid.get((p, o), 0)
            star = "*" if 0 < n < 5 else ""
            if 0 < n < 5:
                has_low = True
            row_cells.append(f"{n}{star} ({_pct(n, denom)})")
        row_cells.append(str(denom))
        lines.append("| " + " | ".join(row_cells) + " |")

    if has_low:
        lines.append("")
        lines.append("> `*` 该格样本量 < 5，谨慎解读")
    if not single:
        lines.append("")
        lines.append(
            f"> 多选题：分母 = 该画像取值下的**回答人数**，不是选项次数；分隔符 `{delimiter}`"
        )
    return "\n".join(lines)


def _cross_tab_scale(
    p_raw: list[str], q_raw: list[str], *, p_norm: Callable[[str], str]
) -> str:
    pairs = []
    invalid = 0
    for p, q in zip(p_raw, q_raw):
        if not p.strip() or not q.strip():
            continue
        try:
            pairs.append((p_norm(p.strip()), float(q.strip())))
        except (ValueError, TypeError):
            invalid += 1

    if not pairs:
        return "（无有效配对数据）"

    by_profile: dict[str, list[float]] = {}
    for p, q in pairs:
        by_profile.setdefault(p, []).append(q)

    lines: list[str] = []
    lines.append("| 画像取值 | 样本量 | 均值 | 中位数 | 标准差 |")
    lines.append("|---|---|---|---|---|")
    has_low = False
    for p, nums in sorted(by_profile.items(), key=lambda kv: -len(kv[1])):
        n = len(nums)
        star = "*" if n < 5 else ""
        if n < 5:
            has_low = True
        mean = statistics.mean(nums)
        median = statistics.median(nums)
        stdev = statistics.pstdev(nums) if n > 1 else 0.0
        lines.append(
            f"| {_md_escape(p)} | {n}{star} | {mean:.2f} | {median:g} | {stdev:.2f} |"
        )
    if has_low:
        lines.append("")
        lines.append("> `*` 该画像取值的样本量 < 5，均值不稳定，谨慎解读")
    if invalid:
        lines.append(f"> 另有 {invalid} 条非数字回答未参与计算")
    return "\n".join(lines)


# ============================================================================
# 开放题原文池：每条带 ids + profile
# ============================================================================


def _collect_open_text(
    col_idx: int,
    body: list[list],
    headers: list[str],
    id_cols: list[dict],
    profile_cols: list[dict],
) -> list[dict]:
    """收集某开放题的所有非空原文，每条附该行的所有 id 列值 + 画像列值。

    LLM 拿到这种结构后能直接做：
      - 按观点聚合时统计画像分布（"持此观点 12 人中王者 8 人..."）
      - 引用原话时附玩家 ID + 画像（"mlbbid:xxx (王者/中国): ..."）
    """
    out: list[dict] = []
    for row in body:
        text = _format_cell(row[col_idx]) if col_idx < len(row) else ""
        if not text.strip():
            continue

        ids: dict[str, str] = {}
        for c in id_cols:
            i = c["index"]
            v = _format_cell(row[i]) if i < len(row) else ""
            if v.strip():
                key = c.get("name") or _safe_header(headers, i)
                ids[key] = v.strip()

        profile: dict[str, str] = {}
        for c in profile_cols:
            i = c["index"]
            v = _format_cell(row[i]) if i < len(row) else ""
            if v.strip():
                key = c.get("name") or _safe_header(headers, i)
                norm = _make_normalizer(c.get("value_aliases"))
                profile[key] = norm(v.strip())

        out.append({
            "ids": ids,
            "profile": profile,
            "text": text.strip(),
        })
    return out


# ============================================================================
# 工具函数
# ============================================================================


def _make_normalizer(aliases: dict | None) -> Callable[[str], str]:
    """根据 value_aliases（{canonical: [aliases...]}）构造一个 value→canonical 的映射函数。

    比对用 strip + casefold（覆盖中英文混合大小写差异）。canonical 自身也算一个别名。
    没有 aliases 时返回 identity（去首尾空白）。
    """
    if not aliases or not isinstance(aliases, dict):
        return lambda v: v.strip() if isinstance(v, str) else v

    table: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        if not isinstance(canonical, str):
            continue
        table[canonical.strip().casefold()] = canonical
        if isinstance(alias_list, list):
            for a in alias_list:
                if isinstance(a, str):
                    table[a.strip().casefold()] = canonical

    def norm(v: str) -> str:
        if not isinstance(v, str):
            return v
        key = v.strip().casefold()
        return table.get(key, v.strip())

    return norm


def _column_values(body: list[list], idx: int) -> list[str]:
    out: list[str] = []
    for row in body:
        if idx < len(row):
            out.append(_format_cell(row[idx]))
        else:
            out.append("")
    return out


def _format_cell(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, list):
        parts = []
        for item in cell:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(cell)


def _is_blank(cell: Any) -> bool:
    if cell is None or cell == "":
        return True
    if isinstance(cell, list) and not cell:
        return True
    if isinstance(cell, str) and not cell.strip():
        return True
    return False


def _md_escape(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ")


def _pct(n: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{n * 100 / total:.1f}%"


_DELIMITER_CANDIDATES = [",", "，", ";", "；", "、", "/", "|"]


def _guess_delimiter(values: list[str]) -> str:
    sample = values[:20]
    counts: dict[str, int] = {d: 0 for d in _DELIMITER_CANDIDATES}
    for v in sample:
        for d in _DELIMITER_CANDIDATES:
            counts[d] += v.count(d)
    best = max(counts.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return ","
    return best[0]


def _safe_header(headers: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(headers):
        return f"列{idx}"
    h = (headers[idx] or "").strip()
    return h or f"列{idx}"


# ============================================================================
# 数字漂移检查（writer 输出后置告警用）
# ============================================================================


_NUMBER_RE = _re.compile(r"\d+(?:\.\d+)?%?")


def find_numbers_not_in_stats(report_md: str, stats_md: str) -> list[str]:
    """从 report_md 抽出所有数字 token（含 %），返回不在 stats_md 里的那些。

    给 main.py 用作"writer 是否乱编数字"的后置告警，**不阻断流程**，只打日志。
    """
    in_stats = set(_NUMBER_RE.findall(stats_md))
    drifted: list[str] = []
    for tok in _NUMBER_RE.finditer(report_md):
        s = tok.group(0)
        if s not in in_stats:
            if len(s) <= 1:
                continue
            drifted.append(s)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in drifted:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq
