import os
from google.cloud import storage

# 从环境变量读取项目 ID 和存储桶名称
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "tube-trans-storage")

# 初始化 Storage 客户端
storage_client = storage.Client(project=PROJECT_ID)
# 获取目标存储桶
bucket = storage_client.bucket(STORAGE_BUCKET) 