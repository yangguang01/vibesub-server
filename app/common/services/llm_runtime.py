import asyncio
import json
from typing import Any, Dict, Optional

import openai
from openai import AsyncOpenAI, OpenAI

from app.common.core.config import RETRY_ATTEMPTS, get_llm_task_config
from app.common.core.logging import logger


class RetryableLLMError(Exception):
    pass


class NonRetryableLLMError(Exception):
    pass


def resolve_provider_override(model_choice: str | None) -> Optional[str]:
    # 请求里的 model 字段仅保留兼容性，不再参与运行时路由。
    return None


def get_llm_task_runtime(task_name: str, provider_override: str | None = None) -> Dict[str, Any]:
    _ = provider_override
    config = get_llm_task_config(task_name)
    if not config.get("api_key"):
        raise NonRetryableLLMError(f"任务 {task_name} 缺少 API Key")
    base_url = config.get("base_url") or None
    config["client"] = AsyncOpenAI(
        api_key=config["api_key"],
        base_url=base_url,
        timeout=config.get("timeout"),
    )
    return config


def get_sync_llm_task_runtime(task_name: str, provider_override: str | None = None) -> Dict[str, Any]:
    _ = provider_override
    config = get_llm_task_config(task_name)
    if not config.get("api_key"):
        raise NonRetryableLLMError(f"任务 {task_name} 缺少 API Key")
    base_url = config.get("base_url") or None
    config["client"] = OpenAI(
        api_key=config["api_key"],
        base_url=base_url,
        timeout=config.get("timeout"),
    )
    return config


async def safe_json_chat_completion(
    task_name: str,
    messages: list[dict[str, str]],
    *,
    provider_override: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    frequency_penalty: float = 0,
    presence_penalty: float = 0,
    max_attempts: int | None = None,
) -> Dict[str, Any]:
    runtime = get_llm_task_runtime(task_name, provider_override=provider_override)
    client = runtime["client"]
    max_attempts = max_attempts or RETRY_ATTEMPTS

    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.chat.completions.create(
                model=runtime["model"],
                response_format={"type": "json_object"},
                messages=messages,
                temperature=runtime.get("temperature", 0.3) if temperature is None else temperature,
                top_p=runtime.get("top_p", 0.7) if top_p is None else top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
            )
            content = response.choices[0].message.content
            parsed = json.loads(content)
            return {"content": content, "parsed": parsed, "runtime": runtime}
        except (
            openai.AuthenticationError,
            openai.BadRequestError,
            openai.PermissionDeniedError,
        ) as exc:
            logger.error(f"LLM 调用不可恢复错误 task={task_name}: {exc}")
            raise NonRetryableLLMError(str(exc)) from exc
        except (json.JSONDecodeError,) as exc:
            logger.warning(f"LLM 返回非法 JSON task={task_name}, attempt={attempt}: {exc}")
            if attempt == max_attempts:
                raise RetryableLLMError(str(exc)) from exc
        except (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        ) as exc:
            logger.warning(f"LLM 临时错误 task={task_name}, attempt={attempt}: {exc}")
            if attempt == max_attempts:
                raise RetryableLLMError(str(exc)) from exc
        except Exception as exc:
            logger.error(f"LLM 未知错误 task={task_name}, attempt={attempt}: {exc}")
            if attempt == max_attempts:
                raise RetryableLLMError(str(exc)) from exc

        await asyncio.sleep(min(2 ** (attempt - 1), 8))

    raise RetryableLLMError(f"任务 {task_name} 达到最大重试次数")
