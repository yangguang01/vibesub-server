"""P000 翻译错位——对齐核心单元测试（T3 修复后，已全绿）。

T1 阶段这里是一组 xfail 骨架（针对旧的 process_transdict_num 按位置重编号）。
T3 修复后改为直接测新的确定性对齐函数 align_translation_by_key —— 之前的 xfail 现在全部转绿。
更重的"整批编排 + 修复 + 占位"测试见 tests/p000/。

align_translation_by_key(llm_json, expected_numbers) -> (aligned, missing)
核心原则（FIXES.md / LESSONS.md）：
  - 按 LLM 实际返回的 key 对齐，绝不按位置重编号；
  - LLM 漏掉的行号收进 missing，绝不让后面的译文往前顶替；
  - 越界 key 忽略；空/非字符串视为没翻出来。
"""
from app.common.services.translation import (
    align_translation_by_key,
    audit_translation_alignment,
    PLACEHOLDER,
)


def test_clean_absolute_keys():
    """老实：绝对行号 key，全部命中，无缺失。"""
    aligned, missing = align_translation_by_key({"11": "a", "12": "b", "13": "c"}, [11, 12, 13])
    assert aligned == {11: "a", 12: "b", 13: "c"}
    assert missing == []


def test_missing_line_marked_not_shifted():
    """漏行（迁移自旧 xfail）：只返回 1 和 3（漏了 2），期望 1..3。
    目标：3 仍在 key 3，缺的 2 进 missing，绝不把 3 的译文前移到 2。"""
    aligned, missing = align_translation_by_key({"1": "A", "3": "C"}, [1, 2, 3])
    assert aligned.get(1) == "A"
    assert aligned.get(3) == "C"      # 没有被前移到 2
    assert 2 not in aligned           # 没有占位、也没有被顶替
    assert missing == [2]


def test_shifted_absolute_keys_not_misassigned():
    """跳号（迁移自旧 xfail）：LLM 把行号写成 2/3/4（应为 1/2/3）。
    目标：按 LLM 自己声明的 key 对齐——2→'B'、3→'C'，4 越界丢弃，1 缺失；
    绝不把 'B' 当成第 1 行。"""
    aligned, missing = align_translation_by_key({"2": "B", "3": "C", "4": "D"}, [1, 2, 3])
    assert aligned.get(1) != "B"      # 关键：B 不会被错当成第 1 行
    assert aligned.get(2) == "B"
    assert aligned.get(3) == "C"
    assert missing == [1]


def test_empty_batch_all_missing_no_collapse():
    """整批失败（迁移自旧 xfail）：空返回不塌缩成一个键，而是每个期望行号都进 missing。"""
    aligned, missing = align_translation_by_key({}, [1, 2, 3])
    assert aligned == {}
    assert missing == [1, 2, 3]       # 逐行缺失，不是塌缩成单键


def test_extra_out_of_range_keys_ignored():
    """多行：越界 key（4）被忽略，1..3 各就各位。"""
    aligned, missing = align_translation_by_key({"1": "A", "2": "B", "3": "C", "4": "X"}, [1, 2, 3])
    assert aligned == {1: "A", 2: "B", 3: "C"}
    assert 4 not in aligned
    assert missing == []


def test_relative_numbering_recovered():
    """LLM 用 1..K 相对编号且数量恰好吻合：确定性按位置映射回绝对行号。"""
    aligned, missing = align_translation_by_key({"1": "x", "2": "y", "3": "z"}, [11, 12, 13])
    assert aligned == {11: "x", 12: "y", 13: "z"}
    assert missing == []


def test_relative_not_triggered_when_count_mismatch():
    """相对编号但数量对不上（只给了 1..2，期望 3 行）：不靠猜，全部判缺失，交给 LLM 修复。"""
    aligned, missing = align_translation_by_key({"1": "x", "2": "y"}, [11, 12, 13])
    assert aligned == {}
    assert missing == [11, 12, 13]


def test_empty_and_nonstring_values_treated_missing():
    """空串、纯空白、非字符串都算没翻出来。"""
    aligned, missing = align_translation_by_key({"1": "A", "2": "   ", "3": "", "4": 123}, [1, 2, 3, 4])
    assert aligned == {1: "A"}
    assert missing == [2, 3, 4]


def test_int_keys_normalized():
    """LLM 返回的 key 是 int 而非 str 也能对齐。"""
    aligned, missing = align_translation_by_key({1: "A", 2: "B"}, [1, 2])
    assert aligned == {1: "A", 2: "B"}
    assert missing == []


# ---------- 全局对齐审计 ----------

def test_audit_all_good():
    """全部翻出：审计报告 0 缺失 0 占位。"""
    summary = audit_translation_alignment([1, 2, 3], {1: "甲", 2: "乙", 3: "丙"})
    assert summary == {'total': 3, 'translated': 3, 'missing_keys': [], 'placeholder_keys': []}


def test_audit_flags_missing_and_placeholder():
    """审计能分别统计'整个键缺失'与'占位 [未翻译]'两类问题。"""
    summary = audit_translation_alignment([1, 2, 3, 4], {1: "甲", 2: PLACEHOLDER, 3: "丙"})
    assert summary['total'] == 4
    assert summary['translated'] == 2
    assert summary['missing_keys'] == [4]
    assert summary['placeholder_keys'] == [2]
