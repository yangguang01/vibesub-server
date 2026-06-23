import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 基础路径
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 目录配置
LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"
SUBTITLES_DIR = STATIC_DIR / "subtitles"
TRANSCRIPTS_DIR = STATIC_DIR / "transcripts"
TMP_DIR = BASE_DIR / "tmp"
AUDIO_DIR = TMP_DIR / "audio"

# API配置
API_PREFIX = "/api"
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1")

# 第三方API配置
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_kEY","")
PROXY_URL = os.getenv("PROXY_URL", "")

# API超时配置 - 优化后的超时设置
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "1200"))  # 默认1200秒（向后兼容）

# 🔥 新增分阶段超时配置
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))      # 下载：5分钟
TRANSCRIPTION_TIMEOUT = int(os.getenv("TRANSCRIPTION_TIMEOUT", "300"))  # 转写：5分钟  
TRANSLATION_TIMEOUT = int(os.getenv("TRANSLATION_TIMEOUT", "300"))    # 翻译：5分钟
TOTAL_TASK_TIMEOUT = int(os.getenv("TOTAL_TASK_TIMEOUT", "900"))      # 总超时：15分钟

# 文件保留配置
SUBTITLES_RETENTION_DAYS = int(os.getenv("SUBTITLES_RETENTION_DAYS", "7"))
TRANSCRIPTS_RETENTION_DAYS = int(os.getenv("TRANSCRIPTS_RETENTION_DAYS", "7"))

# 功能配置
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))  # 仅用于中文长句拆分(llm_batches_split)，勿用于翻译
# 翻译专用批大小：批越小，LLM 越不容易漏行/合并 → P000 错位率越低。
# 各批是 asyncio.gather 并发跑的，批小不会拉长总时长。与拆分路径的 BATCH_SIZE 分开，互不影响。
TRANSLATE_BATCH_SIZE = int(os.getenv("TRANSLATE_BATCH_SIZE", "10"))

# 用量限制
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "5"))

# pubsub配置
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
SERVICE_URL = os.getenv("SERVICE_URL")

#Firebase
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")