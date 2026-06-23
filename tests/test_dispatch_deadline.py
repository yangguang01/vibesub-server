"""
P0-timeout：Cloud Tasks dispatch deadline 测试 app/common/utils/cloud_tasks.py

验证 _create_task_sync 构造的 task 体里带上了 dispatch_deadline（timedelta），
值等于 config.CLOUD_TASKS_DISPATCH_DEADLINE 秒。
"""
from datetime import timedelta

import app.common.utils.cloud_tasks as ct
from app.common.core.config import CLOUD_TASKS_DISPATCH_DEADLINE


def test_dispatch_deadline_set(monkeypatch):
    captured = {}

    class _FakeClient:
        def queue_path(self, *a):
            return "queues/test"

        def create_task(self, parent, task):
            captured["task"] = task

            class _R:
                name = "tasks/abc"

            return _R()

    mgr = ct.CloudTasksManager()
    monkeypatch.setattr(mgr, "get_client", lambda: _FakeClient())

    payload = {
        "youtube_url": "https://youtu.be/x",
        "user_id": "u1",
        "video_id": "v1",
        "content_name": "t",
    }
    name = mgr._create_task_sync(payload)
    assert name == "tasks/abc"

    task = captured["task"]
    assert "dispatch_deadline" in task
    assert task["dispatch_deadline"] == timedelta(seconds=CLOUD_TASKS_DISPATCH_DEADLINE)
