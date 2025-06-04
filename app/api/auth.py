import os
import datetime
import asyncio
from firebase_admin import auth
from typing import Optional
from fastapi import APIRouter, Request, Response, HTTPException, Depends, Header
from pydantic import BaseModel
from concurrent.futures import ThreadPoolExecutor

from app.common.utils.executor import executor
from app.common.core.logging import logger
from app.common.utils.auth import verify_firebase_session
from app.common.models.firestore_models import ensure_user

router = APIRouter()

class SessionLoginRequest(BaseModel):
    idToken: str

@router.post("/sessionLogin")
async def session_login(data: SessionLoginRequest, response: Response):
    expires_in = datetime.timedelta(days=14)
    loop = asyncio.get_event_loop()
    
    try:
        # 1. 创建会话 Cookie
        session_cookie = await loop.run_in_executor(
            executor, auth.create_session_cookie, data.idToken, expires_in
        )
    except Exception:
        raise HTTPException(status_code=400, detail="创建会话 Cookie 失败")
    
    # 2. 验证 ID Token，获取用户信息
    try:
        decoded = await loop.run_in_executor(
            executor, 
            lambda token: auth.verify_id_token(token, check_revoked=True), 
            data.idToken
        )
        uid = decoded["uid"]
        email = decoded.get("email", "")
        logger.info(f"uid: {uid}, email: {email}")
    except auth.InvalidIdTokenErro:
        raise HTTPException(status_code=401, detail="无效的 ID Token")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"验证 ID Token 失败: {e}")

    # 3. 写入或更新用户到 Firestore
    try:
        await loop.run_in_executor(executor, ensure_user, uid, email)
        logger.info(f"写入用户数据成功: {uid}, {email}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入用户数据失败: {e}")

    response.set_cookie(
        key="session",
        value=session_cookie,
        #domain=".rxaigc.com",
        domain="localhost",
        secure=True,
        httponly=True,
        samesite="none",
        max_age=int(expires_in.total_seconds())
    )
    logger.info(f"设置会话 Cookie 成功: {session_cookie}")
    return {"status": "success"}

@router.post("/sessionLogout")
async def session_logout(response: Response):
    response.delete_cookie(
        key="session",
        #domain=".rxaigc.com",
        domain="localhost",
        secure=False,
        httponly=True,
        samesite="lax",
    )
    return {"status": "success"}

@router.get("/me")
async def get_current_user(request: Request):
    session_cookie = request.cookies.get("session")
    if not session_cookie:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    try:
        decoded_claims = auth.verify_session_cookie(session_cookie, check_revoked=True)
    except auth.InvalidSessionCookieError:
        raise HTTPException(status_code=401, detail="无效的会话 Cookie")
    except Exception:
        raise HTTPException(status_code=401, detail="验证会话失败")
    
    try:
        ensure_user(decoded_claims["uid"], decoded_claims["email"])
    except Exception as e:
        # 这里可以根据需要记录日志或上报
        raise HTTPException(status_code=500, detail=f"写入用户数据失败: {e}")
    
    return decoded_claims 
