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
        options = {"storageBucket": STORAGE_BUCKET} if STORAGE_BUCKET else None
        credential_path = GOOGLE_APPLICATION_CREDENTIALS.strip()

        try:
            if credential_path:
                credential_file = Path(credential_path)
                if credential_file.exists():
                    logger.info(f"使用服务账号文件初始化 Firebase: {credential_file}")
                    cred = credentials.Certificate(credential_file)
                    if options:
                        return initialize_app(cred, options)
                    return initialize_app(cred)
                logger.warning(f"Firebase 凭证文件不存在，改用默认凭证初始化: {credential_file}")
            else:
                logger.info("未提供 Firebase 凭证文件，尝试使用默认凭证初始化")

            if options:
                return initialize_app(options=options)
            return initialize_app()
        except Exception as exc:
            logger.error(f"Firebase 初始化失败: {exc}", exc_info=True)
            return None


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
