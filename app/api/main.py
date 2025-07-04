import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
import uvicorn


from app.api.router import api_router
from app.common.core.config import API_PREFIX
from app.common.core.logging import logger
from app.common.utils.file_utils import create_directories
from app.common.utils.cleanup import setup_cleanup_task
from app.common.utils.firebase_init import init_firebase
from app.common.utils.firebase_storage_init import *
from app.common.utils.youtube import log_yt_dlp_version

from dotenv import load_dotenv
load_dotenv()  # 会加载 .env 文件中的 proxy 配置

# 初始化 Firebase Admin SDK
init_firebase()

# 在 FastAPI 或启动脚本中调用一下
log_yt_dlp_version()

# 创建FastAPI应用
app = FastAPI(
    title="YouTube字幕翻译API",
    description="将YouTube视频字幕翻译为中文的API服务",
    version=" 1.0"
)


# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    #allow_origins=["*"],  # 允许所有来源，生产环境应该限制
    allow_origins=["https://auth.rxaigc.com", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  # 明确指定
    allow_headers=[
        "Accept",
        "Accept-Language", 
        "Content-Language",
        "Content-Type",
        "Authorization",
        "X-Requested-With"
    ],
)

# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"全局异常: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后再试"}
    )

@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    logger.warning(f"请求验证错误: {str(exc)}")
    return JSONResponse(
        status_code=422,
        content={"detail": "请求参数无效，请检查输入"}
    )

# 添加路由
app.include_router(api_router, prefix=API_PREFIX)


# 应用启动事件
@app.on_event("startup")
async def startup_event():
    """应用启动时执行的操作"""
    logger.info("应用启动成功")


if __name__ == "__main__":
    # 通过环境变量获取端口，gc run要求8080
    port = int(os.getenv("PORT", "8080"))
    
    # 启动服务
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=port, reload=True) 