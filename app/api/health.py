from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health_check():
    """
    健康检查端点
    
    返回:
        dict: 状态信息
    """
    return {"status": "ok", "version": "0.1.0"} 