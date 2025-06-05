from fastapi import APIRouter, Request, HTTPException
from app.worker.processor import create_translation_task  # 你的核心业务逻辑
from app.common.core.logging import logger

router = APIRouter()

@router.post("/process-translation")
async def process_translation_task(request: Request):
    """
    Cloud Tasks 调用的端点：直接接收 JSON payload，执行翻译任务
    
    替换原来的 /pubsub/push 端点
    """
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
        
        # 🔥 直接同步调用，不使用 asyncio.create_task
        # 因为这里就是实际的工作端点，需要等待完成
        await create_translation_task(**payload)
        
        logger.info(f"✅ 翻译任务完成: {video_id}")
        
        # 返回成功结果给 Cloud Tasks
        return {
            "status": "completed", 
            "video_id": video_id,
            "message": "Translation task completed successfully"
        }
        
    except HTTPException:
        # 直接重新抛出 HTTP 异常
        raise
    except Exception as e:
        video_id = payload.get("video_id", "unknown") if 'payload' in locals() else "unknown"
        logger.error(f"❌ 翻译任务失败 {video_id}: {e}", exc_info=True)
        
        # 🔥 返回 500 错误，让 Cloud Tasks 自动重试
        raise HTTPException(500, f"Translation task failed: {e}")

