from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from app.worker.processor import get_task_status
from app.common.core.config import SUBTITLES_DIR
from app.common.core.logging import logger

router = APIRouter()

@router.get("/{task_id}")
async def get_subtitle_file(task_id: str):
    """
    获取任务生成的字幕文件
    
    参数:
        task_id (str): 任务ID
        
    返回:
        FileResponse: 字幕文件
    """
    task_info = get_task_status(task_id)
    if task_info is None:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if task_info["status"] != "completed":
        raise HTTPException(status_code=400, detail="Task not yet completed")
    
    # 使用视频id查找字幕文件
    video_id = task_info.get("video_id", task_id)
    subtitle_file = SUBTITLES_DIR / f"{task_id}.srt"
    logger.info(f"字幕文件路径: {subtitle_file}")
    
    if not subtitle_file.exists():
        raise HTTPException(status_code=404, detail="Subtitle file not found")
    
    return FileResponse(
        path=subtitle_file,
        filename=f"{video_id}.srt",
        media_type="application/x-subrip"
    ) 