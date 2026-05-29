"""survey_stats 的纯函数测试——不联网，覆盖统计/解析/卡片渲染的关键边角。

跑法：
    cd C:\\Users\\admin\\Desktop\\feishu-bot
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_survey_stats -v

在 Linux 服务器上：
    cd ~/feishu-bot
    .venv/bin/python -m unittest tests.test_survey_stats -v
"""

from __future__ import annotations

import os
import sys
import unittest

# 把仓库根目录加到 sys.path，方便 `python -m unittest tests.test_xxx` 跑
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import survey_plan  # noqa: E402
import survey_stats  # noqa: E402


# ============================================================================
# survey_stats.compute（核心）
# ============================================================================


class TestCompute(unittest.TestCase):
    def _make_plan(self, **overrides):
        plan = {
            "columns": [
                {"index": 0, "name": "ID", "role": "id"},
                {"index": 1, "name": "国家", "role": "profile_dim"},
                {"index": 2, "name": "推荐(1-10)", "role": "scale", "min": 1, "max": 10},
                {"index": 3, "name": "功能", "role": "multi_choice", "delimiter": ","},
                {"index": 4, "name": "建议", "role": "open_text"},
            ],
            "parts": [
                {"name": "基本信息", "column_indexes": [0, 1]},
                {"name": "体验", "column_indexes": [2, 3]},
                {"name": "建议", "column_indexes": [4]},
            ],
            "cross_tabs": [],
            "open_questions": [],
        }
        plan.update(overrides)
        return plan

    def test_single_choice_frequencies(self):
        rows = [
            ["ID", "国家", "推荐(1-10)", "功能", "建议"],
            ["u1", "中国", "8", "A,B", "好用"],
            ["u2", "美国", "5", "A", ""],
            ["u3", "中国", "9", "B,C", "卡顿"],
            ["u4", "中国", "7", "A,B,C", "不错"],
        ]
        md, open_text = survey_stats.compute(rows, self._make_plan())
        # profile_dim 单选频数
        self.assertIn("中国", md)
        self.assertIn("| 中国 | 3 |", md)  # 3/4 = 75.0%
        self.assertIn("75.0%", md)
        # 美国 1 条
        self.assertIn("| 美国 | 1 |", md)

    def test_multi_choice_denominator_is_responders(self):
        """多选题：分母 = 回答人数（4）而不是选择次数（8）。"""
        rows = [
            ["ID", "国家", "推荐(1-10)", "功能", "建议"],
            ["u1", "中国", "8", "A,B", ""],
            ["u2", "美国", "5", "A", ""],
            ["u3", "中国", "9", "B,C", ""],
            ["u4", "中国", "7", "A,B,C", ""],
        ]
        md, _ = survey_stats.compute(rows, self._make_plan())
        # A 被 u1/u2/u4 选 = 3 次；分母 4
        self.assertIn("分母 = 4 份", md)
        # 找到 "| A | 3 |" 的整行
        self.assertRegex(md, r"\|\s*A\s*\|\s*3\s*\|")
        self.assertIn("75.0%", md)  # 3/4

    def test_scale_with_invalid_text(self):
        rows = [
            ["ID", "国家", "推荐(1-10)", "功能", "建议"],
            ["u1", "中国", "8", "", ""],
            ["u2", "美国", "5", "", ""],
            ["u3", "中国", "不知道", "", ""],
            ["u4", "中国", "10", "", ""],
        ]
        md, _ = survey_stats.compute(rows, self._make_plan())
        # 均值 = (8+5+10)/3 ≈ 7.67
        self.assertIn("7.67", md)
        # 非数字回答被剔除
        self.assertIn("非数字回答", md)
        self.assertIn("1 条", md)

    def test_open_text_collected_separately(self):
        rows = [
            ["ID", "国家", "推荐(1-10)", "功能", "建议"],
            ["u1", "中国", "8", "A", "希望加深色模式"],
            ["u2", "美国", "5", "A", ""],
            ["u3", "中国", "9", "A", "卡顿严重"],
        ]
        md, open_text = survey_stats.compute(rows, self._make_plan())
        # 主观题原文不出现在 stats_md 里，只在 open_text 通道
        self.assertNotIn("希望加深色模式", md)
        self.assertNotIn("卡顿严重", md)
        # open_text 通道里每条都是 dict {"ids":..., "profile":..., "text":...}
        items = open_text[4]
        self.assertEqual(len(items), 2)
        texts = {it["text"] for it in items}
        self.assertEqual(texts, {"希望加深色模式", "卡顿严重"})
        # 每条都应带 ID 和画像信息
        for it in items:
            self.assertIn("ID", it["ids"])  # ID 列在 plan 里 role=id
            self.assertIn("国家", it["profile"])  # 国家 是 profile_dim

    def test_empty_column(self):
        rows = [
            ["ID", "国家", "推荐(1-10)", "功能", "建议"],
            ["u1", "", "8", "A", ""],
            ["u2", "", "5", "A", ""],
        ]
        md, _ = survey_stats.compute(rows, self._make_plan())
        self.assertIn("（该列无有效数据）", md)

    def test_empty_rows(self):
        md, open_text = survey_stats.compute([], self._make_plan())
        self.assertIn("总样本: 0", md)
        self.assertEqual(open_text, {})

    def test_part_section_order(self):
        rows = [
            ["ID", "国家", "推荐(1-10)", "功能", "建议"],
            ["u1", "中国", "8", "A", "good"],
        ]
        md, _ = survey_stats.compute(rows, self._make_plan())
        # 顺序应该是 Part 1 -> Part 2 -> Part 3
        i1 = md.find("## Part 1 基本信息")
        i2 = md.find("## Part 2 体验")
        i3 = md.find("## Part 3 建议")
        self.assertGreater(i1, -1)
        self.assertGreater(i2, i1)
        self.assertGreater(i3, i2)


class TestCrossTab(unittest.TestCase):
    def test_cross_tab_categorical_with_low_sample(self):
        plan = {
            "columns": [
                {"index": 0, "name": "国家", "role": "profile_dim"},
                {"index": 1, "name": "选英雄", "role": "single_choice"},
            ],
            "parts": [{"name": "全部", "column_indexes": [1]}],
        }
        rows = [
            ["国家", "选英雄"],
            ["中国", "X"],
            ["中国", "X"],
            ["中国", "Y"],
            ["美国", "X"],  # 美国只有 1 条 → 单元格 < 5 应该带 *
        ]
        md, _ = survey_stats.compute(rows, plan)
        # 新设计：交叉表内嵌每道客观题底下，标题为"按「<画像>」分组"
        self.assertIn("按「国家」分组", md)
        # 小样本 < 5 的格应有 *
        self.assertIn("*", md)
        self.assertIn("样本量 < 5", md)

    def test_cross_tab_scale(self):
        plan = {
            "columns": [
                {"index": 0, "name": "国家", "role": "profile_dim"},
                {"index": 1, "name": "推荐", "role": "scale", "min": 1, "max": 10},
            ],
            "parts": [{"name": "全部", "column_indexes": [1]}],
        }
        rows = [
            ["国家", "推荐"],
            ["中国", "8"],
            ["中国", "10"],
            ["中国", "6"],
            ["中国", "9"],
            ["中国", "7"],
            ["美国", "5"],
            ["美国", "5"],
        ]
        md, _ = survey_stats.compute(rows, plan)
        # 新设计：scale 题底下"按「国家」分组（量表均值对比）"
        self.assertIn("按「国家」分组", md)
        # 中国 5 条均值 8.0
        self.assertIn("8.00", md)
        # 美国 2 条 < 5 → *
        self.assertRegex(md, r"美国\s*\|\s*2\*")


# ============================================================================
# survey_plan.parse_plan_from_llm
# ============================================================================


class TestParsePlan(unittest.TestCase):
    _GOOD_PLAN = """这是我的分析方案：
```json
{
  "columns": [
    {"index": 0, "name": "ID", "role": "id"},
    {"index": 1, "name": "国家", "role": "profile_dim"},
    {"index": 2, "name": "评分", "role": "scale", "min": 1, "max": 10}
  ],
  "parts": [
    {"name": "基础", "column_indexes": [0, 1]},
    {"name": "评估", "column_indexes": [2]}
  ],
  "cross_tabs": [{"profile_index": 1, "question_index": 2}],
  "open_questions": []
}
```
完成。"""

    def test_good_plan(self):
        plan, err = survey_plan.parse_plan_from_llm(self._GOOD_PLAN, header_count=3)
        self.assertIsNone(err)
        self.assertEqual(len(plan["columns"]), 3)

    def test_invalid_role(self):
        bad = self._GOOD_PLAN.replace('"role": "id"', '"role": "user_id_typo"')
        plan, err = survey_plan.parse_plan_from_llm(bad, header_count=3)
        self.assertIsNone(plan)
        self.assertIn("invalid role", err)

    def test_index_out_of_range(self):
        plan, err = survey_plan.parse_plan_from_llm(self._GOOD_PLAN, header_count=2)
        self.assertIsNone(plan)
        self.assertIn("out of range", err)

    def test_missing_part_for_column(self):
        # 缺的是 profile_dim 列（应在 part 里）；id 列不在 part 里反而是允许的
        bad = """```json
{
  "columns": [
    {"index": 0, "name": "ID", "role": "id"},
    {"index": 1, "name": "国家", "role": "profile_dim"}
  ],
  "parts": [{"name": "P1", "column_indexes": [0]}]
}
```"""
        plan, err = survey_plan.parse_plan_from_llm(bad, header_count=2)
        self.assertIsNone(plan)
        self.assertIn("not in any part", err)

    def test_id_columns_dont_need_part(self):
        """id 和 ignore 列不在任何 part 里也是合法的——它们不参与统计章节。"""
        good = """```json
{
  "columns": [
    {"index": 0, "name": "Timestamp", "role": "ignore"},
    {"index": 1, "name": "Discord", "role": "id"},
    {"index": 2, "name": "UID", "role": "id"},
    {"index": 3, "name": "国家", "role": "profile_dim"}
  ],
  "parts": [{"name": "基础", "column_indexes": [3]}]
}
```"""
        plan, err = survey_plan.parse_plan_from_llm(good, header_count=4)
        self.assertIsNone(err, msg=err)
        self.assertEqual(len(plan["columns"]), 4)

    def test_duplicate_column_in_parts(self):
        bad = """```json
{
  "columns": [
    {"index": 0, "name": "A", "role": "single_choice"},
    {"index": 1, "name": "B", "role": "single_choice"}
  ],
  "parts": [
    {"name": "P1", "column_indexes": [0]},
    {"name": "P2", "column_indexes": [0, 1]}
  ]
}
```"""
        plan, err = survey_plan.parse_plan_from_llm(bad, header_count=2)
        self.assertIsNone(plan)
        self.assertIn("multiple parts", err)

    def test_trailing_comma_sanitized(self):
        dirty = """```json
{
  "columns": [
    {"index": 0, "name": "A", "role": "id"},
  ],
  "parts": [{"name": "P", "column_indexes": [0]}],
}
```"""
        plan, err = survey_plan.parse_plan_from_llm(dirty, header_count=1)
        self.assertIsNone(err)
        self.assertEqual(plan["columns"][0]["index"], 0)

    def test_line_comments_sanitized(self):
        dirty = """```json
{
  "columns": [
    {"index": 0, "name": "A", "role": "id"} // 这是 ID 列
  ],
  "parts": [{"name": "P", "column_indexes": [0]}]
}
```"""
        plan, err = survey_plan.parse_plan_from_llm(dirty, header_count=1)
        self.assertIsNone(err, msg=err)
        self.assertEqual(plan["columns"][0]["role"], "id")

    def test_no_json_block(self):
        plan, err = survey_plan.parse_plan_from_llm("我看了下这个表，列分类如下：...", 5)
        self.assertIsNone(plan)
        self.assertIn("no JSON", err)

    def test_no_fence_but_has_braces(self):
        bare = """{"columns":[{"index":0,"name":"A","role":"id"}],"parts":[{"name":"P","column_indexes":[0]}]}"""
        plan, err = survey_plan.parse_plan_from_llm(bare, header_count=1)
        self.assertIsNone(err)
        self.assertEqual(plan["columns"][0]["role"], "id")

    def test_cross_tab_profile_must_be_profile_dim(self):
        bad = """```json
{
  "columns": [
    {"index": 0, "name": "A", "role": "single_choice"},
    {"index": 1, "name": "B", "role": "single_choice"}
  ],
  "parts": [{"name": "P", "column_indexes": [0, 1]}],
  "cross_tabs": [{"profile_index": 0, "question_index": 1}]
}
```"""
        plan, err = survey_plan.parse_plan_from_llm(bad, header_count=2)
        self.assertIsNone(plan)
        self.assertIn("profile_dim", err)


# ============================================================================
# survey_plan.is_user_approval
# ============================================================================


class TestApproval(unittest.TestCase):
    def test_approval_short_words(self):
        for s in ["OK", "ok", "确认", "好的", "可以", "yes", "对", "嗯", "OK！", "确认。"]:
            with self.subTest(s=s):
                self.assertTrue(survey_plan.is_user_approval(s), f"should approve: {s!r}")

    def test_revision_long(self):
        for s in [
            "OK 但是年龄段错了",
            "年龄段是画像不是单选",
            "改一下：第 7 列是段位",
            "no 我要改",
        ]:
            with self.subTest(s=s):
                self.assertFalse(survey_plan.is_user_approval(s), f"should NOT approve: {s!r}")

    def test_empty(self):
        self.assertFalse(survey_plan.is_user_approval(""))


# ============================================================================
# survey_stats.find_numbers_not_in_stats（数字漂移）
# ============================================================================


class TestNumberDrift(unittest.TestCase):
    def test_no_drift(self):
        stats = "中国 80 (41.0%), 美国 60 (30.7%)"
        report = "中国占 41.0% (80 人)，美国占 30.7%。"
        self.assertEqual(survey_stats.find_numbers_not_in_stats(report, stats), [])

    def test_drift_detected(self):
        stats = "中国 80 (41.0%)"
        report = "中国占 42.0%（其实是 41.0%）"
        drifted = survey_stats.find_numbers_not_in_stats(report, stats)
        self.assertIn("42.0%", drifted)


# ============================================================================
# value_aliases：多语言/同义合并统计
# ============================================================================


class TestValueAliases(unittest.TestCase):
    def test_single_choice_aliases_merge(self):
        """同义但不同写法/语言的选项归并到 canonical 后再算频数。"""
        plan = {
            "columns": [
                {"index": 0, "name": "段位", "role": "single_choice",
                 "value_aliases": {"王者": ["Mythic", "Mítica", "王者"]}},
            ],
            "parts": [{"name": "P", "column_indexes": [0]}],
        }
        rows = [
            ["段位"],
            ["Mythic"],
            ["王者"],
            ["Mítica"],
            ["传奇"],
        ]
        md, _ = survey_stats.compute(rows, plan)
        # Mythic / 王者 / Mítica 应合并成 1 行 "王者 | 3 | 75.0%"
        self.assertRegex(md, r"\|\s*王者\s*\|\s*3\s*\|\s*75\.0%")
        # 传奇没在 aliases 里，独立统计
        self.assertRegex(md, r"\|\s*传奇\s*\|\s*1\s*\|")
        # 不应出现独立的 Mythic / Mítica 行
        self.assertNotRegex(md, r"\|\s*Mythic\s*\|\s*\d")

    def test_multi_choice_aliases(self):
        plan = {
            "columns": [
                {"index": 0, "name": "功能", "role": "multi_choice",
                 "delimiter": ",",
                 "value_aliases": {"直播": ["直播", "Live", "En vivo"]}},
            ],
            "parts": [{"name": "P", "column_indexes": [0]}],
        }
        rows = [
            ["功能"],
            ["Live,录播"],          # → {直播, 录播}
            ["直播,En vivo"],       # 同行选两个同义项 → 集合化后 = {直播}
            ["录播"],               # → {录播}
        ]
        md, _ = survey_stats.compute(rows, plan)
        # 直播：被 row1 (Live) 和 row2 (直播+En vivo 去重) = 2 行选过
        # 录播：被 row1 和 row3 = 2 行选过
        self.assertRegex(md, r"\|\s*直播\s*\|\s*2\s*\|")
        self.assertRegex(md, r"\|\s*录播\s*\|\s*2\s*\|")

    def test_aliases_case_insensitive(self):
        """大小写差异也算同义。"""
        plan = {
            "columns": [
                {"index": 0, "name": "X", "role": "single_choice",
                 "value_aliases": {"YES": ["yes", "Yes", "YES"]}},
            ],
            "parts": [{"name": "P", "column_indexes": [0]}],
        }
        rows = [["X"], ["yes"], ["Yes"], ["YES"], ["No"]]
        md, _ = survey_stats.compute(rows, plan)
        self.assertRegex(md, r"\|\s*YES\s*\|\s*3\s*\|")

    def test_no_aliases_field(self):
        """没给 value_aliases 时退化为原始字符串统计。"""
        plan = {
            "columns": [
                {"index": 0, "name": "X", "role": "single_choice"},
            ],
            "parts": [{"name": "P", "column_indexes": [0]}],
        }
        rows = [["X"], ["A"], ["a"], ["B"]]  # 大小写默认不合并
        md, _ = survey_stats.compute(rows, plan)
        # A 和 a 算两个不同选项
        self.assertRegex(md, r"\|\s*A\s*\|\s*1\s*\|")
        self.assertRegex(md, r"\|\s*a\s*\|\s*1\s*\|")


# ============================================================================
# survey_plan.parse_aliases_json：alias enrichment 输出解析
# ============================================================================


class TestParseAliasesJson(unittest.TestCase):
    def test_normal(self):
        answer = """这是合并结果：
```json
{
  "1": {
    "王者": ["Mythic", "Mítica"],
    "传奇": ["Legend"]
  },
  "3": {}
}
```"""
        parsed, err = survey_plan.parse_aliases_json(answer)
        self.assertIsNone(err)
        self.assertEqual(parsed["1"]["王者"], ["Mythic", "Mítica"])
        self.assertEqual(parsed["3"], {})

    def test_missing_block(self):
        parsed, err = survey_plan.parse_aliases_json("没有 JSON")
        self.assertIsNone(parsed)

    def test_apply_aliases(self):
        plan = {
            "columns": [
                {"index": 1, "name": "段位", "role": "single_choice"},
                {"index": 2, "name": "功能", "role": "multi_choice"},
            ],
            "parts": [{"name": "P", "column_indexes": [1, 2]}],
        }
        aliases = {
            "1": {"王者": ["Mythic"]},
            "2": {},  # 显式空 → 清掉旧 aliases（这里本来就没）
        }
        new_plan = survey_plan.apply_aliases_to_plan(plan, aliases)
        self.assertEqual(new_plan["columns"][0]["value_aliases"], {"王者": ["Mythic"]})
        self.assertNotIn("value_aliases", new_plan["columns"][1])


if __name__ == "__main__":
    unittest.main()
