from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytz
from fastapi import HTTPException
from google.cloud import firestore
from google.cloud.firestore_v1 import ArrayUnion, Increment, SERVER_TIMESTAMP
from google.cloud.firestore_v1.base_query import FieldFilter

from app.common.core.config import DEFAULT_DAILY_LIMIT
from app.common.core.logging import logger
from app.common.services.firestore import db

VIDEOINFO_COLLECTION = "videoinfo"
USERINFO_COLLECTION = "userinfo"
USER_TASK_COLLECTION = "user_task"
DAILY_SUBCOL = "daily_usage"

TASK_STATUS_VALUES = {"created", "queued", "processing", "completed", "failed"}


class QuotaExceededError(Exception):
    pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_beijing_time(dt: datetime) -> str:
    beijing = pytz.timezone("Asia/Shanghai")
    return dt.astimezone(beijing).strftime("%Y-%m-%d %H:%M:%S")


def _today_key(dt: Optional[datetime] = None) -> str:
    dt = dt or _now_utc()
    return dt.astimezone(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d")


def _video_ref(video_id: str):
    return db.collection(VIDEOINFO_COLLECTION).document(video_id)


def _user_ref(user_id: str):
    return db.collection(USERINFO_COLLECTION).document(user_id)


def _daily_ref(user_id: str, date_key: str):
    return _user_ref(user_id).collection(DAILY_SUBCOL).document(date_key)


def _user_task_ref(task_id: str):
    return db.collection(USER_TASK_COLLECTION).document(task_id)


def ensure_user(user_id: str, email: Optional[str] = None) -> None:
    now = _now_utc()
    payload = {
        "user_id": user_id,
        "email": email or user_id,
        "updated_at": SERVER_TIMESTAMP,
        "updated_at_beijing": _format_beijing_time(now),
    }
    ref = _user_ref(user_id)
    if not ref.get().exists:
        payload.update(
            {
                "daily_limit": DEFAULT_DAILY_LIMIT,
                "total_requests": 0,
                "created_at": SERVER_TIMESTAMP,
                "created_at_beijing": _format_beijing_time(now),
            }
        )
    ref.set(payload, merge=True)


def reserve_user_daily_quota(user_id: str, email: Optional[str] = None) -> Dict[str, Any]:
    ensure_user(user_id, email)
    date_key = _today_key()
    now = _now_utc()
    user_ref = _user_ref(user_id)
    daily_ref = _daily_ref(user_id, date_key)

    @firestore.transactional
    def _reserve(transaction):
        user_doc = user_ref.get(transaction=transaction)
        user_data = user_doc.to_dict() or {}
        daily_limit = int(user_data.get("daily_limit", DEFAULT_DAILY_LIMIT))

        daily_doc = daily_ref.get(transaction=transaction)
        used_today = int((daily_doc.to_dict() or {}).get("count", 0))
        if used_today >= daily_limit:
            raise QuotaExceededError("您今日的翻译次数已用完，请明天再试")

        transaction.set(
            daily_ref,
            {
                "count": used_today + 1,
                "updated_at": SERVER_TIMESTAMP,
                "updated_at_beijing": _format_beijing_time(now),
            },
            merge=True,
        )
        transaction.set(
            user_ref,
            {
                "email": email or user_id,
                "updated_at": SERVER_TIMESTAMP,
                "updated_at_beijing": _format_beijing_time(now),
                "total_requests": Increment(1),
            },
            merge=True,
        )
        return {
            "date_key": date_key,
            "daily_limit": daily_limit,
            "used_today": used_today + 1,
        }

    return _reserve(db.transaction())


def release_user_daily_quota(user_id: str, date_key: Optional[str] = None) -> None:
    date_key = date_key or _today_key()
    user_ref = _user_ref(user_id)
    daily_ref = _daily_ref(user_id, date_key)
    now = _now_utc()

    @firestore.transactional
    def _release(transaction):
        daily_doc = daily_ref.get(transaction=transaction)
        daily_data = daily_doc.to_dict() or {}
        current = int(daily_data.get("count", 0))
        if current > 0:
            transaction.set(
                daily_ref,
                {
                    "count": current - 1,
                    "updated_at": SERVER_TIMESTAMP,
                    "updated_at_beijing": _format_beijing_time(now),
                },
                merge=True,
            )
        transaction.set(
            user_ref,
            {
                "updated_at": SERVER_TIMESTAMP,
                "updated_at_beijing": _format_beijing_time(now),
                "total_requests": Increment(-1),
            },
            merge=True,
        )

    _release(db.transaction())


def get_user_limit_info(user_id: str) -> Dict[str, Any]:
    ensure_user(user_id, user_id)
    date_key = _today_key()
    user_data = _user_ref(user_id).get().to_dict() or {}
    daily_data = _daily_ref(user_id, date_key).get().to_dict() or {}
    daily_limit = int(user_data.get("daily_limit", DEFAULT_DAILY_LIMIT))
    used_today = int(daily_data.get("count", 0))
    return {"daily_limit": daily_limit, "used_today": used_today}


def check_user_daily_limit(user_id: str) -> bool:
    limit_info = get_user_limit_info(user_id)
    return limit_info["used_today"] < limit_info["daily_limit"]


def create_task_request(
    task_id: str,
    user_id: str,
    video_id: str,
    youtube_url: str,
    content_name: str,
    model: str = "",
    special_terms: str = "",
) -> Dict[str, Any]:
    now = _now_utc()
    video_ref = _video_ref(video_id)
    video_doc = video_ref.get()
    should_enqueue = True

    if video_doc.exists:
        video_data = video_doc.to_dict() or {}
        current_status = video_data.get("status", "created")
        current_stage = video_data.get("stage", "enqueue")

        updates: Dict[str, Any] = {
            "request_count": Increment(1),
            "unique_users": ArrayUnion([user_id]),
            "updated_at": SERVER_TIMESTAMP,
            "updated_at_beijing": _format_beijing_time(now),
        }
        if youtube_url:
            updates["youtube_url"] = youtube_url
        if content_name and not video_data.get("video_title"):
            updates["video_title"] = content_name

        if current_status in {"failed", "created"}:
            updates.update(
                {
                    "status": "created",
                    "stage": "enqueue",
                    "progress": 0.0,
                    "failure_stage": "",
                    "failure_message": "",
                    "error": "",
                    "queue_task_name": "",
                }
            )
            current_status = "created"
            current_stage = "enqueue"
        else:
            should_enqueue = False

        video_ref.set(updates, merge=True)
    else:
        current_status = "created"
        current_stage = "enqueue"
        video_ref.set(
            {
                "video_id": video_id,
                "youtube_url": youtube_url,
                "video_title": content_name,
                "status": current_status,
                "stage": current_stage,
                "progress": 0.0,
                "retry_count": 0,
                "attempt_no": 0,
                "queue_task_name": "",
                "translation_strategies": [],
                "request_count": 1,
                "unique_users": [user_id],
                "result_url": "",
                "english_srt_url": "",
                "debug_url": "",
                "failure_stage": "",
                "failure_message": "",
                "error": "",
                "created_at": SERVER_TIMESTAMP,
                "created_at_beijing": _format_beijing_time(now),
                "updated_at": SERVER_TIMESTAMP,
                "updated_at_beijing": _format_beijing_time(now),
            }
        )

    _user_task_ref(task_id).set(
        {
            "task_id": task_id,
            "user_id": user_id,
            "video_id": video_id,
            "youtube_url": youtube_url,
            "content_name": content_name,
            "model": model,
            "special_terms": special_terms,
            "created_at": SERVER_TIMESTAMP,
            "created_at_beijing": _format_beijing_time(now),
            "updated_at": SERVER_TIMESTAMP,
            "updated_at_beijing": _format_beijing_time(now),
            "is_new": should_enqueue,
        }
    )

    return {
        "task_id": task_id,
        "video_id": video_id,
        "status": current_status,
        "stage": current_stage,
        "should_enqueue": should_enqueue,
    }


def create_user_task(
    user_id: str,
    video_id: str,
    youtube_url: str,
    task_id: str,
    is_new: bool,
) -> None:
    now = _now_utc()
    _user_task_ref(task_id).set(
        {
            "task_id": task_id,
            "user_id": user_id,
            "video_id": video_id,
            "youtube_url": youtube_url,
            "created_at": SERVER_TIMESTAMP,
            "created_at_beijing": _format_beijing_time(now),
            "updated_at": SERVER_TIMESTAMP,
            "updated_at_beijing": _format_beijing_time(now),
            "is_new": is_new,
        },
        merge=True,
    )


def create_or_update_video_task(
    video_id: str,
    youtube_url: str,
    video_title: str,
    user_id: str,
    translation_strategies: Optional[List[str]] = None,
) -> None:
    now = _now_utc()
    ref = _video_ref(video_id)
    payload = {
        "video_id": video_id,
        "youtube_url": youtube_url,
        "video_title": video_title,
        "unique_users": ArrayUnion([user_id]),
        "request_count": Increment(1),
        "updated_at": SERVER_TIMESTAMP,
        "updated_at_beijing": _format_beijing_time(now),
    }
    if translation_strategies is not None:
        payload["translation_strategies"] = translation_strategies

    if not ref.get().exists:
        payload.update(
            {
                "status": "created",
                "stage": "enqueue",
                "progress": 0.0,
                "retry_count": 0,
                "attempt_no": 0,
                "queue_task_name": "",
                "result_url": "",
                "english_srt_url": "",
                "debug_url": "",
                "failure_stage": "",
                "failure_message": "",
                "error": "",
                "created_at": SERVER_TIMESTAMP,
                "created_at_beijing": _format_beijing_time(now),
            }
        )

    ref.set(payload, merge=True)


def update_video_task(
    video_id: str,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    translation_strategies: Optional[List[str]] = None,
    error: Optional[str] = None,
    *,
    stage: Optional[str] = None,
    failure_stage: Optional[str] = None,
    failure_message: Optional[str] = None,
    queue_task_name: Optional[str] = None,
    attempt_no: Optional[int] = None,
    retry_count: Optional[int] = None,
    result_url: Optional[str] = None,
    english_srt_url: Optional[str] = None,
    debug_url: Optional[str] = None,
    extra_updates: Optional[Dict[str, Any]] = None,
) -> None:
    ref = _video_ref(video_id)
    now = _now_utc()
    updates: Dict[str, Any] = {
        "updated_at": SERVER_TIMESTAMP,
        "updated_at_beijing": _format_beijing_time(now),
    }

    if status:
        if status not in TASK_STATUS_VALUES:
            raise ValueError(f"无效任务状态: {status}")
        updates["status"] = status
        if status in {"created", "queued", "processing"}:
            updates["failure_stage"] = ""
            updates["failure_message"] = ""
            updates["error"] = ""
    if progress is not None:
        updates["progress"] = progress
    if translation_strategies is not None:
        updates["translation_strategies"] = translation_strategies
    if error is not None:
        updates["error"] = error
    if stage is not None:
        updates["stage"] = stage
    if failure_stage is not None:
        updates["failure_stage"] = failure_stage
    if failure_message is not None:
        updates["failure_message"] = failure_message
    if queue_task_name is not None:
        updates["queue_task_name"] = queue_task_name
    if attempt_no is not None:
        updates["attempt_no"] = attempt_no
    if retry_count is not None:
        updates["retry_count"] = retry_count
    if result_url is not None:
        updates["result_url"] = result_url
    if english_srt_url is not None:
        updates["english_srt_url"] = english_srt_url
    if debug_url is not None:
        updates["debug_url"] = debug_url
    if status == "queued":
        updates["queued_at"] = SERVER_TIMESTAMP
        updates["queued_at_beijing"] = _format_beijing_time(now)
    if status == "processing":
        updates["started_at"] = SERVER_TIMESTAMP
        updates["started_at_beijing"] = _format_beijing_time(now)
    if status == "completed":
        updates["completed_at"] = SERVER_TIMESTAMP
        updates["completed_at_beijing"] = _format_beijing_time(now)
        updates["failure_stage"] = ""
        updates["failure_message"] = ""
        updates["error"] = ""
    if extra_updates:
        updates.update(extra_updates)

    ref.set(updates, merge=True)


def mark_task_queued(video_id: str, queue_task_name: str) -> None:
    update_video_task(
        video_id,
        status="queued",
        stage="enqueue",
        progress=0.05,
        queue_task_name=queue_task_name,
    )


def start_task_attempt(video_id: str, stage: str = "download") -> Dict[str, int]:
    task_data = get_video_task(video_id) or {}
    attempt_no = int(task_data.get("attempt_no", 0)) + 1
    retry_count = max(attempt_no - 1, 0)
    update_video_task(
        video_id,
        status="processing",
        stage=stage,
        progress=0.1,
        attempt_no=attempt_no,
        retry_count=retry_count,
    )
    return {"attempt_no": attempt_no, "retry_count": retry_count}


def mark_task_completed(
    video_id: str,
    *,
    result_url: str = "",
    english_srt_url: str = "",
    debug_url: str = "",
) -> None:
    update_video_task(
        video_id,
        status="completed",
        stage="upload",
        progress=1.0,
        result_url=result_url,
        english_srt_url=english_srt_url,
        debug_url=debug_url,
    )


def mark_task_failed(video_id: str, failure_stage: str, failure_message: str) -> None:
    update_video_task(
        video_id,
        status="failed",
        stage=failure_stage,
        progress=0.0,
        failure_stage=failure_stage,
        failure_message=failure_message,
        error=failure_message,
    )


def get_video_task(video_id: str) -> Optional[Dict[str, Any]]:
    snap = _video_ref(video_id).get()
    return snap.to_dict() if snap.exists else None


def _get_user_task_or_raise(task_id: str, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
    snap = _user_task_ref(task_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    data = snap.to_dict() or {}
    if owner_user_id and data.get("user_id") != owner_user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return data


def get_video_id_from_task(task_id: str, owner_user_id: Optional[str] = None) -> str:
    task_data = _get_user_task_or_raise(task_id, owner_user_id=owner_user_id)
    video_id = task_data.get("video_id")
    if not video_id:
        raise HTTPException(status_code=500, detail=f"任务 {task_id} 数据异常：缺少 video_id")
    return video_id


def get_task_detail(task_id: str, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
    task_data = _get_user_task_or_raise(task_id, owner_user_id=owner_user_id)
    video_data = get_video_task(task_data["video_id"]) or {}
    merged = {
        "task_id": task_id,
        "video_id": task_data["video_id"],
        "youtube_url": task_data.get("youtube_url") or video_data.get("youtube_url"),
        "video_title": video_data.get("video_title") or task_data.get("content_name"),
        "status": video_data.get("status", "created"),
        "stage": video_data.get("stage", "enqueue"),
        "progress": float(video_data.get("progress", 0.0)),
        "retry_count": int(video_data.get("retry_count", 0)),
        "attempt_no": int(video_data.get("attempt_no", 0)),
        "queue_task_name": video_data.get("queue_task_name", ""),
        "translation_strategies": video_data.get("translation_strategies", []),
        "result_url": video_data.get("result_url"),
        "english_srt_url": video_data.get("english_srt_url"),
        "debug_url": video_data.get("debug_url"),
        "failure_stage": video_data.get("failure_stage"),
        "failure_message": video_data.get("failure_message") or video_data.get("error"),
        "created_at": task_data.get("created_at"),
        "created_at_beijing": task_data.get("created_at_beijing"),
        "updated_at": video_data.get("updated_at") or task_data.get("updated_at"),
        "updated_at_beijing": video_data.get("updated_at_beijing") or task_data.get("updated_at_beijing"),
        "queued_at": video_data.get("queued_at"),
        "started_at": video_data.get("started_at"),
        "completed_at": video_data.get("completed_at"),
        "model": task_data.get("model"),
    }
    return merged


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_task_detail(task_id)
    except HTTPException:
        video_data = get_video_task(task_id)
        return video_data


def get_user_tasks(
    user_id: str,
    limit: int = 10,
    last_doc_id: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = (
        db.collection(USER_TASK_COLLECTION)
        .where(filter=FieldFilter("user_id", "==", user_id))
        .order_by("created_at", direction=firestore.Query.DESCENDING)
    )

    if last_doc_id:
        last_doc = _user_task_ref(last_doc_id).get()
        if last_doc.exists:
            query = query.start_after(last_doc)

    query = query.limit(limit)
    items: List[Dict[str, Any]] = []
    for doc in query.stream():
        task_data = doc.to_dict() or {}
        try:
            detail = get_task_detail(doc.id, owner_user_id=user_id)
        except HTTPException:
            continue
        if status_filter and detail.get("status") != status_filter:
            continue
        task_data.update(detail)
        items.append(task_data)
    return items


def count_user_tasks(user_id: str, status_filter: Optional[str] = None) -> int:
    tasks = get_user_tasks(user_id=user_id, limit=5000, status_filter=status_filter)
    return len(tasks)


def record_successful_request(user_id: str, video_id: str, video_title: str) -> None:
    logger.info(f"任务完成记录已保留: user={user_id}, video={video_id}, title={video_title}")
