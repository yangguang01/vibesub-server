import asyncio
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.common.core.config import get_task_config
from app.common.core.logging import logger
from app.common.models.firestore_models import get_task_detail, get_video_id_from_task
from app.common.utils.auth import get_current_user_id
from app.common.utils.executor import executor
from app.common.services.infrastructure import get_storage_bucket

router = APIRouter()


@router.get("/{task_id}")
async def get_subtitle_file(task_id: str, user_id: str = Depends(get_current_user_id)):
    loop = asyncio.get_event_loop()
    video_id = await loop.run_in_executor(executor, get_video_id_from_task, task_id, user_id)
    task_detail = await loop.run_in_executor(executor, get_task_detail, task_id, user_id)
    if task_detail.get("status") != "completed":
        raise HTTPException(status_code=400, detail="任务尚未完成")

    storage_config = get_task_config("subtitle_storage")
    blob_path = f"{storage_config['path_prefix']}/{video_id}.srt"
    logger.info(f"下载字幕文件: {blob_path}")

    def download_from_storage():
        bucket = get_storage_bucket()
        blob = bucket.blob(blob_path)
        if not blob.exists():
            raise FileNotFoundError(blob_path)
        return blob.download_as_bytes()

    try:
        subtitle_bytes = await loop.run_in_executor(executor, download_from_storage)
    except Exception as exc:
        logger.error(f"字幕下载失败: {exc}", exc_info=True)
        raise HTTPException(status_code=404, detail="字幕文件不存在")

    return StreamingResponse(
        io.BytesIO(subtitle_bytes),
        media_type="application/x-subrip",
        headers={"Content-Disposition": f'attachment; filename="{video_id}.srt"'},
    )
