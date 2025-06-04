import os
from fastapi import Request, HTTPException, Depends
from firebase_admin import auth, credentials, initialize_app
from firebase_admin.auth import InvalidSessionCookieError
from app.common.core.logging import logger


def verify_firebase_session(request: Request):
    # ENV = os.getenv("ENV", "development")

    # if ENV != "production":
    #     return {
    #         "uid": "test_user",
    #         "email": "test@example.com",
    #         "dev": True
    #     }

    session_cookie = request.cookies.get("session")
    if not session_cookie:
        raise HTTPException(status_code=401, detail="未登录或 Session 缺失")

    try:
        decoded_claims = auth.verify_session_cookie(session_cookie, check_revoked=True)
        return decoded_claims
    except auth.InvalidSessionCookieError:
        raise HTTPException(status_code=401, detail="Session 已失效，请重新登录")
    except Exception as e:
        logger.exception("验证 Firebase Session Cookie 失败")
        raise HTTPException(status_code=400, detail=f"验证 Session 时出错：{str(e)}")
    
def get_current_user_id(request: Request):
    session_cookie = request.cookies.get("session")
    if not session_cookie:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        decoded = auth.verify_session_cookie(session_cookie, check_revoked=True)
        return decoded["email"]
    except Exception:
        raise HTTPException(status_code=401, detail="Session 无效")