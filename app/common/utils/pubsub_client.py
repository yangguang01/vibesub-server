import os, json, logging, asyncio
from google.cloud import pubsub_v1
from google.api_core.exceptions import GoogleAPIError
from app.worker.processor import process_translation_task  # 你的核心业务逻辑
from app.common.core.config import GCP_PROJECT_ID, PUBSUB_TOPIC

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# —— Pub/Sub 客户端初始化 ——  
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(GCP_PROJECT_ID, PUBSUB_TOPIC)

def publish_translation_task(payload: dict):
    """
    HTTP /tasks 中调用，把任务下发到 Pub/Sub Topic
    """
    try:
        data = json.dumps(payload).encode("utf-8")
        message_id = publisher.publish(topic_path, data=data).result(timeout=10)
        logger.info(f"Published message {message_id} to {topic_path}")
        return message_id
    except GoogleAPIError as e:
        logger.error("Failed to publish to Pub/Sub", exc_info=e)
        raise

# —— 同时把 process_translation_task 也导出到 utils，供 pubsub_push 调用 ——  
# (可选：如果你喜欢把所有“入口”都放在 utils)