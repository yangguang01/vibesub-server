"""P000 — 随机化不变量测试（fuzz）。

用 200 个随机批 × 随机"作恶的 LLM 行为"（漏行/相对编号/越界/空值/坏 JSON/API 报错，
叠加修复阶段也可能失败），断言核心不变量【恒成立】：

  1) 输出行号集合 == 输入行号集合（绝不塌缩、绝不丢键）；
  2) 每个行号的值要么是该行自己的正确译文 mark(n)、要么是占位符
     （绝无"把别的行的译文串到这一行"的错位）。

这是 P000 修复最强的离线证明：无论 LLM 怎么乱来，机械错位都不可能发生。
"""
import random


def test_fuzz_invariant_always_holds(p000):
    rng = random.Random(20260623)
    runs = 200

    for _ in range(runs):
        start = rng.randint(1, 500)
        size = rng.randint(1, 14)
        expected = list(range(start, start + size))

        # ---- 随机初次行为 ----
        kind = rng.choice(["good", "drop", "relative", "extra", "empty", "bad", "raise"])
        if kind == "good":
            initial = p000.good
        elif kind == "drop":
            ds = set(rng.sample(expected, rng.randint(1, size)))
            initial = p000.drop(ds)
        elif kind == "relative":
            initial = p000.relative
        elif kind == "extra":
            initial = p000.extra
        elif kind == "empty":
            es = set(rng.sample(expected, rng.randint(1, size)))
            initial = p000.empty_values(es)
        elif kind == "bad":
            initial = p000.bad_json
        else:
            initial = p000.raise_api

        # ---- 随机修复行为（可能也失败）----
        rkind = rng.choice(["good", "drop", "bad"])
        if rkind == "good":
            repair = p000.good
        elif rkind == "drop":
            repair = p000.drop(set(rng.sample(expected, rng.randint(1, size))))
        else:
            repair = p000.bad_json

        trans, _ = p000.run_chunk(expected, initial, repair)

        ctx = f"kind={kind} rkind={rkind} expected={expected}"
        # 不变量 1：key 集合恰等于期望
        assert set(trans.keys()) == set(expected), f"key 集合不符: {ctx} got={sorted(trans.keys())}"
        # 不变量 2：每个值要么是本行正确译文，要么是占位（绝无串行）
        for n in expected:
            assert trans[n] == p000.mark(n) or trans[n] == p000.PLACEHOLDER, \
                f"错位/串行: 行{n} 得到 {trans[n]!r} ({ctx})"
