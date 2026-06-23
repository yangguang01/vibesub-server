"""
P0-timeout 端点行为测试：app/api/cloud_tasks.py

覆盖：
1. 端点把翻译丢后台、立刻返回 200（accepted），不在请求里等任务跑完。
2. 后台任务正常完成 → 不写 failed。
3. 后台任务抛任意异常 → _safe_run_translation 落 failed 态。
4. 后台任务 TimeoutError → 落 failed 态。
5. 缺字段 / 坏 JSON → 400。
"""
import asyncio
import time
import types

import pytest

import app.api.cloud_tasks as ep


class _FakeRequest:
    def __init__(self, payload, raise_on_json=False):
        self._payload = payload
        self._raise = raise_on_json

    async def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._payload


def _run(coro):
    return asyncio.run(coro)


def _valid_payload():
    return {
        "youtube_url": "https://youtu.be/abc",
        "user_id": "u1",
        "video_id": "vid123",
        "content_name": "Test Video",
    }


def test_endpoint_returns_200_immediately(monkeypatch):
    """端点应立刻返回 accepted，且不等待长任务跑完。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_create_translation_task(**kwargs):
        started.set()
        # 模拟长任务：直到测试放行才结束
        await release.wait()

    monkeypatch.setattr(ep, "create_translation_task", slow_create_translation_task)
    # 隔离 Firestore：后台任务结束后不去碰真实数据库
    monkeypatch.setattr(ep, "get_video_task", lambda vid: None)
    monkeypatch.setattr(ep, "update_video_task", lambda *a, **k: None)

    async def scenario():
        t0 = time.monotonic()
        resp = await ep.process_translation_task(_FakeRequest(_valid_payload()))
        elapsed = time.monotonic() - t0
        # 立刻返回，不应阻塞在长任务上
        assert elapsed < 0.5
        assert resp["status"] == "accepted"
        assert resp["video_id"] == "vid123"
        # 后台任务确实被调度起来了
        await asyncio.wait_for(started.wait(), timeout=1)
        # 被模块级 set 引用着，防 GC
        assert len(ep._background_tasks) >= 1
        # 放行后台任务并等待其结束
        release.set()
        await asyncio.sleep(0)
        # 等后台任务清理完
        for _ in range(50):
            if not ep._background_tasks:
                break
            await asyncio.sleep(0.01)

    _run(scenario())


def test_background_success_does_not_mark_failed(monkeypatch):
    calls = []

    async def ok_task(**kwargs):
        return kwargs["video_id"]

    monkeypatch.setattr(ep, "create_translation_task", ok_task)
    monkeypatch.setattr(ep, "get_video_task", lambda vid: None)
    monkeypatch.setattr(
        ep, "update_video_task", lambda *a, **k: calls.append((a, k))
    )

    _run(ep._safe_run_translation(_valid_payload()))
    assert calls == [], "成功路径不应写 failed"


def test_background_exception_marks_failed(monkeypatch):
    recorded = {}

    async def boom_task(**kwargs):
        raise RuntimeError("translation exploded")

    def fake_update(video_id, status, *a, **k):
        recorded["video_id"] = video_id
        recorded["status"] = status
        recorded["error"] = a[-1] if a else None

    monkeypatch.setattr(ep, "create_translation_task", boom_task)
    monkeypatch.setattr(ep, "get_video_task", lambda vid: None)
    monkeypatch.setattr(ep, "update_video_task", fake_update)

    _run(ep._safe_run_translation(_valid_payload()))
    assert recorded["status"] == "failed"
    assert recorded["video_id"] == "vid123"
    assert "translation exploded" in str(recorded["error"])


def test_background_timeout_marks_failed(monkeypatch):
    recorded = {}

    async def hang_task(**kwargs):
        await asyncio.sleep(10)

    def fake_update(video_id, status, *a, **k):
        recorded["status"] = status
        recorded["error"] = a[-1] if a else None

    monkeypatch.setattr(ep, "create_translation_task", hang_task)
    monkeypatch.setattr(ep, "get_video_task", lambda vid: None)
    monkeypatch.setattr(ep, "update_video_task", fake_update)
    # 把总超时压到极短以便快速触发 TimeoutError
    monkeypatch.setattr(ep, "TOTAL_TASK_TIMEOUT", 1)

    async def scenario():
        # asyncio.wait_for 会在 1s 后抛 TimeoutError；为加速，直接 patch wait_for
        orig = asyncio.wait_for

        async def fast_wait_for(coro, timeout):
            # 立刻取消并抛超时
            fut = asyncio.ensure_future(coro)
            fut.cancel()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
        await ep._safe_run_translation(_valid_payload())

    _run(scenario())
    assert recorded["status"] == "failed"
    assert "超时" in str(recorded["error"])


def test_completed_video_skipped_on_exec_side(monkeypatch):
    """执行侧幂等：领到任务时该 video 已 completed → 不跑翻译、不写状态。"""
    ran = {"called": False}

    async def should_not_run(**kwargs):
        ran["called"] = True

    updates = []
    monkeypatch.setattr(ep, "create_translation_task", should_not_run)
    monkeypatch.setattr(ep, "get_video_task", lambda vid: {"status": "completed"})
    monkeypatch.setattr(ep, "update_video_task", lambda *a, **k: updates.append(a))

    _run(ep._safe_run_translation(_valid_payload()))
    assert ran["called"] is False
    assert updates == []


def test_missing_fields_returns_400():
    bad = {"youtube_url": "x"}  # 缺 user_id/video_id/content_name
    with pytest.raises(Exception) as exc:
        _run(ep.process_translation_task(_FakeRequest(bad)))
    assert getattr(exc.value, "status_code", None) == 400


def test_bad_json_returns_400():
    with pytest.raises(Exception) as exc:
        _run(ep.process_translation_task(_FakeRequest(None, raise_on_json=True)))
    assert getattr(exc.value, "status_code", None) == 400
