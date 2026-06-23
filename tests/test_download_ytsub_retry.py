"""T4 单元测试：字幕抓取重试 + 永久/暂时失败分类。

mock 掉 yt-dlp（download_ytsub 内部的 YoutubeDL），覆盖：
- 限流(429) → 暂时性失败 → 退避重试后成功
- 私有视频 → 永久失败 → 立刻抛出、不重试
- 无字幕(automatic_captions 为空) → 返回 None → 调用方走音频 ASR 兜底（非失败）
- 分类函数本身的关键字判定

为避免真实等待，把退避时间打成 0（patch tenacity 的 wait）。
"""

from pathlib import Path

import pytest

import app.common.services.download_ytsub as dy
from app.common.services.download_ytsub import (
    download_auto_subtitle,
    PermanentSubtitleError,
    TemporarySubtitleError,
    SubtitleFetchError,
    _classify_download_error,
)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """把探测/下载两个被 @retry 装饰的函数的退避时间清零，测试不真的 sleep。"""
    # tenacity 的 retry 对象挂在被装饰函数的 .retry 属性上
    for fn in (dy._probe_video, dy._download_subtitle):
        retrying = getattr(fn, "retry", None)
        if retrying is not None:
            retrying.wait = lambda *a, **k: 0
    yield


class _FakeYDL:
    """模拟 yt_dlp.YoutubeDL：按预设脚本对 extract_info 抛错或返回。

    script: 可调用列表，每次 extract_info 弹出一个；元素是 Exception 则抛，
            否则当作返回的 info dict。
    calls: 累计 extract_info 调用次数（类级别，便于断言重试次数）。
    """

    def __init__(self, opts=None):
        self.opts = opts or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        _FakeYDL.calls += 1
        step = _FakeYDL.script[_FakeYDL.calls - 1] if _FakeYDL.calls - 1 < len(_FakeYDL.script) else _FakeYDL.script[-1]
        if isinstance(step, Exception):
            raise step
        return step


def _install_fake(monkeypatch, script):
    _FakeYDL.script = script
    _FakeYDL.calls = 0
    monkeypatch.setattr(dy, "YoutubeDL", _FakeYDL)


# ---------------------------------------------------------------------------
# 分类函数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg, expected", [
    ("ERROR: Private video. Sign in if you've been granted access", PermanentSubtitleError),
    ("Video unavailable", PermanentSubtitleError),
    ("This video has been removed by the user", PermanentSubtitleError),
    ("'xyz' is not a valid URL", PermanentSubtitleError),
    ("Join this channel to get access to members-only content", PermanentSubtitleError),
    ("HTTP Error 429: Too Many Requests", TemporarySubtitleError),
    ("The read operation timed out", TemporarySubtitleError),
    ("HTTP Error 503: Service Unavailable", TemporarySubtitleError),
    ("Connection reset by peer", TemporarySubtitleError),
    ("some brand new error we have never seen", TemporarySubtitleError),  # 未知保守按暂时
])
def test_classify(msg, expected):
    assert isinstance(_classify_download_error(Exception(msg)), expected)


# ---------------------------------------------------------------------------
# 限流 → 重试后成功
# ---------------------------------------------------------------------------
def test_rate_limited_then_success(monkeypatch):
    info_ok = {
        "title": "T",
        "uploader": "C",
        "automatic_captions": {"en": [{"ext": "json3"}, {"ext": "srv3"}]},
    }
    dl_ok = {"requested_subtitles": {"en": {"filepath": "/tmp/vid.json3"}}}
    # 探测：第1次 429（重试），第2次成功；下载：成功
    script = [
        Exception("HTTP Error 429: Too Many Requests"),
        info_ok,   # 探测重试成功
        dl_ok,     # 下载成功
    ]
    _install_fake(monkeypatch, script)

    path, title, channel = download_auto_subtitle("http://x")
    assert path == Path("/tmp/vid.json3")
    assert title == "T" and channel == "C"
    # 探测被调用了 2 次（1 次失败 + 1 次重试成功），证明确实重试了
    assert _FakeYDL.calls == 3  # 2 探测 + 1 下载


# ---------------------------------------------------------------------------
# 私有视频 → 永久失败，立刻抛出，不重试
# ---------------------------------------------------------------------------
def test_private_video_permanent_no_retry(monkeypatch):
    script = [Exception("ERROR: Private video")]
    _install_fake(monkeypatch, script)

    with pytest.raises(PermanentSubtitleError) as ei:
        download_auto_subtitle("http://x")
    # 永久失败只探测 1 次，没有重试
    assert _FakeYDL.calls == 1
    assert "私有" in ei.value.user_message or "无法访问" in ei.value.user_message


# ---------------------------------------------------------------------------
# 无字幕 → 不是失败，返回 None 让调用方走音频 ASR 兜底（不抛异常、不下载）
# ---------------------------------------------------------------------------
def test_no_subtitles_returns_none_for_audio_fallback(monkeypatch):
    info_no_sub = {"title": "T", "uploader": "C", "automatic_captions": {}}
    _install_fake(monkeypatch, [info_no_sub])

    path, title, channel = download_auto_subtitle("http://x")
    # 无字幕：返回 None 路径（=该走音频兜底），标题/频道仍带回
    assert path is None
    assert title == "T"
    assert channel == "C"
    # 探测 1 次即判定无字幕，未触发下载
    assert _FakeYDL.calls == 1


# ---------------------------------------------------------------------------
# 暂时性失败重试用尽 → 抛 TemporarySubtitleError（调用方据此走兜底）
# ---------------------------------------------------------------------------
def test_temporary_exhausted_raises_temporary(monkeypatch):
    # 探测每次都超时；_TOTAL_ATTEMPTS 次后放弃
    script = [Exception("The read operation timed out")]
    _install_fake(monkeypatch, script)

    with pytest.raises(TemporarySubtitleError):
        download_auto_subtitle("http://x")
    # 探测被尝试 _TOTAL_ATTEMPTS 次
    assert _FakeYDL.calls == dy._TOTAL_ATTEMPTS


# ---------------------------------------------------------------------------
# 探测说有字幕、下载却没落地文件 → 暂时性，下载步会重试
# ---------------------------------------------------------------------------
def test_download_missing_file_retries(monkeypatch):
    info_ok = {"title": "T", "uploader": "C",
               "automatic_captions": {"en": [{"ext": "json3"}]}}
    dl_empty = {"requested_subtitles": {}}  # 没有 filepath
    dl_ok = {"requested_subtitles": {"en": {"filepath": "/tmp/v.json3"}}}
    # 探测成功；下载第1次空（重试），第2次成功
    script = [info_ok, dl_empty, dl_ok]
    _install_fake(monkeypatch, script)

    path, _, _ = download_auto_subtitle("http://x")
    assert path == Path("/tmp/v.json3")
    # 1 探测 + 2 下载
    assert _FakeYDL.calls == 3


def test_temporary_is_subtitlefetcherror_subclass():
    assert issubclass(TemporarySubtitleError, SubtitleFetchError)
    assert issubclass(PermanentSubtitleError, SubtitleFetchError)
