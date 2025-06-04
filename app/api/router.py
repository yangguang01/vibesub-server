from fastapi import APIRouter
from app.api.tasks import router as tasks_router
from app.api.subtitles import router as subtitles_router
from app.api.health import router as health_router
from app.api.auth import router as auth_router
from app.api.pubsub_push import router as pubsub_push_router

# 创建API路由器
api_router = APIRouter()

# 添加各个端点
api_router.include_router(tasks_router, prefix="/tasks", tags=["任务"])
api_router.include_router(subtitles_router, prefix="/subtitles", tags=["获取字幕"])
api_router.include_router(health_router, tags=["健康检查"])
api_router.include_router(auth_router, prefix="/auth", tags=["认证"])  
api_router.include_router(pubsub_push_router, prefix="/pubsub", tags=["pubsub"])