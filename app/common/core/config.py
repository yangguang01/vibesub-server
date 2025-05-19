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

# API超时配置
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "1200"))  # 默认120秒

# 文件保留配置
SUBTITLES_RETENTION_DAYS = int(os.getenv("SUBTITLES_RETENTION_DAYS", "7"))
TRANSCRIPTS_RETENTION_DAYS = int(os.getenv("TRANSCRIPTS_RETENTION_DAYS", "7"))

# 功能配置
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50")) 