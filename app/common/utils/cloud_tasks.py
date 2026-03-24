import json
from typing import Any, Dict

from google.cloud import tasks_v2

from app.common.core.config import CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL, WORKER_SERVICE_AUDIENCE, get_task_config
from app.common.core.logging import logger
from app.common.services.infrastructure import get_cloud_tasks_client


class CloudTasksManager:
    def __init__(self):
        self.client = None

    def _client(self):
        if self.client is None:
            self.client = get_cloud_tasks_client()
        return self.client

    def create_translation_task(self, payload: Dict[str, Any]) -> str:
        config = get_task_config("translation_dispatch")
        target_url = f"{config['target_url'].rstrip('/')}{config['path']}"
        client = self._client()
        parent = client.queue_path(config["project"], config["location"], config["queue"])

        http_request: Dict[str, Any] = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": target_url,
            "body": json.dumps(payload).encode("utf-8"),
            "headers": {"Content-Type": "application/json"},
        }

        if config.get("auth_mode") == "oidc" and CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:
            http_request["oidc_token"] = {
                "service_account_email": CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL,
                "audience": WORKER_SERVICE_AUDIENCE or target_url,
            }

        response = client.create_task(parent=parent, task={"http_request": http_request})
        logger.info(f"Cloud Task 创建成功: {response.name}")
        return response.name


tasks_manager = CloudTasksManager()


def create_translation_cloud_task(payload: Dict[str, Any]) -> str:
    return tasks_manager.create_translation_task(payload)
