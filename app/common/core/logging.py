import logging
import sys
from pathlib import Path

from app.common.core.config import LOGS_DIR


def setup_logging():
    """设置日志系统"""
    # 确保日志目录存在
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOGS_DIR / "app.log"),
            logging.StreamHandler(sys.stdout)  # 同时输出到控制台
        ]
    )
    
    # 设置各模块的日志级别
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("fastapi").setLevel(logging.INFO)
    
    # 创建应用日志实例
    logger = logging.getLogger("yube-trans")
    
    return logger


# 创建通用日志实例
logger = setup_logging() 