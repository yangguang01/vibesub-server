import uuid
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.common.core.logging import logger
from app.common.models.firestore_models import (
    QuotaExceededError,
    count_user_tasks,
    create_task_request,
    ensure_user,
    get_task_detail,
    get_user_limit_info,
    get_user_tasks,
    get_video_id_from_task,
    release_user_daily_quota,
    reserve_user_daily_quota,
)
from app.common.models.schemas import (
    PaginatedTaskListResponse,
    TaskDetail,
    TaskListItem,
    TaskResponse,
    TaskStatus,
    TranslationRequest,
    TranslationStrategiesResponse,
    UserLimitInfoResponse,
)
from app.common.utils.auth import get_current_user_id
from app.common.utils.cloud_tasks import create_translation_cloud_task
from app.common.utils.executor import executor
from app.common.utils.youtube import extract_video_id, get_video_id_by_yt_dlp
from app.common.models.firestore_models import mark_task_failed, mark_task_queued

router = APIRouter()


@router.post("", response_model=TaskResponse)
async def translate_video(request: TranslationRequest, user_id: str = Depends(get_current_user_id)):
    loop = asyncio.get_event_loop()
    ensure_user(user_id, user_id)

    try:
        quota_reservation = reserve_user_daily_quota(user_id, user_id)
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    youtube_url = str(request.youtube_url)
    video_id = extract_video_id(youtube_url)
    if not video_id:
        video_id = await loop.run_in_executor(executor, get_video_id_by_yt_dlp, youtube_url)
    if not video_id:
        release_user_daily_quota(user_id, quota_reservation["date_key"])
        raise HTTPException(status_code=400, detail="无法从 URL 提取 YouTube 视频 ID")

    task_id = str(uuid.uuid4())
    task_request = create_task_request(
        task_id=task_id,
        user_id=user_id,
        video_id=video_id,
        youtube_url=youtube_url,
        content_name=request.content_name or "",
        model=request.model or "",
        special_terms=request.special_terms or "",
    )

    if not task_request["should_enqueue"]:
        logger.info(f"复用已有任务: video_id={video_id}, status={task_request['status']}")
        return TaskResponse(task_id=task_id, status=task_request["status"])

    payload = {
        "task_id": task_id,
        "youtube_url": youtube_url,
        "user_id": user_id,
        "video_id": video_id,
        "content_name": request.content_name or "",
        "special_terms": request.special_terms or "",
        "model": request.model or "",
    }

    try:
        queue_task_name = await loop.run_in_executor(executor, create_translation_cloud_task, payload)
        mark_task_queued(video_id, queue_task_name)
    except Exception as exc:
        logger.error(f"Cloud Task 创建失败: {exc}", exc_info=True)
        release_user_daily_quota(user_id, quota_reservation["date_key"])
        mark_task_failed(video_id, "enqueue", f"任务入队失败: {exc}")
        raise HTTPException(status_code=503, detail="任务入队失败，请稍后重试")

    return TaskResponse(task_id=task_id, status="queued")


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task_detail_endpoint(task_id: str, user_id: str = Depends(get_current_user_id)):
    return TaskDetail(**get_task_detail(task_id, owner_user_id=user_id))


@router.get("", response_model=PaginatedTaskListResponse)
async def list_tasks(
    limit: int = Query(10, ge=1, le=50),
    last_doc_id: Optional[str] = None,
    status: Optional[str] = None,
    user_id: str = Depends(get_current_user_id),
):
    tasks = get_user_tasks(user_id=user_id, limit=limit + 1, last_doc_id=last_doc_id, status_filter=status)
    has_more = len(tasks) > limit
    if has_more:
        tasks = tasks[:limit]

    total = count_user_tasks(user_id=user_id, status_filter=status)
    items = [TaskListItem(**task) for task in tasks]
    last_id = items[-1].task_id if items else None

    return PaginatedTaskListResponse(items=items, total=total, has_more=has_more, last_doc_id=last_id)


@router.get("/{task_id}/status", response_model=TaskStatus)
async def get_task_status_endpoint(task_id: str, user_id: str = Depends(get_current_user_id)):
    detail = get_task_detail(task_id, owner_user_id=user_id)
    return TaskStatus(
        task_id=task_id,
        video_id=detail["video_id"],
        status=detail["status"],
        stage=detail.get("stage"),
        progress=detail.get("progress", 0.0),
        retry_count=detail.get("retry_count", 0),
        failure_message=detail.get("failure_message"),
        updated_at=detail.get("updated_at"),
    )


@router.get("/{task_id}/strategies", response_model=TranslationStrategiesResponse)
async def get_translation_strategies(task_id: str, user_id: str = Depends(get_current_user_id)):
    detail = get_task_detail(task_id, owner_user_id=user_id)
    return TranslationStrategiesResponse(strategies=detail.get("translation_strategies") or [])


@router.get("/limit/info", response_model=UserLimitInfoResponse)
async def get_limit_info(user_id: str = Depends(get_current_user_id)):
    return UserLimitInfoResponse(**get_user_limit_info(user_id))
