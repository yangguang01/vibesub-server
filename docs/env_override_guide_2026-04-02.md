# 环境变量覆盖 `external_services.toml` 指南

本文说明当前项目里，哪些 `config/external_services.toml` 默认值可以通过环境变量覆盖，以及具体怎么配。

## 1. 设计原则

- `config/external_services.toml` 是项目默认值。
- 环境变量只在“当前部署环境想临时改默认值”时使用。
- 环境变量优先级高于 `external_services.toml`。
- 重启服务或发布新的 Cloud Run revision 后，新的环境变量才会生效。

## 2. 当前支持环境变量覆盖的范围

目前支持覆盖两大类：

1. 运行参数：
- `MAX_CONCURRENT_TASKS`
- `RETRY_ATTEMPTS`
- `BATCH_SIZE`

2. LLM 槽位参数：
- `translation`
- `context`
- `alignment`
- `sentence_splitter`

另外，以下 provider 级敏感信息本来就是通过环境变量注入，不写死在 `external_services.toml` 里：

- `OPENAI_API_KEY`
- `DEEPSEEK_API_KEY`
- `MOONSHOT_API_KEY`
- `MOONSHOT_BASE_URL`
- `ASSEMBLYAI_API_KEY`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GOOGLE_CLOUD_PROJECT`
- `STORAGE_BUCKET`
- `WORKER_SERVICE_URL`
- `PROXY_URL`

## 3. 运行参数如何覆盖

`external_services.toml` 默认值：

```toml
[runtime]
max_concurrent_tasks = 5
retry_attempts = 2
batch_size = 50
```

如果要在 Cloud Run 后台覆盖：

```env
MAX_CONCURRENT_TASKS=8
RETRY_ATTEMPTS=4
BATCH_SIZE=80
```

对应关系：

- `runtime.max_concurrent_tasks` -> `MAX_CONCURRENT_TASKS`
- `runtime.retry_attempts` -> `RETRY_ATTEMPTS`
- `runtime.batch_size` -> `BATCH_SIZE`

## 4. LLM 槽位如何覆盖

### 4.1 通用规则

`llm.<slot>` 里的每个字段，都会映射成：

```text
LLM_<SLOT名称大写>_<字段名大写>
```

例如：

- `llm.context.provider` -> `LLM_CONTEXT_PROVIDER`
- `llm.context.model` -> `LLM_CONTEXT_MODEL`
- `llm.alignment.fallback_chunk_size` -> `LLM_ALIGNMENT_FALLBACK_CHUNK_SIZE`

### 4.2 translation

默认配置：

```toml
[llm.translation]
provider = "deepseek"
model = "deepseek-chat"
temperature_main = 0.3
top_p_main = 0.7
temperature_strict = 0.0
top_p_strict = 1.0
temperature_fallback = 0.0
top_p_fallback = 1.0
```

可覆盖环境变量：

```env
LLM_TRANSLATION_PROVIDER=
LLM_TRANSLATION_MODEL=
LLM_TRANSLATION_TEMPERATURE_MAIN=
LLM_TRANSLATION_TOP_P_MAIN=
LLM_TRANSLATION_TEMPERATURE_STRICT=
LLM_TRANSLATION_TOP_P_STRICT=
LLM_TRANSLATION_TEMPERATURE_FALLBACK=
LLM_TRANSLATION_TOP_P_FALLBACK=
```

示例：把翻译主链切到 Kimi

```env
MOONSHOT_API_KEY=你的key
LLM_TRANSLATION_PROVIDER=kimi
LLM_TRANSLATION_MODEL=kimi-k2.5
```

### 4.3 context

默认配置：

```toml
[llm.context]
provider = "deepseek"
model = "deepseek-chat"
temperature = 1.0
top_p = 0.7
```

可覆盖环境变量：

```env
LLM_CONTEXT_PROVIDER=
LLM_CONTEXT_MODEL=
LLM_CONTEXT_TEMPERATURE=
LLM_CONTEXT_TOP_P=
```

示例：只把上下文推断切到 Kimi

```env
MOONSHOT_API_KEY=你的key
LLM_CONTEXT_PROVIDER=kimi
LLM_CONTEXT_MODEL=kimi-k2.5
```

### 4.4 alignment

默认配置：

```toml
[llm.alignment]
provider = "deepseek"
model = "deepseek-chat"
temperature = 0.0
top_p = 1.0
mode = "enforce"
max_validator_retries = 2
max_content_retries = 3
fallback_chunk_size = 12
```

可覆盖环境变量：

```env
LLM_ALIGNMENT_PROVIDER=
LLM_ALIGNMENT_MODEL=
LLM_ALIGNMENT_TEMPERATURE=
LLM_ALIGNMENT_TOP_P=
LLM_ALIGNMENT_MODE=
LLM_ALIGNMENT_MAX_VALIDATOR_RETRIES=
LLM_ALIGNMENT_MAX_CONTENT_RETRIES=
LLM_ALIGNMENT_FALLBACK_CHUNK_SIZE=
```

示例：把对齐回退 chunk 从 12 改成 8

```env
LLM_ALIGNMENT_FALLBACK_CHUNK_SIZE=8
```

### 4.5 sentence_splitter

默认配置：

```toml
[llm.sentence_splitter]
provider = "deepseek"
model = "deepseek-chat"
temperature = 0.0
top_p = 1.0
```

可覆盖环境变量：

```env
LLM_SENTENCE_SPLITTER_PROVIDER=
LLM_SENTENCE_SPLITTER_MODEL=
LLM_SENTENCE_SPLITTER_TEMPERATURE=
LLM_SENTENCE_SPLITTER_TOP_P=
```

示例：把中文分割切到 Kimi

```env
MOONSHOT_API_KEY=你的key
LLM_SENTENCE_SPLITTER_PROVIDER=kimi
LLM_SENTENCE_SPLITTER_MODEL=kimi-k2.5
```

## 5. 常见用法

### 全链路切到 Kimi

```env
MOONSHOT_API_KEY=你的key

LLM_TRANSLATION_PROVIDER=kimi
LLM_TRANSLATION_MODEL=kimi-k2.5

LLM_CONTEXT_PROVIDER=kimi
LLM_CONTEXT_MODEL=kimi-k2.5

LLM_ALIGNMENT_PROVIDER=kimi
LLM_ALIGNMENT_MODEL=kimi-k2.5

LLM_SENTENCE_SPLITTER_PROVIDER=kimi
LLM_SENTENCE_SPLITTER_MODEL=kimi-k2.5
```

### 只切翻译，其他保持默认

```env
MOONSHOT_API_KEY=你的key
LLM_TRANSLATION_PROVIDER=kimi
LLM_TRANSLATION_MODEL=kimi-k2.5
```

### 只调运行参数，不改模型

```env
MAX_CONCURRENT_TASKS=8
RETRY_ATTEMPTS=4
BATCH_SIZE=80
```

## 6. Kimi 的 `top_p` 特殊限制

当前 Kimi 2.5 接口要求 `top_p` 使用 `0.95`。

如果对应任务使用的是 Kimi，请不要把下面这些变量改成 `0.7` 或 `1.0`：

- `LLM_TRANSLATION_TOP_P_MAIN`
- `LLM_TRANSLATION_TOP_P_STRICT`
- `LLM_TRANSLATION_TOP_P_FALLBACK`
- `LLM_CONTEXT_TOP_P`
- `LLM_ALIGNMENT_TOP_P`
- `LLM_SENTENCE_SPLITTER_TOP_P`

否则会收到类似错误：

```text
invalid top_p: only 0.95 is allowed for this model
```

## 7. 当前不建议通过环境变量覆盖的项

目前下面这些值，仍以 `external_services.toml` 中的 provider 默认值为主：

- `providers.<name>.timeout`
- `providers.<name>.temperature`
- `providers.<name>.top_p`

原因是当前实现把“日常切换”重点放在 slot 级：

- 选哪个 provider
- 用哪个 model
- 用哪些 slot 参数

如果你需要换这些 provider 默认值，建议直接改 `external_services.toml`。

例外：Kimi 的 `base_url` 现在支持通过环境变量覆盖：

```env
MOONSHOT_BASE_URL=https://api.moonshot.cn/v1
```

如果不设置，默认仍然使用 `external_services.toml` 中的：

```toml
[providers.kimi]
base_url = "https://api.moonshot.ai/v1"
```

## 8. Cloud Run 操作方式

在 Cloud Run 后台编辑服务时：

1. 打开对应服务
2. 进入“编辑并部署新修订版本”
3. 找到“环境变量”
4. 按本文中的变量名新增或修改
5. 发布新 revision

发布后，新的环境变量会覆盖 `external_services.toml` 默认值。
