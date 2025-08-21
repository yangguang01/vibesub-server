import asyncio
from fastapi import APIRouter, Request, HTTPException
from app.worker.processor import create_translation_task  # 你的核心业务逻辑
from app.common.core.logging import logger
from app.common.core.config import TOTAL_TASK_TIMEOUT
from app.common.models.firestore_models import update_video_task

router = APIRouter()

@router.post("/process-translation")
async def process_translation_task(request: Request):
    """
    Cloud Tasks 调用的端点：直接接收 JSON payload，执行翻译任务
    
    替换原来的 /pubsub/push 端点
    """
    payload = None
    video_id = "unknown"
    
    try:
        # 🔥 直接解析 JSON，不需要 base64 解码
        payload = await request.json()
        
        # 验证必需字段
        required_fields = ["youtube_url", "user_id", "video_id", "content_name"]
        missing_fields = [field for field in required_fields if field not in payload]
        if missing_fields:
            raise HTTPException(400, f"Missing required fields: {missing_fields}")
        
        video_id = payload.get("video_id")
        logger.info(f"🎬 Cloud Tasks 开始处理翻译任务: {video_id}")
        
        # 🔥 关键改进：添加总超时控制
        try:
            await asyncio.wait_for(
                create_translation_task(**payload),
                timeout=TOTAL_TASK_TIMEOUT  # 使用配置文件中的超时设置
            )
            
            logger.info(f"✅ 翻译任务完成: {video_id}")
            
            # 返回成功结果给 Cloud Tasks
            return {
                "status": "completed", 
                "video_id": video_id,
                "message": "Translation task completed successfully"
            }
            
        except asyncio.TimeoutError:
            # 超时处理：立即更新任务状态为失败
            timeout_minutes = TOTAL_TASK_TIMEOUT // 60
            logger.error(f"⏰ 翻译任务超时: {video_id} ({timeout_minutes}分钟)")
            update_video_task(video_id, "failed", 0, [], f"任务执行超时 ({timeout_minutes}分钟)")
            
            # 返回成功状态让 Cloud Tasks 停止重试
            return {
                "status": "timeout", 
                "video_id": video_id,
                "message": f"Translation task timed out after {timeout_minutes} minutes"
            }
        
    except HTTPException:
        # 直接重新抛出 HTTP 异常
        raise
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ 翻译任务失败 {video_id}: {error_msg}", exc_info=True)
        
        # 🔥 关键改进：错误分类处理
        # 永久性错误直接返回成功，避免 Cloud Tasks 重试
        permanent_errors = [
            "bytes missing", "eoferror", "无法从URL提取", 
            "invalid url", "private video", "video unavailable"
        ]
        
        if any(keyword in error_msg.lower() for keyword in permanent_errors):
            logger.info(f"🚫 检测到永久性错误，停止重试: {video_id}")
            # 更新任务状态为失败
            if payload:
                update_video_task(video_id, "failed", 0, [], error_msg)
            
            # 返回成功状态让 Cloud Tasks 停止重试
            return {
                "status": "permanent_error", 
                "video_id": video_id,
                "error": error_msg
            }
        
        # 临时性错误：返回 500 让 Cloud Tasks 重试
        raise HTTPException(500, f"Translation task failed: {error_msg}")

