import os
from pathlib import Path

from dotenv import load_dotenv

from app.common.core.external_services import ExternalServiceRegistry

load_dotenv()

REPO_DIR = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[2]

EXTERNAL_SERVICES_PATH = REPO_DIR / "config" / "external_services.toml"
external_services = ExternalServiceRegistry(EXTERNAL_SERVICES_PATH)

LOGS_DIR = APP_DIR / "logs"
STATIC_DIR = APP_DIR / "static"
SUBTITLES_DIR = STATIC_DIR / "subtitles"
TRANSCRIPTS_DIR = STATIC_DIR / "transcripts"
TMP_DIR = APP_DIR / "tmp"
AUDIO_DIR = TMP_DIR / "audio"

APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
API_PREFIX = "/api"
INTERNAL_API_PREFIX = "/internal"
DEBUG = os.getenv("DEBUG", "false").lower() in {"true", "1", "yes"}

API_TIMEOUT = int(os.getenv("API_TIMEOUT", "1200"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
TRANSCRIPTION_TIMEOUT = int(os.getenv("TRANSCRIPTION_TIMEOUT", "300"))
TRANSLATION_TIMEOUT = int(os.getenv("TRANSLATION_TIMEOUT", "300"))
TOTAL_TASK_TIMEOUT = int(os.getenv("TOTAL_TASK_TIMEOUT", "900"))

SUBTITLES_RETENTION_DAYS = int(os.getenv("SUBTITLES_RETENTION_DAYS", "7"))
TRANSCRIPTS_RETENTION_DAYS = int(os.getenv("TRANSCRIPTS_RETENTION_DAYS", "7"))

MAX_CONCURRENT_TASKS = int(external_services.get_runtime_value("max_concurrent_tasks", env_name="MAX_CONCURRENT_TASKS", default=5))
RETRY_ATTEMPTS = int(external_services.get_runtime_value("retry_attempts", env_name="RETRY_ATTEMPTS", default=2))
BATCH_SIZE = int(external_services.get_runtime_value("batch_size", env_name="BATCH_SIZE", default=50))
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "5"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY", "")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")

GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
STORAGE_BUCKET = os.getenv(
    "STORAGE_BUCKET",
    external_services.get_task_config("subtitle_storage").get("bucket", ""),
)
API_SERVICE_URL = os.getenv("API_SERVICE_URL", "")
WORKER_SERVICE_URL = os.getenv("WORKER_SERVICE_URL", "")
PROXY_URL = os.getenv("PROXY_URL", "")

CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL = os.getenv("CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL", "")
WORKER_SERVICE_AUDIENCE = os.getenv("WORKER_SERVICE_AUDIENCE", WORKER_SERVICE_URL)
INTERNAL_AUTH_ENABLED = os.getenv("INTERNAL_AUTH_ENABLED", "true").lower() in {"true", "1", "yes"}


def get_task_config(task_name: str, provider_override: str | None = None):
    return external_services.get_task_config(task_name, provider_override=provider_override)


def get_llm_task_config(task_name: str):
    return external_services.get_llm_task_config(task_name)


def get_provider_config(provider_name: str):
    return external_services.get_provider(provider_name)
