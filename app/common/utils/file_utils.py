import os
import re
import shutil
import time
from pathlib import Path
from datetime import datetime, timedelta

from app.common.core.config import SUBTITLES_DIR, TRANSCRIPTS_DIR, TMP_DIR, AUDIO_DIR
from app.common.core.logging import logger


def create_directories():
    """创建必要的目录结构"""
    dirs = [SUBTITLES_DIR, TRANSCRIPTS_DIR, TMP_DIR, AUDIO_DIR]
    for dir_path in dirs:
        dir_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"确保目录存在: {dir_path}")

# 250403更新：删除get_file_paths函数，在create_translation_task函数初始化paths
# def get_file_paths(task_id, video_title=None, video_id=None):
#     """生成与任务相关的文件路径"""
#     # 确保文件名安全
#     if video_title:
#         # 移除特殊字符并用连字符替换空格
#         safe_title = re.sub(r'[^\w\s-]', '', video_title)
#         safe_title = re.sub(r'\s+', '-', safe_title)
#     else:
#         safe_title = task_id
    
#     # 生成各类文件路径
#     paths = {
#         "task_dir": TMP_DIR / task_id,
#         "audio": AUDIO_DIR / f"{task_id}.webm",
#         "transcript": TRANSCRIPTS_DIR / f"{task_id}.json",
#         "transcript_srt": TRANSCRIPTS_DIR / f"{task_id}.srt",
#         "subtitle": SUBTITLES_DIR / f"{video_id}.srt",
#         #"subtitle": SUBTITLES_DIR / f"{task_id}.srt"
#     }
    
#     # 确保任务目录存在
#     paths["task_dir"].mkdir(exist_ok=True)
    
#     return paths


def cleanup_task_files(task_id):
    """清理任务临时文件"""
    # 清理临时目录
    task_dir = TMP_DIR / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
        logger.info(f"已清理任务临时目录: {task_dir}")


def cleanup_audio_file(file_path):
    """清理音频文件"""
    try:
        if file_path.exists():
            file_path.unlink()
            logger.info(f"已清理音频文件: {file_path}")
    except Exception as e:
        logger.error(f"清理音频文件失败: {str(e)}")


def cleanup_old_files(directory, days):
    """清理超过指定天数的文件"""
    try:
        directory_path = Path(directory)
        if not directory_path.exists():
            return
        
        now = time.time()
        cutoff = now - (days * 86400)  # 天数转换为秒
        
        count = 0
        for file_path in directory_path.glob("*"):
            if file_path.is_file() and file_path.stat().st_mtime < cutoff:
                file_path.unlink()
                count += 1
        
        if count > 0:
            logger.info(f"从 {directory} 清理了 {count} 个超过 {days} 天的文件")
    except Exception as e:
        logger.error(f"清理旧文件时出错: {str(e)}")


def cleanup_all_audio_files():
    """清理所有临时音频文件"""
    try:
        if not AUDIO_DIR.exists():
            return
        
        count = 0
        for file_path in AUDIO_DIR.glob("*.webm"):
            if file_path.is_file():
                file_path.unlink()
                count += 1
        
        if count > 0:
            logger.info(f"清理了 {count} 个临时音频文件")
    except Exception as e:
        logger.error(f"清理音频文件时出错: {str(e)}")


def get_file_url(file_path):
    """获取文件URL"""
    if "static" in str(file_path):
        parts = str(file_path).split("static")
        return f"/static{parts[1]}"
    return str(file_path) 