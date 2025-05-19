FROM alpine:latest

WORKDIR /app

# 安装Python和依赖
RUN apk add --no-cache python3 py3-pip gcc python3-dev musl-dev libffi-dev

# 创建虚拟环境
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# 安装依赖
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ ./app/


# 创建必要的目录
RUN mkdir -p logs data/tasks tmp/audio static/subtitles static/transcripts

# 设置环境变量
ENV PYTHONPATH=/app
ENV PORT=8080

# 暴露端口
EXPOSE 8080

# 启动应用
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"] 