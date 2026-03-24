import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.common.core.config import APP_VERSION, INTERNAL_API_PREFIX
from app.common.core.logging import logger
from app.common.utils.file_utils import create_directories
from app.common.utils.firebase_init import init_firebase
from app.common.utils.youtube import log_yt_dlp_version
from app.worker.routes import router as worker_router

init_firebase()
log_yt_dlp_version()

app = FastAPI(
    title="YouTube字幕翻译Worker",
    description="处理 Cloud Tasks 回调并执行字幕翻译任务",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Worker 全局异常: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "worker 内部错误"})


@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    logger.warning(f"Worker 请求验证错误: {exc}")
    return JSONResponse(status_code=422, content={"detail": "请求参数无效"})


app.include_router(worker_router, prefix=INTERNAL_API_PREFIX)


@app.on_event("startup")
async def startup_event():
    create_directories()
    logger.info("worker 启动成功")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.worker.main:app", host="0.0.0.0", port=port, reload=True)
