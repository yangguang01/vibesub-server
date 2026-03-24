from pathlib import Path
from functools import lru_cache

from firebase_admin import credentials, get_app, initialize_app, storage as firebase_storage
from google.cloud import firestore, tasks_v2

from app.common.core.config import (
    GOOGLE_APPLICATION_CREDENTIALS,
    GOOGLE_CLOUD_PROJECT,
    STORAGE_BUCKET,
)
from app.common.core.logging import logger


@lru_cache
def get_firebase_app():
    try:
        return get_app()
    except ValueError:
        if not GOOGLE_APPLICATION_CREDENTIALS or not Path(GOOGLE_APPLICATION_CREDENTIALS).exists():
            logger.warning("Firebase 凭证文件不存在，当前跳过 Firebase 初始化")
            return None
        cred = credentials.Certificate(GOOGLE_APPLICATION_CREDENTIALS)
        options = {"storageBucket": STORAGE_BUCKET} if STORAGE_BUCKET else None
        if options:
            return initialize_app(cred, options)
        return initialize_app(cred)


@lru_cache
def get_firestore_client():
    return firestore.Client(project=GOOGLE_CLOUD_PROJECT)


@lru_cache
def get_storage_bucket():
    app = get_firebase_app()
    if app is None:
        raise RuntimeError("Firebase 未初始化，无法访问 Storage")
    if STORAGE_BUCKET:
        return firebase_storage.bucket(STORAGE_BUCKET)
    return firebase_storage.bucket()


@lru_cache
def get_cloud_tasks_client():
    return tasks_v2.CloudTasksClient()
