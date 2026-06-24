import asyncio
import json
from functools import wraps

import aiohttp

from app.common.core.config import RETRY_ATTEMPTS
from app.common.core.logging import logger


def async_retry(max_attempts=None, exceptions=None):
    """智能异步函数重试装饰器"""
    if max_attempts is None:
        max_attempts = RETRY_ATTEMPTS
    if exceptions is None:
        exceptions = (Exception,)

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    error_msg = str(e).lower()

                    # 🔥 智能错误分类：某些错误不值得重试
                    non_retryable_errors = [
                        "invalid api key", "authentication failed", "permission denied",
                        "model not found", "invalid request", "quota exceeded",
                        "content policy violation", "invalid json", "malformed request"
                    ]

                    if any(err in error_msg for err in non_retryable_errors):
                        logger.error(f"检测到不可重试错误: {str(e)}")
                        raise e

                    # 🔥 动态调整等待时间
                    if "rate limit" in error_msg or "too many requests" in error_msg:
                        # 限流错误：更长等待时间
                        wait_time = min(5 * (2 ** attempt), 60)
                    elif "timeout" in error_msg or "connection" in error_msg:
                        # 网络错误：标准等待时间
                        wait_time = min(2 * (2 ** attempt), 16)
                    else:
                        # 其他错误：快速重试
                        wait_time = min(1 * (2 ** attempt), 8)

                    if attempt < max_attempts - 1:  # 不是最后一次尝试
                        logger.warning(f"尝试 {attempt+1}/{max_attempts} 失败: {str(e)}，等待 {wait_time}秒后重试")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"所有重试已用尽，最终失败: {str(e)}")

            # 所有重试都失败了
            raise last_exception or Exception("最大重试次数已用尽")
        return wrapper
    return decorator


def async_retry_always(max_attempts=None, exceptions=None):
    """异步函数的重试装饰器"""
    if max_attempts is None:
        max_attempts = RETRY_ATTEMPTS  # 替换为直接使用RETRY_ATTEMPTS配置，而不是CONFIG字典
    if exceptions is None:
        exceptions = (aiohttp.ClientError, json.JSONDecodeError, Exception)  # 修改为合适的异常类型

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    # 指数退避策略
                    wait_time = min(1 * (2 ** attempt), 8)  # 使用固定的退避策略参数
                    logger.warning(f"尝试 {attempt+1}/{max_attempts} 失败: {str(e)}，等待 {wait_time}秒后重试")
                    await asyncio.sleep(wait_time)
            # 所有重试都失败了
            logger.error(f"达到最大重试次数 {max_attempts}，最后错误: {str(last_exception)}")
            raise last_exception or Exception("最大重试次数已用尽")
        return wrapper
    return decorator
