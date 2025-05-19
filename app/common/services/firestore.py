import os
from google.cloud import firestore

# 从环境变量读取项目 ID
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")

# 初始化 Firestore 客户端
db = firestore.Client(project=PROJECT_ID) 