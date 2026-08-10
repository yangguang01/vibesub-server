"""
纯函数单元测试（不依赖任何外部 API：不连 OpenAI / AssemblyAI / yt-dlp / Firebase）。

这是重构的安全网（CLAUDE.md 第 6 节 / REFACTOR_PLAN.md 阶段 1 / T1.1）。
后续任何"搬代码、改结构"的提交，跑一次 `pytest` 应当全绿——若变红，说明改动
无意中改变了这些纯函数的行为。

运行方式（在 vibesub-server 目录下，已装好依赖的 venv 里）：
    PYTHONPATH=. pytest -q
或直接（已在 pytest.ini 配置 pythonpath=.）：
    pytest -q
"""
import datetime
from types import SimpleNamespace

from app.common.services.translation import (
    format_time,
    format_time_AssemblyAI,
    time_to_str,
    parse_time_range,
    subtitles_to_dict,
    extract_asr_sentences,
    json_to_srt,
    convert_AssemblyAI_to_srt,
)
from app.common.services.transcription import sentences_to_plain_text


# ---------- 时间格式化 ----------

def test_format_time_basic():
    # 秒(float) -> HH:MM:SS,mmm
    assert format_time(0) == "00:00:00,000"
    assert format_time(3661.5) == "01:01:01,500"
    assert format_time(59.999) == "00:00:59,999"


def test_format_time_assemblyai_basic():
    # 毫秒(int) -> HH:MM:SS,mmm
    assert format_time_AssemblyAI(0) == "00:00:00,000"
    assert format_time_AssemblyAI(3661500) == "01:01:01,500"
    assert format_time_AssemblyAI(1500) == "00:00:01,500"


def test_time_to_str_basic():
    dt = datetime.datetime(1900, 1, 1, 1, 2, 3, 456000)
    assert time_to_str(dt) == "01:02:03,456"


def test_parse_time_range_roundtrip():
    # 活路径用的是 parse_time_range（注意：旧的 parse_time 是坏的死代码，见 LESSONS.md N1）
    start_dt, end_dt = parse_time_range("00:00:01,500 --> 00:00:03,250")
    assert time_to_str(start_dt) == "00:00:01,500"
    assert time_to_str(end_dt) == "00:00:03,250"


# ---------- SRT <-> 字典 ----------

SAMPLE_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:02,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:02,000 --> 00:00:04,000\n"
    "Second line\n"
    "\n"
)


def test_subtitles_to_dict():
    d = subtitles_to_dict(SAMPLE_SRT)
    assert d[1] == ("00:00:00,000 --> 00:00:02,000", "Hello world")
    assert d[2] == ("00:00:02,000 --> 00:00:04,000", "Second line")


def test_extract_asr_sentences():
    sentences = extract_asr_sentences(SAMPLE_SRT)
    assert sentences == {1: "Hello world", 2: "Second line"}


def test_sentences_to_plain_text_orders_trims_and_skips_empty_sentences():
    sentences = {
        3: "  Third line  ",
        1: " First line\n",
        4: "   ",
        2: "Second line",
    }

    text = sentences_to_plain_text(sentences)

    assert text == "First line\n\nSecond line\n\nThird line"
    assert not text.endswith("\n")


def test_json_to_srt():
    data = {"segments": [
        {"start": 0, "end": 2, "text": "Hi"},
        {"start": 2, "end": 4.5, "text": "Bye"},
    ]}
    srt = json_to_srt(data)
    assert "00:00:00,000 --> 00:00:02,000" in srt
    assert "00:00:02,000 --> 00:00:04,500" in srt
    assert "Hi" in srt and "Bye" in srt
    # 第一段从序号 1 开始
    assert srt.splitlines()[0] == "1"


def test_convert_assemblyai_to_srt():
    # AssemblyAI 返回的 Sentence 对象有 .text / .start / .end(毫秒)
    sentences = [
        SimpleNamespace(text="Hello", start=0, end=2000),
        SimpleNamespace(text="World", start=2000, end=4000),
    ]
    srt = convert_AssemblyAI_to_srt(sentences)
    assert "00:00:00,000 --> 00:00:02,000" in srt
    assert "00:00:02,000 --> 00:00:04,000" in srt
    assert "Hello" in srt and "World" in srt
    assert srt.startswith("1\n")
