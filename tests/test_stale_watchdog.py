"""
P0-timeout 看门狗测试：app/common/models/firestore_models.py

覆盖 is_stale_processing / fail_if_stale_processing：
- 陈旧 processing → 判失败
- 新鲜 processing → 不判
- completed / failed → 不判（即便时间戳很旧）
- 缺时间戳 / 缺数据 → 保守不判
- fail_if_stale_processing 对陈旧任务会调用 update_video_task(..., failed, ...)
"""
from datetime import datetime, timedelta, timezone

import app.common.models.firestore_models as fm

STALE = 30


def _ts(minutes_ago):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def test_stale_processing_is_stale():
    task = {"status": "processing", "updated_at": _ts(45)}
    assert fm.is_stale_processing(task, STALE) is True


def test_fresh_processing_not_stale():
    task = {"status": "processing", "updated_at": _ts(5)}
    assert fm.is_stale_processing(task, STALE) is False


def test_completed_never_stale():
    task = {"status": "completed", "updated_at": _ts(999)}
    assert fm.is_stale_processing(task, STALE) is False


def test_failed_never_stale():
    task = {"status": "failed", "updated_at": _ts(999)}
    assert fm.is_stale_processing(task, STALE) is False


def test_strategies_ready_can_be_stale():
    # strategies_ready 是中间活跃态，长时间不动也应被看门狗回收
    task = {"status": "strategies_ready", "updated_at": _ts(45)}
    assert fm.is_stale_processing(task, STALE) is True


def test_missing_timestamp_not_stale():
    task = {"status": "processing"}
    assert fm.is_stale_processing(task, STALE) is False


def test_none_task_not_stale():
    assert fm.is_stale_processing(None, STALE) is False


def test_naive_datetime_treated_as_utc():
    naive = datetime.utcnow() - timedelta(minutes=45)  # 无 tzinfo
    task = {"status": "processing", "updated_at": naive}
    assert fm.is_stale_processing(task, STALE) is True


def test_falls_back_to_created_at():
    task = {"status": "processing", "created_at": _ts(45)}
    assert fm.is_stale_processing(task, STALE) is True


def test_fail_if_stale_marks_failed(monkeypatch):
    recorded = {}

    snapshots = iter(
        [
            {"status": "processing", "updated_at": _ts(45)},  # 第一次读
            {"status": "failed", "error": "x"},               # 置 failed 后再读
        ]
    )
    monkeypatch.setattr(fm, "get_video_task", lambda vid: next(snapshots))

    def fake_update(video_id, status, *a, **k):
        recorded["video_id"] = video_id
        recorded["status"] = status
        recorded["error"] = k.get("error")

    monkeypatch.setattr(fm, "update_video_task", fake_update)

    result = fm.fail_if_stale_processing("vidX", STALE)
    assert recorded["status"] == "failed"
    assert recorded["video_id"] == "vidX"
    assert result["status"] == "failed"


def test_fail_if_stale_leaves_fresh_alone(monkeypatch):
    called = {"update": False}
    monkeypatch.setattr(
        fm, "get_video_task", lambda vid: {"status": "processing", "updated_at": _ts(2)}
    )
    monkeypatch.setattr(
        fm, "update_video_task", lambda *a, **k: called.__setitem__("update", True)
    )
    result = fm.fail_if_stale_processing("vidX", STALE)
    assert called["update"] is False
    assert result["status"] == "processing"


def test_fail_if_stale_none_task(monkeypatch):
    monkeypatch.setattr(fm, "get_video_task", lambda vid: None)
    assert fm.fail_if_stale_processing("missing", STALE) is None
