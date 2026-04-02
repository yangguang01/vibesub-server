from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl

TaskStatusValue = Literal["created", "queued", "processing", "completed", "failed"]
TaskStageValue = Literal[
    "enqueue",
    "download",
    "context",
    "asr",
    "translate",
    "validate_alignment",
    "postprocess",
    "upload",
]


class TranslationRequest(BaseModel):
    youtube_url: HttpUrl
    custom_prompt: Optional[str] = ""
    special_terms: Optional[str] = ""
    content_name: Optional[str] = ""
    language: str = "zh-CN"
    model: Optional[str] = None
    channel_name: Optional[str] = ""


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatusValue | str
    message: Optional[str] = None


class TaskStatus(BaseModel):
    task_id: str
    video_id: str
    status: TaskStatusValue
    stage: Optional[TaskStageValue] = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    retry_count: int = 0
    failure_message: Optional[str] = None
    updated_at: Optional[datetime] = None


class TranslationStrategiesResponse(BaseModel):
    strategies: Optional[List[str]] = None


class TaskDetail(BaseModel):
    task_id: str
    status: TaskStatusValue
    stage: Optional[TaskStageValue] = None
    progress: Optional[float] = None
    youtube_url: Optional[str] = None
    video_title: Optional[str] = None
    video_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_url: Optional[str] = None
    english_srt_url: Optional[str] = None
    debug_url: Optional[str] = None
    failure_stage: Optional[str] = None
    failure_message: Optional[str] = None
    model: Optional[str] = None
    created_at_beijing: Optional[str] = None
    updated_at_beijing: Optional[str] = None
    retry_count: int = 0
    attempt_no: int = 0
    queue_task_name: Optional[str] = None
    translation_strategies: Optional[List[str]] = None


class TaskListItem(BaseModel):
    task_id: str
    status: TaskStatusValue
    stage: Optional[TaskStageValue] = None
    video_title: Optional[str] = None
    video_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    progress: Optional[float] = None
    created_at_beijing: Optional[str] = None
    updated_at_beijing: Optional[str] = None
    retry_count: int = 0
    failure_message: Optional[str] = None


class PaginatedTaskListResponse(BaseModel):
    items: List[TaskListItem]
    total: int
    has_more: bool
    last_doc_id: Optional[str] = None


class UserDailyLimitResponse(BaseModel):
    has_limit: bool
    limit_exceeded: bool
    daily_limit: int
    used_today: int
    remaining: int


class UserLimitInfoResponse(BaseModel):
    daily_limit: int
    used_today: int


class UsageStatItem(BaseModel):
    date: str
    count: int
    videos: List[str]


class UserTaskStatsResponse(BaseModel):
    total_requests: int
    daily_limit: int
    daily_usage: List[UsageStatItem]


class AnalyticsSummary(BaseModel):
    total_requests: int
    unique_users: int
    new_tasks: int
    completed_tasks: int
    failed_tasks: int


class PopularVideo(BaseModel):
    video_id: str
    title: str
    count: int
