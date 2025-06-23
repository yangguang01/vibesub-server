"""
更高效的自动字幕下载 – 完整版
================================
- 先 **探测** 视频信息（1 次 yt-dlp 调用）
- 再按优先级 **下载** 自动字幕（至多再 1 次调用）
- 支持全局 `PROXY_URL` 设置代理
- 仅公开一个函数：`download_auto_subtitle(url)`

依赖：
```bash
pip install yt-dlp
```
"""

import logging
from pathlib import Path
from typing import Optional, Tuple
from yt_dlp import YoutubeDL

from app.common.core.logging import logger

# ---------------- 可自行修改的全局配置 ----------------
_LANG = "en"                               # 字幕语言
_PRIORITY = ("json3", "srv3")              # 自动字幕格式优先级
PROXY_URL: str | None = None               # 例如 "socks5://127.0.0.1:1080"
# ------------------------------------------------------


def _probe_video(url: str) -> dict:
    """仅探测视频元数据，不下载任何文件。"""
    ydl_opts = {"skip_download": True, "quiet": True}
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
        logger.info(f"使用代理检测字幕信息: {PROXY_URL}")
    return YoutubeDL(ydl_opts).extract_info(url, download=False)


def _select_caption(info: dict) -> Optional[str]:
    """根据优先级选择自动字幕格式，返回 ext（如 json3）或 None。"""
    auto_caps = info.get("automatic_captions") or {}
    tracks = auto_caps.get(_LANG) or []
    for ext in _PRIORITY:
        if any(t["ext"] == ext for t in tracks):
            return ext
    return None


def _download_subtitle(url: str, ext: str) -> Optional[Path]:
    """下载指定格式的自动字幕，成功返回本地文件路径。"""

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

    with YoutubeDL(dl_opts) as ydl:
        result = ydl.extract_info(url, download=True)

    sub_meta = result.get("requested_subtitles", {}).get(_LANG)
    if sub_meta and "filepath" in sub_meta:
        return Path(sub_meta["filepath"])
    return None


def download_auto_subtitle(url: str) -> Tuple[Optional[Path], str, str]:
    """下载 YouTube 自动字幕（json3 → srv3）。

    参数
    ------
    url : str
        YouTube 视频链接。

    返回
    ------
    tuple
        `(字幕文件 Path | None, 视频标题, 频道名称)`
    """

    # ① 探测视频信息（仅 1 次调用）
    info = _probe_video(url)
    title = info.get("title", "")
    channel = info.get("uploader") or info.get("channel", "")

    # ② 选择可用字幕格式
    chosen_ext = _select_caption(info)
    if not chosen_ext:
        logger.info("未找到自动字幕：%s (%s)", url, _LANG)
        return None, title, channel

    # ③ 下载字幕（第 2 次调用）
    path = _download_subtitle(url, chosen_ext)
    return path, title, channel

if __name__ == "__main__":
    url = "https://www.youtube.com/watch?v=LCEmiRjPEtQ"
    path, title, channel = download_auto_subtitle(url)
    print(path)
    print(title)
    print(channel)