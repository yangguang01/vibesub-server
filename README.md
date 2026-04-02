# YouTube字幕翻译API

一个基于FastAPI的YouTube视频字幕翻译服务，支持将英文字幕转写并翻译为中文。

## 功能特点

- 从YouTube链接获取视频并提取音频
- 使用Whisper模型进行语音识别和转写
- 将转写的英文字幕翻译为中文
- 处理字幕时间轴，生成标准SRT格式字幕文件
- 异步任务处理，支持长时间运行的翻译任务
- 提供REST API接口，方便集成到其他应用

## 安装与设置

### 依赖项

- Python 3.8+
- FFmpeg (用于音频处理)

### 步骤

1. 克隆仓库

```bash
git clone <repository-url>
cd youtube-subtitle-translator
```

2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows
```

3. 安装依赖

```bash
pip install -r requirements.txt
```

4. 设置环境变量

复制示例环境变量文件并填入您的API密钥：

```bash
cp .env.example .env
```

编辑`.env`文件，至少设置以下密钥：

- `OPENAI_API_KEY`
- `DEEPSEEK_API_KEY`
- `MOONSHOT_API_KEY`
- `ASSEMBLYAI_API_KEY`

LLM 的默认厂商、模型、温度等参数统一写在 `config/external_services.toml` 中。
通常只需要改配置文件即可，不需要额外传一堆环境变量。

`MAX_CONCURRENT_TASKS`、`RETRY_ATTEMPTS`、`BATCH_SIZE` 这类运行参数现在也有配置文件默认值，
只有在需要临时覆盖时，才需要在环境变量里设置。

如果只是临时覆盖某个子任务的配置，再使用环境变量即可，例如：

```bash
MOONSHOT_BASE_URL=https://api.moonshot.cn/v1
LLM_CONTEXT_PROVIDER=kimi
LLM_CONTEXT_MODEL=kimi-k2.5
LLM_ALIGNMENT_FALLBACK_CHUNK_SIZE=8
```

当前支持按子任务分别配置：

- `translation`
- `context`
- `alignment`
- `sentence_splitter`

如果需要查看“如何通过 Cloud Run 环境变量覆盖 `external_services.toml` 默认值”，见：
[docs/env_override_guide_2026-04-02.md](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/docs/env_override_guide_2026-04-02.md)

## 启动服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

或使用Python直接运行：

```bash
python -m app.main
```

服务将在 http://localhost:8000 启动，API文档可在 http://localhost:8000/docs 查看。

## API使用说明

### 创建翻译任务

```bash
curl -X POST "http://localhost:8000/api/translate" \
     -H "Content-Type: application/json" \
     -d '{"youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID", "custom_prompt": "这是一个科技视频"}'
```

### 获取任务状态

```bash
curl "http://localhost:8000/api/tasks/{task_id}"
```

### 下载字幕文件

```bash
curl "http://localhost:8000/api/subtitles/{task_id}" -o subtitle.srt
```

## 更新

### 05/06 2.6版本更新
1.新增AI翻译策略相关代码
2.代理地址配置化
3.优化句子分割方式
4.下载改为异步

### 04/17 2.4版本更新

1.增加模型选择功能
2.支持openai GPT4.1 mini
3.将分割功能使用openai GPT4.1 mini实现

### 04/20 2.5版本更新

1.使用新的ASR服务 

### 04/02 3.x 配置化 LLM 更新

1.新增 Moonshot Kimi 2.5 官方 API 接入
2.LLM 选择改为配置文件默认生效
3.支持通过环境变量按需覆盖 `translation / context / alignment / sentence_splitter`
4.不再通过请求里的 `model` 字段决定实际运行模型
