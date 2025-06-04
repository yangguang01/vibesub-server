# 使用官方 Python 3.11 Alpine 版作为基础镜像
FROM python:3.11-alpine

# 设置工作目录
WORKDIR /app

# 安装系统依赖（用于编译某些 Python 扩展）
RUN apk add --no-cache build-base libffi-dev

# 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# 复制整个 app 目录（包含 app/api/main.py 及其他模块）
COPY app/ ./app/


# 设置环境变量
ENV PYTHONPATH=/app
ENV PORT=8080

# 暴露监听端口
EXPOSE 8080

# 启动命令：使用 Uvicorn 加载 app/api/main.py 中定义的 FastAPI app 实例
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8080"]