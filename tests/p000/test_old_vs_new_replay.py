"""P000 — 旧逻辑 vs 新逻辑，喂同一份 LLM 输出的确定性对比（before/after 证据）。

把"同一段 LLM 返回"分别喂给旧对齐和新对齐，直接展示旧代码的两个错位源会怎样毁掉行号，
而新代码不会。纯离线、确定、可复现——不依赖任何外部 API。

旧逻辑（T3 之前，复刻自被删的 process_chunk + 仍在文件里的 process_transdict_num）：
  - 行数相等 → process_transdict_num 按位置重编号（漏一行就整体前移，静默错位）；
  - 行数不等 → 整批塌缩成 first 一个键（整批真实译文丢失）。
新逻辑：align_translation_by_key 按 key 对齐，漏的进 missing（之后会被纠错重译/占位），绝不错位。
"""
import json

from app.common.services.translation import (
    process_transdict_num,
    align_translation_by_key,
)


def zh(n):
    return f"译{n}"


def old_align(raw, expected):
    """复刻 T3 之前 process_chunk 的对齐结果。"""
    first, end = expected[0], expected[-1]
    try:
        j = json.loads(raw)
    except Exception:
        return {first: "翻译失败"}
    if isinstance(j, dict) and len(j) == len(expected):
        renum = process_transdict_num(j, first, end)   # 位置重编号
        return {int(k): v for k, v in renum.items()}
    return {first: "翻译失败"}                          # 整批塌缩


def test_count_mismatch_old_collapses_new_keeps():
    """漏一行导致行数不等：旧代码把整批 10 行塌缩成 1 个键（丢 9 行真实译文）；
    新代码保住 9 行、只把漏的那行标 missing（之后会被纠错重译补回）。"""
    expected = list(range(51, 61))                     # 第二批 51..60
    j = {str(n): zh(n) for n in expected if n != 53}   # LLM 漏了 53，共 9 个 key
    raw = json.dumps(j, ensure_ascii=False)

    old = old_align(raw, expected)
    new_aligned, new_missing = align_translation_by_key(json.loads(raw), expected)

    # 旧：塌缩——只剩 first 一个键，9 行真实译文全丢
    assert old == {51: "翻译失败"}
    # 新：9 行各就各位，唯独 53 进 missing（不前移、不塌缩）
    assert set(new_aligned.keys()) == set(expected) - {53}
    assert new_aligned[54] == zh(54)
    assert new_missing == [53]


def test_count_matches_but_shifted_old_silently_corrupts():
    """行数"凑巧"相等但内容错位：旧代码按位置重编号→53 拿到 54 的译文(静默错位)、
    译53 永久丢失、越界的 61 被顶成第 60 行；新代码按 key 对齐，53 正确判缺、其余不串行。"""
    expected = list(range(51, 61))                     # 51..60
    # LLM 漏了 53，又多塞了越界的 61 → 正好 10 个 key（行数检查会"通过"）
    present = [51, 52, 54, 55, 56, 57, 58, 59, 60, 61]
    j = {str(n): zh(n) for n in present}
    raw = json.dumps(j, ensure_ascii=False)

    old = old_align(raw, expected)
    new_aligned, new_missing = align_translation_by_key(json.loads(raw), expected)

    # 旧：静默错位 —— 53 被塞进了 54 的译文，译53 丢失，越界 61 被当作第 60 行
    assert old[53] == zh(54)                            # 错位！
    assert zh(53) not in old.values()                  # 译53 永久丢失
    assert old[60] == zh(61)                            # 越界垃圾被顶成第 60 行

    # 新：按 key 对齐 —— 53 正确判缺，54 仍是 54、不前移，越界 61 被忽略
    assert 53 not in new_aligned
    assert new_missing == [53]
    assert new_aligned[54] == zh(54)
    assert zh(61) not in new_aligned.values()


def test_bad_json_old_collapses_new_flags_all_missing():
    """LLM 返回坏 JSON：旧代码塌缩成 1 个键；新代码把整批标 missing（交给纠错重译/占位）。"""
    expected = list(range(1, 11))
    raw = "not json {{{"

    old = old_align(raw, expected)
    new_aligned, new_missing = align_translation_by_key({}, expected)

    assert old == {1: "翻译失败"}
    assert new_aligned == {}
    assert new_missing == expected
