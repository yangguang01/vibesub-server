"""
更高效的自动字幕下载 – 完整版
================================
- 先 **探测** 视频信息（1 次 yt-dlp 调用）
- 再按优先级 **下载** 自动字幕（至多再 1 次调用）
- 支持全局 `PROXY_URL` 设置代理
- 探测/下载两步都带**退避重试**（仅对暂时性失败重试）
- 区分**永久失败**（私有/不存在/无字幕/无效 URL）与**暂时失败**（限流/超时/5xx），
  分别抛出 `PermanentSubtitleError` / `TemporarySubtitleError`
- 仅公开一个函数：`download_auto_subtitle(url)` 与两个异常类型

依赖：
```bash
pip install yt-dlp
```
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

import yt_dlp
from yt_dlp import YoutubeDL
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.common.core.config import PROXY_URL, RETRY_ATTEMPTS
from app.common.core.logging import logger

# ---------------- 可自行修改的全局配置 ----------------
_LANG = "en"                               # 字幕语言
_PRIORITY = ("json3", "srv3")              # 自动字幕格式优先级
# 探测/下载重试：复用 config.RETRY_ATTEMPTS。它是「重试次数」，
# tenacity 的 stop_after_attempt 是「总尝试次数」=首次 + 重试，故 +1。
_TOTAL_ATTEMPTS = max(1, RETRY_ATTEMPTS + 1)
# ------------------------------------------------------


# ====================== 失败语义 ======================
class SubtitleFetchError(Exception):
    """字幕抓取失败的基类。`message` 为面向用户的可读文案。"""

    #: 面向用户、可直接展示的中文文案
    user_message: str = "字幕获取失败，请稍后重试"

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.user_message)
        if message:
            self.user_message = message


class PermanentSubtitleError(SubtitleFetchError):
    """永久失败：私有/已删除/无字幕/无效 URL。**不应重试，也不应走音频兜底。**"""

    user_message = "该视频无法获取字幕"


class TemporarySubtitleError(SubtitleFetchError):
    """暂时失败：限流/网络/超时/5xx。重试用尽后由调用方决定是否走音频兜底。"""

    user_message = "网络繁忙，字幕获取失败，请稍后重试"


# 永久性失败关键字（视频本身不可用 / 不会因重试而变好）。
# 统一收敛 cloud_tasks.py 里那份散落的清单，避免两处各写一份。
_PERMANENT_KEYWORDS = (
    "private video",
    "video unavailable",
    "video is unavailable",
    "this video is not available",
    "removed by the user",
    "account associated with this video has been terminated",
    "members-only",
    "join this channel",
    "sign in to confirm your age",
    "age-restricted",
    "is not a valid url",
    "unsupported url",
    "incomplete youtube id",
    "does not pass",  # yt-dlp: "... does not pass any of the URL patterns"
)

# 暂时性失败关键字（重试有意义）。
_TEMPORARY_KEYWORDS = (
    "429",
    "too many requests",
    "timed out",
    "timeout",
    "connection",
    "connection reset",
    "temporary failure",
    "temporarily unavailable",
    "http error 5",     # 5xx
    "read operation",
    "remote end closed",
    "unable to download webpage",
    "failed to extract",
    "nameresolutionerror",
    "max retries exceeded",
)


def _classify_download_error(err: Exception) -> SubtitleFetchError:
    """把 yt-dlp / 网络异常映射为永久 or 暂时失败。

    判定优先级：先永久（不可恢复）→ 再暂时（可重试）→ 兜底按暂时处理
    （宁可多重试一次，也不要把可恢复的错误误判成永久而放弃）。
    """
    msg = str(err).lower()

    if any(k in msg for k in _PERMANENT_KEYWORDS):
        # 给出更具体的用户文案
        if "private" in msg or "members-only" in msg or "join this channel" in msg:
            return PermanentSubtitleError("该视频为私有或会员专享，无法获取字幕")
        if "age" in msg:
            return PermanentSubtitleError("该视频有年龄限制，无法获取字幕")
        if "valid url" in msg or "unsupported url" in msg or "youtube id" in msg or "does not pass" in msg:
            return PermanentSubtitleError("视频链接无效，请检查后重试")
        return PermanentSubtitleError("该视频已被删除或无法访问")

    if any(k in msg for k in _TEMPORARY_KEYWORDS):
        return TemporarySubtitleError()

    # 未知错误：保守按暂时处理（允许重试），避免误判放弃可恢复的情况。
    return TemporarySubtitleError()


@retry(
    stop=stop_after_attempt(_TOTAL_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(TemporarySubtitleError),
    reraise=True,
)
def _probe_video(url: str) -> dict:
    """仅探测视频元数据，不下载任何文件。

    成功返回 info dict；失败按分类抛出 Permanent/TemporarySubtitleError。
    仅暂时性失败会触发 tenacity 重试，永久失败立即抛出不重试。
    """
    logger.info(f"开始探测视频信息: {url}")
    ydl_opts = {"skip_download": True, "quiet": True}
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
        logger.info(f"使用代理检测字幕信息: {PROXY_URL}")
    try:
        return YoutubeDL(ydl_opts).extract_info(url, download=False)
    except SubtitleFetchError:
        raise
    except Exception as e:
        classified = _classify_download_error(e)
        if isinstance(classified, TemporarySubtitleError):
            logger.warning(f"探测视频暂时性失败（将重试）: {e}")
        else:
            logger.error(f"探测视频永久性失败（不重试）: {e}")
        raise classified from e


def _select_caption(info: dict) -> Optional[str]:
    """根据优先级选择自动字幕格式，返回 ext（如 json3）或 None。"""
    auto_caps = info.get("automatic_captions") or {}
    tracks = auto_caps.get(_LANG) or []
    for ext in _PRIORITY:
        if any(t["ext"] == ext for t in tracks):
            return ext
    return None


@retry(
    stop=stop_after_attempt(_TOTAL_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(TemporarySubtitleError),
    reraise=True,
)
def _download_subtitle(url: str, ext: str) -> Path:
    """下载指定格式的自动字幕，成功返回本地文件路径。

    探测阶段已确认该格式存在，故下载落空视为暂时性失败（值得重试）。
    """

    dl_opts = {
        "skip_download": True,          # 不下载视频数据
        "writeautomaticsub": True,      # 仅下载自动字幕（等同 --write-auto-sub）
        "subtitleslangs": [_LANG],
        "subtitlesformat": ext,         # 指定字幕格式
        "quiet": True,
        "outtmpl": {
            "default": "%(id)s.%(ext)s",          # 主文件 (用不到但保留)
            "subtitle": "%(id)s.%(ext)s" # 字幕专用模板
        },
    }
    if PROXY_URL:
        dl_opts["proxy"] = PROXY_URL
        logger.info(f"使用代理下载字幕信息: {PROXY_URL}")

    try:
        with YoutubeDL(dl_opts) as ydl:
            result = ydl.extract_info(url, download=True)
    except SubtitleFetchError:
        raise
    except Exception as e:
        classified = _classify_download_error(e)
        if isinstance(classified, TemporarySubtitleError):
            logger.warning(f"下载字幕暂时性失败（将重试）: {e}")
        else:
            logger.error(f"下载字幕永久性失败（不重试）: {e}")
        raise classified from e

    sub_meta = (result.get("requested_subtitles") or {}).get(_LANG)
    if sub_meta and "filepath" in sub_meta:
        return Path(sub_meta["filepath"])

    # 探测说有、下载却拿不到文件：偶发，按暂时处理触发重试。
    logger.warning(f"探测到 {_LANG}/{ext} 字幕但下载未落地文件，按暂时性失败重试")
    raise TemporarySubtitleError("字幕下载未成功，请稍后重试")


def download_auto_subtitle(url: str) -> Tuple[Path, str, str]:
    """下载 YouTube 自动字幕（json3 → srv3）。

    参数
    ------
    url : str
        YouTube 视频链接。

    返回
    ------
    tuple
        `(字幕文件 Path, 视频标题, 频道名称)` —— 仅在成功时返回，Path 非空。

    异常
    ------
    PermanentSubtitleError
        视频私有/已删除/无字幕/链接无效。调用方应直接置 failed，**不要走音频兜底**。
    TemporarySubtitleError
        限流/网络/超时等暂时性失败（已退避重试用尽）。调用方可选择走音频兜底。
    """

    # ① 探测视频信息（带重试）
    info = _probe_video(url)
    title = info.get("title", "")
    channel = info.get("uploader") or info.get("channel", "")

    # ② 选择可用字幕格式：没有自动字幕 = 永久失败（不重试、不兜底）
    chosen_ext = _select_caption(info)
    if not chosen_ext:
        logger.info("未找到自动字幕（永久失败，不走音频兜底）：%s (%s)", url, _LANG)
        raise PermanentSubtitleError("该视频未提供英文字幕")

    # ③ 下载字幕（带重试）
    path = _download_subtitle(url, chosen_ext)
    logger.info(f"字幕下载成功: {path}（{title} / {channel}）")
    return path, title, channel


if __name__ == "__main__":
    url = "https://www.youtube.com/watch?v=LCEmiRjPEtQ"
    try:
        path, title, channel = download_auto_subtitle(url)
        print(path)
        print(title)
        print(channel)
    except SubtitleFetchError as e:
        print(f"[{type(e).__name__}] {e.user_message}")
