import asyncio
import os

import yt_dlp
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.common.core.config import PROXY_URL
from app.common.core.logging import logger


def download_audio_webm(url, file_path):
    """
    从指定 URL 下载音频（仅下载 webm 格式的音频流）

    参数:
        url (str): 媒体资源的 URL
        file_path (Path): 保存音频的路径

    返回:
        Path: 下载后的音频文件路径
    """
    try:
        logger.info(f"开始下载视频: {url}")

        ydl_opts = {
            'format': 'bestaudio[ext=webm]',
            'outtmpl': str(file_path),
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
            'force_ipv4': True,
            'proxy': 'socks5://8t4v58911-region-US-sid-JaboGcGm-t-5:wl34yfx7@us2.cliproxy.io:443'
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        logger.info(f"视频下载完成: {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"下载失败: {str(e)}", exc_info=True)
        raise


def get_video_info(url):
    """
    获取YouTube视频信息

    参数:
        url (str): YouTube URL

    返回:
        dict: 视频信息字典
    """
    try:
        logger.info(f"获取视频信息: {url}")

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'forcejson': True,
            'force_ipv4': True,
            'proxy': 'socks5://8t4v58911-region-US-sid-JaboGcGm-t-5:wl34yfx7@us2.cliproxy.io:443',
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # 确保返回的信息中包含视频ID
        video_data = {
            'title': info.get('title', 'Unknown'),
            'id': info.get('id', ''),  # 提取视频ID
            'channel': info.get('channel', 'Unknown'),
            'duration': info.get('duration', 0),
            # 其他需要的信息...
        }

        logger.info(f"获取视频信息成功: {video_data['title']}, ID: {video_data['id']}")
        return video_data
    except Exception as e:
        logger.error(f"获取视频信息失败: {str(e)}", exc_info=True)
        raise


async def get_video_info_and_download_async(url, file_path):
    """
    异步获取YouTube视频信息并下载

    参数:
        url (str): YouTube URL
        file_path (str/Path): 目标文件路径

    返回:
        dict: 视频信息字典
    """
    logger.info("任务开始! 音频下载中...")

    # 定义一个同步函数用于在线程中执行
    def download_video():
        logger.info(f"开始在单独线程中下载视频: {url}")
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio[ext=webm]',
            'outtmpl': str(file_path),
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
            'force_ipv4': True,
            #'proxy': 'socks5://8t4v58911-region-US-sid-JaboGcGm-t-5:wl34yfx7@us2.cliproxy.io:443',
        }
        if PROXY_URL:
            ydl_opts['proxy'] = PROXY_URL
            logger.info(f"使用代理: {PROXY_URL}")

        print(file_path)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        logger.info(f"视频下载完成: {file_path}")
        return info

    # 使用asyncio.to_thread在单独的线程中执行下载操作
    logger.info("开始在单独线程中执行下载操作")
    info = await asyncio.to_thread(download_video)

    # 处理并返回视频信息
    video_data = {
        'title': info.get('title', 'Unknown'),
        'id': info.get('id', ''),  # 提取视频ID
        'channel': info.get('channel', 'Unknown'),
        'duration': info.get('duration', 0),
        # 其他需要的信息...
    }

    return video_data


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((yt_dlp.utils.DownloadError, OSError, ConnectionError))
)
def get_video_info_and_download(url):
    """
    从 YouTube 下载音频到当前工作目录，文件名为 <video_id>.webm，
    并返回视频信息和下载后的文件名。

    Args:
        url (str): YouTube 视频链接

    Returns:
        tuple:
            video_data (dict): 包含 title, id, channel
            filename (str): 下载到本地的文件名 (例如 "abc123.webm")
    """
    logger.info("任务开始！音频下载中...")

    # 直接在当前目录下，以视频 ID 作为文件名，后缀由 format 决定（这里固定 webm）
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio[abr<=128]/bestaudio',
        'outtmpl': '%(id)s.%(ext)s',
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
        },
        'force_ipv4': True,
        # 🔥 添加超时控制
        'socket_timeout': 60,  # 60秒连接超时
        'retries': 2,  # yt-dlp内部重试2次
    }
    if PROXY_URL:
        ydl_opts['proxy'] = PROXY_URL
        logger.info(f"使用代理: {PROXY_URL}")

    logger.info(f"开始下载视频: {url}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 🔥 先提取信息，检查视频可用性
            info = ydl.extract_info(url, download=False)

            # 检查视频状态
            if info.get('is_live'):
                raise ValueError("不支持直播视频")
            if info.get('availability') in ['private', 'premium_only', 'subscriber_only']:
                raise ValueError(f"视频不可访问: {info.get('availability')}")

            # 提取并下载
            info = ydl.extract_info(url, download=True)
            # ydl.prepare_filename 会使用 outtmpl 规则，返回实际写入的文件路径
            filepath = ydl.prepare_filename(info)

        video_id = info.get('id', '')
        # filepath 可能包含路径，这里只取文件名
        filename = os.path.basename(filepath)

        video_data = {
            'title': info.get('title', 'Unknown'),
            'id': video_id,
            'channel': info.get('channel', 'Unknown'),
        }

        logger.info(f"视频下载完成: {filename}")
        return video_data, filename

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"yt-dlp下载错误: {error_msg}")

        # 🔥 分类处理不同类型的下载错误
        if any(keyword in error_msg.lower() for keyword in [
            "bytes missing", "eoferror", "connection reset", "timeout",
            "http error 5", "temporary failure"
        ]):
            # 这些是临时性网络错误，可以重试
            logger.warning(f"检测到临时性网络错误，将重试: {error_msg}")
            raise  # 让tenacity重试
        else:
            # 永久性错误，不重试
            logger.error(f"检测到永久性错误，不重试: {error_msg}")
            raise ValueError(f"视频下载失败: {error_msg}")

    except Exception as e:
        logger.error(f"视频下载异常: {str(e)}")
        raise
