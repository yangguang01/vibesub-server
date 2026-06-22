"""
P000 翻译错位——目标行为测试骨架（T1.2）。

这些测试编码的是【修复后应有的行为】，当前代码做不到，故全部标记 xfail。
它们正是 T3（修 P000）要逐个修绿的目标。T3 阶段会由作者新开 session 单独细化，
届时这些用例可能随最终接口微调（例如 process_transdict_num 被重命名/替换）。

核心原则（FIXES.md / LESSONS.md）：
  - 按 LLM 实际返回的 key 对齐，绝不按位置重编号；
  - LLM 漏掉的行号标占位符，绝不让后面的译文往前顶替；
  - 单批失败时整批按行号标占位，不塌缩成 1 个键、不污染其他批次。

当前实现的两个错位源（见 LESSONS.md 一节）：
  - process_transdict_num (translation.py:910) 用 enumerate(start=start_num) 按位置重编号；
  - process_chunk 失败兜底 (translation.py:882/886) 把整批塌缩成 first_item_number 一个键。
"""
import pytest

from app.common.services.translation import process_transdict_num

# 修复后用于标记"该行 LLM 没翻出来"的占位符（具体字符串由 T3 最终确定）
PLACEHOLDER = "[未翻译]"

xfail_p000 = pytest.mark.xfail(
    reason="P000 未修：当前按位置重编号 + 塌缩，待 T3 修复",
    strict=False,
)


@xfail_p000
def test_missing_line_marked_not_shifted():
    """漏行：LLM 只返回了 1 和 3（漏了 2），范围期望 1..3。
    目标：3 仍在 key 3，缺的 2 标占位，绝不把 3 的译文前移到 2。"""
    llm_out = {"1": "A", "3": "C"}
    result = process_transdict_num(llm_out, 1, 3)
    result = {int(k): v for k, v in result.items()}
    assert result.get(1) == "A"
    assert result.get(3) == "C"          # 当前会失败：C 被前移到了 2
    assert result.get(2) == PLACEHOLDER  # 当前会失败：没有占位


def test_extra_line_does_not_corrupt():
    """多行：LLM 多返回了一行（4 超出范围 1..3）。
    现状即正确：process_transdict_num 在 i>end_num 时 break，丢弃第 4 行，1..3 各就各位。
    保留为普通回归测试——T3 修复时不要让这条变红。"""
    llm_out = {"1": "A", "2": "B", "3": "C", "4": "X"}
    result = process_transdict_num(llm_out, 1, 3)
    result = {int(k): v for k, v in result.items()}
    assert result.get(1) == "A"
    assert result.get(2) == "B"
    assert result.get(3) == "C"
    assert 4 not in result


@xfail_p000
def test_renumbered_keys_realigned_by_value_key():
    """跳号：LLM 把行号写成了 2/3/4（应为 1/2/3）。
    目标：按 LLM 自己声明的 key 对齐到全局编号，而不是盲目按位置塞。
    （此用例描述目标语义，T3 最终接口可能据此调整）"""
    llm_out = {"2": "B", "3": "C", "4": "D"}
    result = process_transdict_num(llm_out, 1, 3)
    result = {int(k): v for k, v in result.items()}
    # 目标：不应把 B 当成第 1 行（当前实现恰恰会这么做）
    assert result.get(1) != "B"


@xfail_p000
def test_failed_batch_does_not_collapse_to_single_key():
    """整批失败：不应塌缩成 first_item_number 一个键。
    目标：失败批次按行号逐行标占位，范围内每个 key 都在。
    （process_transdict_num 仅是对齐环节；本用例标注 collapse 的最终防线在 process_chunk，
      T3 修 process_chunk 失败分支后，应有一个能直接验证'失败批次逐行占位'的入口。）"""
    # 模拟"对齐后仍缺整批"的输入：空 dict + 期望范围 1..3
    llm_out = {}
    result = process_transdict_num(llm_out, 1, 3)
    result = {int(k): v for k, v in result.items()}
    assert result.get(1) == PLACEHOLDER
    assert result.get(2) == PLACEHOLDER
    assert result.get(3) == PLACEHOLDER
