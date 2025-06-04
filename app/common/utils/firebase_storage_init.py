import os
import firebase_admin
from firebase_admin import credentials

# 1. 在程序启动时（只执行一次）初始化 Firebase Admin SDK 并绑定默认 bucket
if not firebase_admin._apps:
    cred = credentials.Certificate(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    firebase_admin.initialize_app(cred, {
        "storageBucket": "tube-trans.firebasestorage.app"   # 替换成你的 bucket 名
    })