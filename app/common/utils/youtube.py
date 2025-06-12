import yt_dlp
import subprocess
from urllib.parse import urlparse, parse_qs

from app.common.core.logging import logger
from app.common.core.config import PROXY_URL


def extract_video_id(youtube_url: str) -> str:
    """
    从YouTube URL提取视频ID
    
    支持的URL格式:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - https://www.youtube.com/v/VIDEO_ID
    
    参数:
        youtube_url: YouTube视频URL
        
    返回:
        str: 视频ID
    """
    
    # 使用 urlparse 解析复杂URL

    parsed = urlparse(youtube_url)
    if 'youtube.com' in parsed.netloc:
        return parse_qs(parsed.query).get('v', [None])[0]
    
    elif 'youtu.be' in parsed.netloc:
        return parsed.path[1:]
    
    return ""

def get_video_id_by_yt_dlp(youtube_url: str) -> str:
    logger.info(f"使用yt-dlp提取视频ID")
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,  # 关键：只提取基本信息
        'skip_download': True,
    }
    if PROXY_URL:
        ydl_opts['proxy'] = PROXY_URL
        logger.info(f"使用代理获取视频ID: {PROXY_URL}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        return info.get('id')
    
import subprocess
import logging

def log_yt_dlp_version():
    try:
        version = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
        logger.info(f"yt-dlp version: {version}")
    except Exception as e:
        logger.error(f"无法获取 yt-dlp 版本：{e}")
