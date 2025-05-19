import os
from google.cloud import pubsub_v1

# 从环境变量读取项目 ID 和主题名称
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "tube-trans-tasks")

# 初始化 Pub/Sub 发布客户端
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, PUBSUB_TOPIC) 