#!/bin/bash

# 确保目录结构存在
mkdir -p logs static/subtitles static/transcripts tmp/audio

# 检查虚拟环境
if [ -d "venv" ]; then
    echo "激活虚拟环境..."
    source venv/bin/activate
fi

# 检查环境变量文件
if [ ! -f ".env" ]; then
    echo "环境变量文件不存在，正在从示例文件创建..."
    cp .env.example .env
    echo "请编辑 .env 文件，填入您的API密钥"
    exit 1
fi

# 启动应用
echo "启动API服务..."
uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000 