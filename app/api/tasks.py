from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List

from app.common.models.schemas import (
    TranslationRequest, TaskResponse, TaskStatus, 
    TranslationStrategiesResponse, PaginatedTaskListResponse,
    TaskDetail, TaskListItem, UserDailyLimitResponse
)
from app.worker.processor import create_translation_task, get_task_status, get_task_translation_strategies
from app.common.services.firestore import db
from app.common.models.firestore_models import (
    get_task, get_user_tasks, count_user_tasks, check_user_daily_limit
)
from app.api.auth import get_current_user_id
from app.common.core.logging import logger

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
    # 检查用户是否超过每日限额
    limit_info = check_user_daily_limit(user_id)
    
    # 如果有限制且超过限额，拒绝请求
    if limit_info.get("limit_exceeded", False):
        raise HTTPException(
            status_code=429, 
            detail={
                "message": "您今日的翻译次数已用完，请明天再试",
                "limit_info": limit_info
            }
        )
    
    task_id = create_translation_task(
        youtube_url=str(request.youtube_url),
        custom_prompt=request.custom_prompt,
        special_terms=request.special_terms,
        content_name=request.content_name,
        channel_name=request.channel_name,
        language=request.language,
        model=request.model,
        user_id=user_id
    )
    
    return {"task_id": task_id, "status": "pending"}

@router.get("/{task_id}", response_model=TaskDetail)
async def get_task_detail(task_id: str, user_id: str = Depends(get_current_user_id)):
    """
    获取任务详细信息
    
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
    获取任务状态
    
    参数:
        task_id (str): 任务ID
        
    返回:
        TaskStatus: 任务状态信息
    """
    task_info = get_task_status(task_id)
    if task_info is None:
        # 尝试从Firestore获取
        task_info = get_task(task_id)
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
    result = get_task_translation_strategies(task_id)
    if result is None:
        raise HTTPException(404, "Task not found or strategies not yet generated")
    return result

@router.get("/limit/check", response_model=UserDailyLimitResponse)
async def check_daily_limit(user_id: str = Depends(get_current_user_id)):
    """
    检查当前用户的每日使用限额
    
    返回:
        UserDailyLimitResponse: 用户限额信息
    """
    limit_info = check_user_daily_limit(user_id)
    return UserDailyLimitResponse(**limit_info) 