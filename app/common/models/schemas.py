from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


class TranslationRequest(BaseModel):
    """翻译请求模型"""
    youtube_url: HttpUrl
    custom_prompt: Optional[str] = ""
    special_terms: Optional[str] = ""
    content_name: Optional[str] = ""
    language: str = "zh-CN"
    model: Optional[str] = "gpt"  # 新增
    channel_name: Optional[str] = ""


class TaskResponse(BaseModel):
    """任务响应模型"""
    task_id: str
    status: str


class TaskStatus(BaseModel):
    """任务状态模型"""
    status: str
    progress: Optional[float] = None
    result_url: Optional[str] = None
    error: Optional[str] = None
    video_title: Optional[str] = None


class TranslationStrategiesResponse(BaseModel):
    """翻译策略响应模型"""
    strategies: Optional[List[str]] = None


class TaskDetail(BaseModel):
    """任务详细信息模型"""
    task_id: str
    status: str
    progress: Optional[float] = None
    youtube_url: Optional[str] = None
    video_title: Optional[str] = None
    video_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    result_url: Optional[str] = None
    error: Optional[str] = None
    model: Optional[str] = None
    language: Optional[str] = None
    created_at_beijing: Optional[str] = None
    completed_at: Optional[datetime] = None
    completed_at_beijing: Optional[str] = None
    request_count: Optional[int] = None
    english_srt_url: Optional[str] = None


class TaskListItem(BaseModel):
    """任务列表项模型"""
    task_id: str
    status: str
    video_title: Optional[str] = None
    video_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    progress: Optional[float] = None
    created_at_beijing: Optional[str] = None
    completed_at_beijing: Optional[str] = None
    request_count: Optional[int] = None
    first_requested_at_beijing: Optional[str] = None
    last_requested_at_beijing: Optional[str] = None
    user_request_count: Optional[int] = None


class PaginatedTaskListResponse(BaseModel):
    """分页任务列表响应模型"""
    items: List[TaskListItem]
    total: int
    has_more: bool
    last_doc_id: Optional[str] = None


class TaskQueryParams(BaseModel):
    """任务查询参数模型"""
    limit: Optional[int] = Field(10, ge=1, le=50)
    last_doc_id: Optional[str] = None
    status: Optional[str] = None


class UserDailyLimitResponse(BaseModel):
    """用户每日限额响应模型"""
    has_limit: bool
    limit_exceeded: bool
    daily_limit: int
    used_today: int
    remaining: int


class UsageStatItem(BaseModel):
    """使用统计项模型"""
    date: str
    count: int
    videos: List[str]


class UserTaskStatsResponse(BaseModel):
    """用户任务统计响应模型"""
    total_requests: int
    daily_limit: int
    daily_usage: List[UsageStatItem]


class AnalyticsSummary(BaseModel):
    """分析摘要模型"""
    total_requests: int
    unique_users: int
    new_tasks: int
    completed_tasks: int
    failed_tasks: int


class PopularVideo(BaseModel):
    """热门视频模型"""
    video_id: str
    title: str
    count: int 