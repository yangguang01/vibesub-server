from fastapi import APIRouter, Request, HTTPException
import base64, json, asyncio
from app.worker.processor import create_translation_task  # 你的核心业务逻辑

router = APIRouter()

@router.post("/pubsub/push")
async def pubsub_push(request: Request):
    """
    Pub/Sub Push 订阅的入口：解码消息、调度后台任务、立即返回 200
    """
    body = await request.json()
    msg  = body.get("message", {})
    data_b64 = msg.get("data")
    if not data_b64:
        raise HTTPException(400, "Invalid Pub/Sub message payload")
    try:
        payload = json.loads(base64.b64decode(data_b64))
    except Exception as e:
        raise HTTPException(400, f"Bad message format: {e}")

    # 异步调度，不阻塞 HTTP ack
    asyncio.create_task(create_translation_task(**payload))
    return {"status": "ok"}