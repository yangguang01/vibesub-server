"""P000 离线测试共享夹具：一个伪装成 AsyncOpenAI 的最小桩 + 各种"作恶的 LLM"行为构造器。

设计要点（见 session 计划 / FIXES.md）：
- **不连任何外部 API**：FakeLLMClient 直接按脚本返回内容，离线、确定、可反复跑。
- **按 prompt 类型而非调用次数决定行为**：safe_api_call_async 外面套了 @async_retry（会偷偷重试），
  所以桩根据"消息里有没有修复反馈签名"来区分『初次翻译』和『纠错重译』，让隐藏的内部重试保持一致。
- **值里编码真实源行号**（mark(n)=译{n}），这样一旦发生"错位/串行"，断言能立刻抓到。
- autouse 的 _no_sleep 把退避 sleep 变成瞬时，保证测试快。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.common.services.translation import process_chunk, PLACEHOLDER

# process_chunk 会对 system 模板做 .format()，这里给一个最小可格式化的模板即可
SYS_TEMPLATE = "sys {custom_prompt} lines {first_item_number}-{end_item_number} n={check_chunk_string}"

MARK = "译"  # 译文标记前缀；mark(n) 唯一对应源行 n，便于检测错位


def mark(n):
    return f"{MARK}{n}"


def _resp(content):
    """构造一个形如 OpenAI 响应的对象：response.choices[0].message.content。"""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# ---------- "作恶的 LLM"行为构造器：给定 expected 行号列表，返回一段输出内容(str)，或 raise ----------

def good(expected):
    """老实：用绝对行号当 key，全部翻出。"""
    return json.dumps({str(n): mark(n) for n in expected}, ensure_ascii=False)


def drop(drop_set):
    """漏行：缺掉 drop_set 里的行号（其余正确）。"""
    def _b(expected):
        return json.dumps({str(n): mark(n) for n in expected if n not in drop_set}, ensure_ascii=False)
    return _b


def relative(expected):
    """没听绝对行号指令，用了 1..K 相对编号，但值仍对应正确源行（顺序翻全）。"""
    return json.dumps({str(i): mark(expected[i - 1]) for i in range(1, len(expected) + 1)}, ensure_ascii=False)


def extra(expected):
    """多返回若干越界 key（应被忽略，不进结果、不顶替）。"""
    d = {str(n): mark(n) for n in expected}
    d["99999"] = "越界垃圾"
    d[str(expected[-1] + 50)] = "越界垃圾2"
    return json.dumps(d, ensure_ascii=False)


def empty_values(empty_set):
    """部分行返回空字符串（应被判为没翻出来）。"""
    def _b(expected):
        return json.dumps({str(n): ("" if n in empty_set else mark(n)) for n in expected}, ensure_ascii=False)
    return _b


def bad_json(expected):
    """返回非法 JSON（safe_api_call_async 会抛错）。"""
    return "this is not json {{{"


def raise_api(expected):
    """模拟 API 调用本身抛异常。"""
    raise RuntimeError("simulated api boom")


class FakeLLMClient:
    """伪装成 AsyncOpenAI 的最小桩。

    initial / repair 都是 callable(expected_numbers) -> content_str（可 raise）。
    通过消息里是否包含修复反馈签名来区分初次与修复，与 async_retry 的内部重试解耦。
    """
    REPAIR_SIG = "You missed or left empty"

    def __init__(self, expected, initial, repair=None):
        self.expected = list(expected)
        self.initial = initial
        self.repair = repair if repair is not None else good
        self.create_calls = []  # 记录每次 create 的 messages，便于断言"是否触发了修复"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        messages = kwargs.get("messages", [])
        self.create_calls.append(messages)
        is_repair = any(self.REPAIR_SIG in (m.get("content") or "") for m in messages)
        behavior = self.repair if is_repair else self.initial
        content = behavior(self.expected)  # 可能 raise，交给上层 async_retry / process_chunk
        return _resp(content)


async def _run_chunk(expected, client, model):
    chunk = [(n, f"en line {n}") for n in expected]
    semaphore = asyncio.Semaphore(5)
    result = await process_chunk(chunk, "ctx", model, client, semaphore, SYS_TEMPLATE, "vid-test")
    return result["translations"]


def run_chunk(expected, initial, repair=None, model="deepseek-chat"):
    """跑一整批 process_chunk，返回 (translations, client)。"""
    client = FakeLLMClient(expected, initial, repair)
    trans = asyncio.run(_run_chunk(expected, client, model))
    return trans, client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """把 async_retry 的退避 sleep 变成瞬时，保证离线测试快。"""
    async def _instant(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.fixture
def p000():
    return SimpleNamespace(
        run_chunk=run_chunk,
        FakeLLMClient=FakeLLMClient,
        PLACEHOLDER=PLACEHOLDER,
        good=good,
        drop=drop,
        relative=relative,
        extra=extra,
        empty_values=empty_values,
        bad_json=bad_json,
        raise_api=raise_api,
        mark=mark,
    )
