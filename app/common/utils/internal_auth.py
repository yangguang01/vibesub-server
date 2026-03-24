from fastapi import HTTPException, Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

from app.common.core.config import (
    CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL,
    INTERNAL_AUTH_ENABLED,
    WORKER_SERVICE_AUDIENCE,
)
from app.common.core.logging import logger


def verify_internal_request(request: Request):
    if not INTERNAL_AUTH_ENABLED:
        return {"auth": "disabled"}

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少内部调用凭证")

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        token_info = id_token.verify_oauth2_token(
            token,
            GoogleAuthRequest(),
            audience=WORKER_SERVICE_AUDIENCE,
        )
    except Exception as exc:
        logger.error(f"内部调用鉴权失败: {exc}")
        raise HTTPException(status_code=401, detail="内部调用鉴权失败")

    email = token_info.get("email", "")
    if CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL and email != CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:
        raise HTTPException(status_code=403, detail="内部调用身份不匹配")

    return token_info
