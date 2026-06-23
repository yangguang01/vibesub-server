"""P000 — process_chunk 端到端编排测试（用 FakeLLMClient，离线、零外部 API）。

验证修复后的核心保证（不依赖 LLM 老实，由代码构造保证）：
  返回 translations 的 key 集合 == 本批期望行号集合；
  每行要么是该行的正确译文、要么是占位符；
  绝不塌缩成单键、绝不让译文前移顶替别的行号；
  纠错重译只在代码已检测到缺行时才触发（不是每批都做的全量验证层）。
"""

EXPECTED = list(range(11, 21))  # 故意用非 1 起始的批，暴露"绝对行号 vs 相对编号"问题


def test_clean_first_pass_no_repair(p000):
    """老实翻译：一次通过，且没有多余的 LLM 调用（零成本）。"""
    trans, client = p000.run_chunk(EXPECTED, p000.good)
    assert set(trans.keys()) == set(EXPECTED)
    assert all(trans[n] == p000.mark(n) for n in EXPECTED)
    assert len(client.create_calls) == 1  # 首批通过 → 不触发修复


def test_dropped_lines_repaired(p000):
    """漏行：初次漏 13、17，纠错重译补回；无占位。"""
    trans, client = p000.run_chunk(EXPECTED, p000.drop({13, 17}), repair=p000.good)
    assert set(trans.keys()) == set(EXPECTED)
    assert trans[13] == p000.mark(13) and trans[17] == p000.mark(17)
    assert p000.PLACEHOLDER not in trans.values()
    assert len(client.create_calls) >= 2  # 检测到缺行 → 触发了修复


def test_persistent_drop_becomes_placeholder_no_collapse(p000):
    """初次和修复都漏 14、15：最终标占位，但 key 集合不塌缩、其它行不被污染/前移。"""
    trans, client = p000.run_chunk(EXPECTED, p000.drop({14, 15}), repair=p000.drop({14, 15}))
    assert set(trans.keys()) == set(EXPECTED)  # 关键：不塌缩成单键
    assert trans[14] == p000.PLACEHOLDER and trans[15] == p000.PLACEHOLDER
    for n in EXPECTED:
        if n not in (14, 15):
            assert trans[n] == p000.mark(n)  # 没有任何行被前移顶替


def test_relative_numbering_recovered_deterministically(p000):
    """LLM 用 1..K 相对编号：确定性按位置救回，顺序正确，且不需要 LLM 修复。"""
    trans, client = p000.run_chunk(EXPECTED, p000.relative)
    assert set(trans.keys()) == set(EXPECTED)
    assert all(trans[n] == p000.mark(n) for n in EXPECTED)
    assert len(client.create_calls) == 1  # 纯代码救回，零额外调用


def test_extra_out_of_range_keys_ignored(p000):
    """LLM 多返回越界 key：被忽略，不进结果、不顶替。"""
    trans, client = p000.run_chunk(EXPECTED, p000.extra)
    assert set(trans.keys()) == set(EXPECTED)
    assert all(trans[n] == p000.mark(n) for n in EXPECTED)


def test_bad_json_then_repair(p000):
    """初次返回非法 JSON：当作整批漏译，纠错重译救回。"""
    trans, client = p000.run_chunk(EXPECTED, p000.bad_json, repair=p000.good)
    assert set(trans.keys()) == set(EXPECTED)
    assert all(trans[n] == p000.mark(n) for n in EXPECTED)


def test_api_error_then_repair(p000):
    """初次 API 抛异常：不崩整批，纠错重译救回。"""
    trans, client = p000.run_chunk(EXPECTED, p000.raise_api, repair=p000.good)
    assert set(trans.keys()) == set(EXPECTED)
    assert all(trans[n] == p000.mark(n) for n in EXPECTED)


def test_total_failure_all_placeholder_no_collapse(p000):
    """初次和修复全失败：整批逐行占位，绝不塌缩成单键。"""
    trans, client = p000.run_chunk(EXPECTED, p000.bad_json, repair=p000.bad_json)
    assert set(trans.keys()) == set(EXPECTED)
    assert all(trans[n] == p000.PLACEHOLDER for n in EXPECTED)


def test_empty_values_treated_missing_and_repaired(p000):
    """空字符串译文视为没翻出来 → 修复补回。"""
    trans, client = p000.run_chunk(EXPECTED, p000.empty_values({12, 19}), repair=p000.good)
    assert set(trans.keys()) == set(EXPECTED)
    assert trans[12] == p000.mark(12) and trans[19] == p000.mark(19)


def test_first_batch_starting_at_one(p000):
    """边界：第一批 1..10（绝对编号恰好等于相对编号），不能误判、不能错位。"""
    expected = list(range(1, 11))
    trans, client = p000.run_chunk(expected, p000.good)
    assert set(trans.keys()) == set(expected)
    assert all(trans[n] == p000.mark(n) for n in expected)
    assert len(client.create_calls) == 1
