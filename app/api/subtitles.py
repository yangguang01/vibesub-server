import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse

from app.common.utils.executor import executor
from app.worker.processor import get_task_status
from app.common.models.firestore_models import get_video_task, get_video_id_from_task
from app.common.core.config import SUBTITLES_DIR
from app.common.core.logging import logger

from firebase_admin import credentials, storage
from fastapi.responses import StreamingResponse
import io
import os

from app.common.utils.auth import get_current_user_id

router = APIRouter()

# 从环境变量读取 GCS bucket 名
SUBTITLE_PREFIX = "cn_srt"  # GCS 中字幕文件路径前缀，如 subtitles/abc123.srt

@router.get("/{task_id}")
async def get_subtitle_file(
    task_id: str,
    user_id: str = Depends(get_current_user_id)
):
    loop = asyncio.get_event_loop()

    # 1. 验证任务存在且已完成
    video_id = await loop.run_in_executor(executor, get_video_id_from_task, task_id)
    task_info = await loop.run_in_executor(executor, get_video_task, video_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="Task not found")
    if task_info.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Task not yet completed")

    # 2. 生成下载路径
    blob_path = f"{SUBTITLE_PREFIX}/{video_id}.srt"
    logger.info(f"blob_path: {blob_path}")

    # 3. 从 Firebase Storage 下载
    try:
        # 不传 name 则使用初始化时 options 中的 storageBucket
        def download_from_storage():
            bucket = storage.bucket()  
            blob = bucket.blob(blob_path)
            if not blob.exists():
                raise ValueError("Blob does not exist")
            return blob.download_as_bytes()
            
        subtitle_bytes = await loop.run_in_executor(executor, download_from_storage)
    except Exception as e:
        logger.error(f"Firebase Storage 下载失败: {e}", exc_info=True)
        raise HTTPException(status_code=404, detail="Subtitle file not found in storage")

    # 4. 返回 StreamingResponse
    return StreamingResponse(
        io.BytesIO(subtitle_bytes),
        media_type="application/x-subrip",
        headers={
            "Content-Disposition": f'attachment; filename="{video_id}.srt"'
        }
    )