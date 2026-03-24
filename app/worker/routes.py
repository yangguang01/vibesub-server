import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.common.core.config import TOTAL_TASK_TIMEOUT
from app.common.core.logging import logger
from app.common.models.firestore_models import (
    mark_task_failed,
    start_task_attempt,
)
from app.common.utils.internal_auth import verify_internal_request
from app.worker.processor import PermanentTaskError, create_translation_task

router = APIRouter()


@router.post("/cloud-tasks/process-translation")
async def process_translation_task(
    request: Request,
    _: dict = Depends(verify_internal_request),
):
    payload = await request.json()
    required_fields = ["youtube_url", "user_id", "video_id", "content_name", "task_id"]
    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        raise HTTPException(status_code=400, detail=f"缺少字段: {missing_fields}")

    video_id = payload["video_id"]
    start_task_attempt(video_id, stage="download")

    try:
        await asyncio.wait_for(create_translation_task(**payload), timeout=TOTAL_TASK_TIMEOUT)
        return {"status": "completed", "video_id": video_id}
    except PermanentTaskError as exc:
        logger.error(f"任务永久失败 {video_id}: {exc}")
        mark_task_failed(video_id, exc.stage, str(exc))
        return {"status": "failed", "video_id": video_id, "message": str(exc)}
    except asyncio.TimeoutError:
        timeout_minutes = TOTAL_TASK_TIMEOUT // 60
        message = f"任务执行超时 ({timeout_minutes} 分钟)"
        logger.error(f"任务超时 {video_id}: {message}")
        mark_task_failed(video_id, "processing", message)
        raise HTTPException(status_code=500, detail=message)
    except Exception as exc:
        logger.error(f"任务临时失败 {video_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="翻译任务执行失败")
