import uuid
import asyncio
from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor

from app.common.utils.executor import executor
from app.common.models.schemas import (
    TranslationRequest, TaskResponse, TaskStatus, 
    TranslationStrategiesResponse, PaginatedTaskListResponse,
    TaskDetail, TaskListItem, UserDailyLimitResponse, UserLimitInfoResponse
)
from app.worker.processor import create_translation_task, get_task_status, get_task_translation_strategies
from app.common.services.firestore import db
from app.common.models.firestore_models import (
    get_task, get_user_tasks, count_user_tasks, check_user_daily_limit,
    update_user_task_stats, get_video_task, create_user_task, create_or_update_video_task, update_video_task, record_successful_request, get_user_limit_info, get_video_id_from_task
)
from app.common.utils.auth import verify_firebase_session, get_current_user_id
from app.common.core.logging import logger
from app.common.utils.youtube import extract_video_id, get_video_id_by_yt_dlp
from app.common.utils.pubsub_client import publish_translation_task

router = APIRouter()

@router.post("", response_model=TaskResponse)
async def translate_video(request: TranslationRequest, user_id: str = Depends(get_current_user_id)):
    """
    创建新的视频字幕翻译任务
    
    参数:
        request (TranslationRequest): 包含YouTube URL和翻译选项的请求体
        
    返回:
        TaskResponse: 包含任务ID和状态的响应体
    """
    loop = asyncio.get_event_loop()
    
    # 判断是否超限
    if not await loop.run_in_executor(executor, check_user_daily_limit, user_id):
        raise HTTPException(
            status_code=429,
            detail="您今日的翻译次数已用完，请明天再试"
        )
    
    # 从YouTube URL提取视频ID
    youtube_url = str(request.youtube_url)
    video_id = extract_video_id(youtube_url)
    if not video_id:
        video_id = await loop.run_in_executor(executor, get_video_id_by_yt_dlp, youtube_url)
    if not video_id:
        raise HTTPException(
            status_code=400,
            detail="无法从URL提取YouTube视频ID，请检查URL格式"
        )
    
    # 创建任务id，判断结果是否存在
    task_id = str(uuid.uuid4())

    # 检查video_id对应的任务是否已存在且已完成
    video_task = await loop.run_in_executor(executor, get_video_task, video_id)
    if video_task and video_task.get("status") == "completed":
        logger.info("已存在，复用翻译结果")
        # 已完成，写入用户任务信息
        await loop.run_in_executor(
            executor, create_user_task, user_id, video_id, youtube_url, task_id, False
        )
        await loop.run_in_executor(
            executor, create_or_update_video_task, video_id, request.content_name, youtube_url, user_id
        )
        await loop.run_in_executor(
            executor, record_successful_request, user_id, video_id, request.content_name
        )
        return {"task_id": task_id, "status": video_task.get("status", "completed")}

    # 从来没有翻译的视频，创建翻译任务
    await loop.run_in_executor(
        executor, create_user_task, user_id, video_id, youtube_url, task_id, True
    )

    # 测试代码
    # pubsub测试未完成
    logger.info("新视频，通过pubsub下发任务")
    payload = {
        "youtube_url": youtube_url,
        "user_id": user_id,
        "video_id": video_id,
        "content_name": request.content_name,
        "special_terms": request.special_terms or "",
        "model": request.model or ""
    }
    publish_translation_task(payload)
    return {"task_id": task_id, "status": "pending"}


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task_detail(task_id: str, user_id: str = Depends(get_current_user_id)):
    """
    获取任务详细信息
    用于内部任务分析，不对用户开放
    
    参数:
        task_id (str): 任务ID
        
    返回:
        TaskDetail: 任务详细信息
    """
    # 首先从Firestore获取任务数据
    task_data = get_task(task_id)
    
    # 如果Firestore中不存在，则从内存状态获取
    if task_data is None:
        task_info = get_task_status(task_id)
        if task_info is None:
            raise HTTPException(status_code=404, detail="Task not found")
        task_data = task_info
    
    # 确保任务ID包含在响应中
    task_data["task_id"] = task_id
    
    return TaskDetail(**task_data)

@router.get("", response_model=PaginatedTaskListResponse)
async def list_tasks(
    limit: int = Query(10, ge=1, le=50),
    last_doc_id: Optional[str] = None,
    status: Optional[str] = None,
    user_id: str = Depends(get_current_user_id)
):
    """
    获取当前用户的任务列表
    以后用在dashboard页面，显示用户的翻译历史
    不对用户开发
    
    参数:
        limit (int): 每页记录数
        last_doc_id (str, optional): 上一页最后一条记录的ID
        status (str, optional): 状态过滤
        
    返回:
        PaginatedTaskListResponse: 分页任务列表
    """
    # 从Firestore获取任务列表
    tasks = get_user_tasks(
        user_id=user_id,
        limit=limit + 1,  # 多获取一条用于判断是否有更多
        last_doc_id=last_doc_id,
        status_filter=status
    )
    
    # 判断是否有更多数据
    has_more = len(tasks) > limit
    if has_more:
        tasks = tasks[:limit]  # 只保留请求的数量
    
    # 计算总数
    total = count_user_tasks(user_id=user_id, status_filter=status)
    
    # 获取最后一条记录的ID
    last_id = tasks[-1]["task_id"] if tasks else None
    
    # 转换为响应模型
    items = [TaskListItem(**task) for task in tasks]
    
    return PaginatedTaskListResponse(
        items=items,
        total=total,
        has_more=has_more,
        last_doc_id=last_id
    )


@router.get("/{task_id}/status", response_model=TaskStatus)
async def get_task_status_endpoint(task_id: str, user_id: str = Depends(get_current_user_id)):
    """
    接收task_id,从user_task表中获取video_id,再用video id从videoinfo里面取status
    """
    loop = asyncio.get_event_loop()

    video_id = await loop.run_in_executor(executor, get_video_id_from_task, task_id)
    task_info = await loop.run_in_executor(executor, get_video_task, video_id)
    
    if task_info is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskStatus(**task_info)


@router.get("/{task_id}/strategies", response_model=TranslationStrategiesResponse)
async def get_translation_strategies(task_id: str, user_id: str = Depends(get_current_user_id)):
    """
    获取任务的翻译策略
    
    参数:
        task_id (str): 任务ID
        
    返回:
        TranslationStrategiesResponse: 包含翻译策略的响应体
    """
    loop = asyncio.get_event_loop()

    video_id = await loop.run_in_executor(executor, get_video_id_from_task, task_id)
    video_doc = await loop.run_in_executor(executor, get_video_task, video_id)
    if video_doc is None:
        raise HTTPException(404, "Task not found or strategies not yet generated")
    
    strategies = video_doc.get("translation_strategies", [])
    return {"strategies": strategies}


@router.get("/limit/info", response_model=UserLimitInfoResponse)
async def get_limit_info(user_id: str = Depends(get_current_user_id)):
    """
    获取用户的使用限额信息
    返回当日使用量和每日上限
    
    返回:
        UserLimitInfoResponse: 用户限额信息
    """
    loop = asyncio.get_event_loop()

    limit_info = await loop.run_in_executor(executor, get_user_limit_info, user_id)
    return UserLimitInfoResponse(**limit_info) 