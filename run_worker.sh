#!/bin/bash

mkdir -p logs static/subtitles static/transcripts tmp/audio

if [ -d "venv" ]; then
    source venv/bin/activate
fi

if [ ! -f ".env" ]; then
    echo "环境变量文件不存在，请先创建 .env"
    exit 1
fi

echo "启动 Worker 服务..."
uvicorn app.worker.main:app --reload --host 0.0.0.0 --port 8001
