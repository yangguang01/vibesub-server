import asyncio
from app.common.core.logging import logger
from app.common.core.config import SUBTITLES_DIR, TRANSCRIPTS_DIR, SUBTITLES_RETENTION_DAYS, TRANSCRIPTS_RETENTION_DAYS
from app.common.utils.file_utils import cleanup_old_files, cleanup_all_audio_files


async def periodic_cleanup():
    """定期运行的清理任务"""
    while True:
        try:
            logger.info("开始执行文件清理任务...")
            
            # 清理所有临时音频文件，不管多旧
            cleanup_all_audio_files()
            
            # 只保留最近指定天数的字幕和转写文件
            cleanup_old_files(SUBTITLES_DIR, SUBTITLES_RETENTION_DAYS)
            cleanup_old_files(TRANSCRIPTS_DIR, TRANSCRIPTS_RETENTION_DAYS)
            
            logger.info("文件清理任务完成")
        except Exception as e:
            logger.error(f"清理过程中发生错误: {str(e)}")
        
        # 每12小时运行一次
        await asyncio.sleep(12 * 3600)


def setup_cleanup_task():
    """设置清理任务"""
    asyncio.create_task(periodic_cleanup())
    logger.info("已启动定期文件清理任务") 