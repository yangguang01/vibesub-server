"""
P0-timeout 下发侧幂等去重测试：app/api/tasks.py 的 translate_video

覆盖：
- completed       → 复用结果，不下发新 Cloud Task
- 新鲜 processing → 不下发新 Cloud Task，直接回当前状态
- 陈旧 processing → 允许重新下发
- failed          → 允许重新下发
- 全新 video      → 下发
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.api.tasks as tasks
from app.common.core.config import STALE_TASK_MINUTES


def _ts(minutes_ago):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


class _FakeRequest:
    youtube_url = "https://youtu.be/vidABC"
    content_name = "Title"
    special_terms = ""
    model = ""


@pytest.fixture
def patched(monkeypatch):
    """打桩 tasks 模块里所有外部调用，返回一个记录器。"""
    state = {
        "dispatched": False,
        "recorded_success": False,
        "user_tasks": [],
        "video_task": None,  # 由各测试设置
    }

    # executor.run_in_executor(executor, fn, *args) → 直接同步调用 fn(*args)
    class _Loop:
        async def run_in_executor(self, _executor, fn, *args):
            return fn(*args)

    monkeypatch.setattr(tasks.asyncio, "get_event_loop", lambda: _Loop())

    monkeypatch.setattr(tasks, "check_user_daily_limit", lambda uid: True)
    monkeypatch.setattr(tasks, "extract_video_id", lambda url: "vidABC")
    monkeypatch.setattr(tasks, "get_video_id_by_yt_dlp", lambda url: "vidABC")

    monkeypatch.setattr(tasks, "get_video_task", lambda vid: state["video_task"])

    def _create_user_task(*a):
        state["user_tasks"].append(a)

    monkeypatch.setattr(tasks, "create_user_task", _create_user_task)
    monkeypatch.setattr(tasks, "create_or_update_video_task", lambda *a, **k: None)

    def _record_success(*a):
        state["recorded_success"] = True

    monkeypatch.setattr(tasks, "record_successful_request", _record_success)

    # 下发侧：把后台 create_cloud_task 的实际网络调用替换为标记
    async def _fake_dispatch(payload):
        state["dispatched"] = True
        return "fake-task-name"

    monkeypatch.setattr(tasks, "create_translation_cloud_task_safe", _fake_dispatch)

    return state


def _call(req=None):
    return asyncio.run(tasks.translate_video(req or _FakeRequest(), user_id="u1"))


async def _drain_background():
    # translate_video 用 asyncio.create_task 后台下发，给它机会跑完
    await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _call_and_drain(req=None):
    async def runner():
        result = await tasks.translate_video(req or _FakeRequest(), user_id="u1")
        await _drain_background()
        return result

    return asyncio.run(runner())


def test_completed_reuses_no_dispatch(patched):
    patched["video_task"] = {"status": "completed"}
    result = _call_and_drain()
    assert result["status"] == "completed"
    assert patched["dispatched"] is False
    assert patched["recorded_success"] is True  # 复用路径记一次成功


def test_fresh_processing_blocks_dispatch(patched):
    patched["video_task"] = {"status": "processing", "updated_at": _ts(3)}
    result = _call_and_drain()
    assert result["status"] == "processing"
    assert patched["dispatched"] is False, "新鲜 processing 不应重复下发"
    assert patched["recorded_success"] is False, "进行中不应重复扣用量"


def test_stale_processing_allows_dispatch(patched):
    patched["video_task"] = {
        "status": "processing",
        "updated_at": _ts(STALE_TASK_MINUTES + 10),
    }
    result = _call_and_drain()
    assert result["status"] == "pending"
    assert patched["dispatched"] is True, "陈旧 processing 应允许重发"


def test_failed_allows_dispatch(patched):
    patched["video_task"] = {"status": "failed", "updated_at": _ts(1)}
    result = _call_and_drain()
    assert result["status"] == "pending"
    assert patched["dispatched"] is True, "失败任务应允许重发"


def test_brand_new_video_dispatches(patched):
    patched["video_task"] = None
    result = _call_and_drain()
    assert result["status"] == "pending"
    assert patched["dispatched"] is True
