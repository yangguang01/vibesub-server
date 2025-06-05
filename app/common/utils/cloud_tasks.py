import json
import asyncio
import logging
from typing import Dict, Any
from google.cloud import tasks_v2
import uuid

from app.common.core.config import GOOGLE_CLOUD_PROJECT, SERVICE_URL

logger = logging.getLogger(__name__)

# 配置常量
LOCATION = "us-west1"
QUEUE_NAME = "vibesub-queue"

class CloudTasksManager:
    def __init__(self):
        self.client = None
    
    def get_client(self):
        """懒加载 Cloud Tasks 客户端"""
        if self.client is None:
            self.client = tasks_v2.CloudTasksClient()
        return self.client
    
    async def create_translation_task_async(self, payload: Dict[str, Any]) -> str:
        """异步创建翻译任务"""
        try:
            # 使用 asyncio.to_thread 让同步调用变异步
            task_name = await asyncio.to_thread(self._create_task_sync, payload)
            logger.info(f"✅ Cloud Task created: {task_name}")
            return task_name
        except Exception as e:
            logger.error(f"❌ Failed to create Cloud Task: {e}")
            raise
    
    def _create_task_sync(self, payload: Dict[str, Any]) -> str:
        """同步创建任务（被异步包装）"""
        client = self.get_client()
        parent = client.queue_path(GOOGLE_CLOUD_PROJECT, LOCATION, QUEUE_NAME)
        
        # 构建任务
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{SERVICE_URL}/api/cloud-tasks/process-translation",
                "body": json.dumps(payload).encode(),
                "headers": {
                    "Content-Type": "application/json",
                    # 可选：添加认证头
                    "User-Agent": "CloudTasks-Translation-Worker"
                },
            }
        }
        
        # 可选：设置任务调度时间（立即执行可以不设置）
        # task["schedule_time"] = {"seconds": int(time.time()) + delay_seconds}
        
        response = client.create_task(parent=parent, task=task)
        return response.name
    
    async def create_task_with_retry(self, payload: Dict[str, Any], max_retries: int = 3) -> str:
        """带重试机制的任务创建"""
        for attempt in range(max_retries):
            try:
                return await self.create_translation_task_async(payload)
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"❌ Failed to create task after {max_retries} attempts: {e}")
                    raise
                else:
                    logger.warning(f"⚠️ Task creation attempt {attempt + 1} failed, retrying: {e}")
                    await asyncio.sleep(2 ** attempt)  # 指数退避

# 全局实例
tasks_manager = CloudTasksManager()

# 便捷函数
async def create_translation_cloud_task(payload: Dict[str, Any]) -> str:
    """创建翻译任务的便捷函数"""
    return await tasks_manager.create_translation_task_async(payload)

async def create_translation_cloud_task_safe(payload: Dict[str, Any]) -> str:
    """带重试的安全创建函数"""
    return await tasks_manager.create_task_with_retry(payload)