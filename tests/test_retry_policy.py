import asyncio
import json

import pytest

from app.common.utils import retry
from app.common.services import sentence_splitter, translation, video_download


async def _instant_sleep(_seconds):
    return None


def test_smart_retry_does_not_retry_invalid_json(monkeypatch):
    monkeypatch.setattr(retry.asyncio, "sleep", _instant_sleep)
    calls = {"count": 0}

    @retry.async_retry(max_attempts=3)
    async def flaky():
        calls["count"] += 1
        raise ValueError("invalid json from model")

    with pytest.raises(ValueError):
        asyncio.run(flaky())

    assert calls["count"] == 1


def test_always_retry_retries_json_errors(monkeypatch):
    monkeypatch.setattr(retry.asyncio, "sleep", _instant_sleep)
    calls = {"count": 0}

    @retry.async_retry_always(max_attempts=3)
    async def flaky_then_ok():
        calls["count"] += 1
        if calls["count"] < 3:
            raise json.JSONDecodeError("bad json", "{}", 0)
        return "ok"

    assert asyncio.run(flaky_then_ok()) == "ok"
    assert calls["count"] == 3


def test_llm_call_sites_keep_expected_retry_wrappers():
    assert sentence_splitter.split_safe_api_call_async.__wrapped__
    assert translation.get_video_context_from_llm.__wrapped__
    assert translation.safe_api_call_async.__wrapped__


def test_audio_download_keeps_tenacity_retry_wrapper():
    assert hasattr(video_download.get_video_info_and_download, "retry")
