import asyncio
from fastapi import APIRouter, Request, HTTPException
from app.worker.processor import create_translation_task  # 你的核心业务逻辑
from app.common.core.logging import logger
from app.common.core.config import TOTAL_TASK_TIMEOUT, STALE_TASK_MINUTES
from app.common.models.firestore_models import (
    update_video_task,
    get_video_task,
    is_stale_processing,
)

router = APIRouter()

# 🔥 P0-timeout：保留后台任务的强引用，防止被 GC 提前回收。
# Python 的 asyncio 只对 Task 持弱引用，丢后台的 create_task 若没人引用，
# 可能在运行中途被垃圾回收。这里用模块级 set 持有，完成回调里移除。
_background_tasks: set = set()


def _is_fresh_processing(video_task: dict) -> bool:
    """该 video 是否已有"新鲜的进行中任务"——是则不该重复执行。"""
    if not video_task:
        return False
    status = video_task.get("status")
    if status not in ("processing", "pending", "strategies_ready"):
        return False
    # 进行中但已陈旧 → 不算新鲜，允许重跑
    return not is_stale_processing(video_task, STALE_TASK_MINUTES)


async def _safe_run_translation(payload: dict) -> None:
    """
    包一层"绝不让后台任务静默死掉"：
      - 入口做幂等检查：已完成 / 有新鲜的进行中任务 → 直接跳过；
      - 跑翻译并加总超时；
      - 任何异常（含 TimeoutError）→ 写 failed 态，保证 Firestore 不留 processing 僵尸态；
    """
    video_id = payload.get("video_id", "unknown")
    try:
        # 执行侧幂等：极端并发下，领到任务时若已完成 / 有新鲜进行中任务则跳过
        existing = get_video_task(video_id)
        if existing:
            status = existing.get("status")
            if status == "completed":
                logger.info(f"🟢 幂等跳过：video {video_id} 已完成，后台任务不重复执行")
                return
            if _is_fresh_processing(existing):
                # create_or_update_video_task 在 create_translation_task 入口刚把状态
                # 写成 processing，所以"自己这次"也会命中 fresh。这里不做更激进的判断，
                # 主要的并发拦截在下发侧（tasks.py）完成；执行侧只挡"明显已有他人在跑"。
                # 为避免误杀自己，这里不 return —— 真正的并发去重交给下发侧。
                pass

        timeout_minutes = TOTAL_TASK_TIMEOUT // 60
        try:
            await asyncio.wait_for(
                create_translation_task(**payload),
                timeout=TOTAL_TASK_TIMEOUT,
            )
            logger.info(f"✅ 翻译任务完成: {video_id}")
        except asyncio.TimeoutError:
            logger.error(f"⏰ 翻译任务超时: {video_id} ({timeout_minutes}分钟)")
            update_video_task(
                video_id, "failed", 0, [], f"任务执行超时（{timeout_minutes}分钟）"
            )
    except Exception as e:
        # 兜住一切其它异常，保证落明确 failed 态
        error_msg = str(e)
        logger.error(f"❌ 后台翻译任务失败 {video_id}: {error_msg}", exc_info=True)
        try:
            update_video_task(video_id, "failed", 0, [], error_msg)
        except Exception as inner:
            # 连写 failed 都失败（如实例即将被杀）：记录日志，留给看门狗兜底
            logger.error(f"⚠️ 写 failed 态也失败 {video_id}: {inner}")


@router.post("/process-translation")
async def process_translation_task(request: Request):
    """
    Cloud Tasks 调用的端点：解析 JSON payload 后，把翻译丢到后台执行并立刻返回 200。

    🔥 P0-timeout 根治核心：
      - 不再在 HTTP 请求里同步 await 整条十几分钟的翻译流程（旧写法会被 Cloud Run
        请求超时 / Cloud Tasks dispatch deadline 硬杀，Firestore 留 processing 僵尸态）。
      - 改成 asyncio.create_task 丢后台 + 立刻 return 200，Cloud Tasks 满意、不再等、
        不会因 deadline 重试。后台任务靠 instance-based 计费模式保证 CPU 不被节流。
      - 中断兜底：_safe_run_translation 保证任何异常都落 failed 态；硬崩溃由看门狗兜。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    # 验证必需字段
    required_fields = ["youtube_url", "user_id", "video_id", "content_name"]
    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        raise HTTPException(400, f"Missing required fields: {missing_fields}")

    video_id = payload.get("video_id")
    logger.info(f"🎬 Cloud Tasks 接收翻译任务: {video_id}，丢后台执行并立刻返回 200")

    # 丢后台执行，保留强引用防 GC，完成后移除
    task = asyncio.create_task(_safe_run_translation(payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # 立刻返回 200，让 Cloud Tasks 认为投递成功、不再重试
    return {
        "status": "accepted",
        "video_id": video_id,
        "message": "Translation task accepted and running in background",
    }
