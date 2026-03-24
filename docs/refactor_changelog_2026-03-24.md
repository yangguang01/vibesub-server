# Refactor Changelog

日期：2026-03-24  
分支：`codex/overall-refactor`

## 本轮提交记录

1. `1f36e3e` `1.7 外部服务配置化与基础设施抽离`
2. `24f7d6e` `1.8 重构任务执行与字幕翻译流水线`

## 本轮重构目标

- 将外部服务接入信息从业务代码中抽离，统一改为配置驱动。
- 将公开 API 与内部 worker 执行面拆开，减少职责混杂。
- 重构任务生命周期、配额控制与 Firestore 数据读写方式。
- 重构字幕翻译、对齐校验、调试记录与回传链路。

## 改动详情

### 版本 1.7 外部服务配置化与基础设施抽离

核心变化：

- 新增 [config/external_services.toml](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/config/external_services.toml)，统一声明：
  - LLM provider
  - ASR provider
  - Firebase / Firestore / Storage
  - Cloud Tasks 队列
  - YouTube 下载参数
  - 各业务 task 对应的 provider、model、temperature、path_prefix
- 新增 [app/common/core/external_services.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/core/external_services.py)，提供配置加载与 task/provider 合并能力。
- 重写 [app/common/core/config.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/core/config.py)：
  - 目录基准改为 `REPO_DIR` / `APP_DIR`
  - 增加 `APP_VERSION`、`INTERNAL_API_PREFIX`
  - 增加 worker、internal auth、Cloud Tasks 相关环境变量
  - 提供 `get_task_config()` / `get_provider_config()`
- 新增 [app/common/services/infrastructure.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/infrastructure.py)，统一懒加载：
  - Firebase App
  - Firestore Client
  - Storage Bucket
  - Cloud Tasks Client
- 新增 [app/common/services/llm_runtime.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/llm_runtime.py)：
  - 统一 LLM runtime 构造
  - 支持 `gpt` / `deepseek` provider override
  - 提供 JSON 输出调用与重试封装
  - 区分可重试与不可重试异常
- 精简旧的客户端初始化入口：
  - [app/common/services/firestore.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/firestore.py)
  - [app/common/services/storage.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/storage.py)
  - [app/common/utils/firebase_init.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/utils/firebase_init.py)
  - [app/common/utils/firebase_storage_init.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/utils/firebase_storage_init.py)
  - [app/common/utils/firestore_init.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/utils/firestore_init.py)
- 重写 [app/common/utils/cloud_tasks.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/utils/cloud_tasks.py)：
  - Cloud Tasks 参数走配置
  - 目标 URL 改为指向 worker 内部路由
  - 支持 OIDC token 注入
  - 创建任务逻辑由异步包装改为同步单职责函数

影响：

- 后续业务代码不再硬编码 provider/model/base_url/queue/path。
- Firebase / Firestore / Storage / Cloud Tasks 初始化逻辑集中，便于复用和替换。

### 版本 1.8 重构任务执行与字幕翻译流水线

#### 1. API 与 worker 边界拆分

- 新增独立 worker 应用：
  - [app/worker/main.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/worker/main.py)
  - [app/worker/routes.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/worker/routes.py)
- 新增 [app/common/utils/internal_auth.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/utils/internal_auth.py)，对 Cloud Tasks 到 worker 的内部请求做 OIDC 校验。
- [app/api/router.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/api/router.py) 移除公开 API 下的 cloud task 路由入口。
- [app/api/main.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/api/main.py) 统一使用 `APP_VERSION`，启动时补目录初始化。
- 新增 [run_worker.sh](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/run_worker.sh)，本地单独启动 worker。

#### 2. 任务模型与 Firestore 读写重构

- 重写 [app/common/models/firestore_models.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/models/firestore_models.py)：
  - 明确 `videoinfo`、`userinfo`、`user_task`、`daily_usage` 集合职责
  - 增加 `QuotaExceededError`
  - 增加 `reserve_user_daily_quota()` / `release_user_daily_quota()`
  - 增加 `create_task_request()`，统一处理新建任务、复用任务、写入 user_task
  - 增加 `mark_task_queued()` / `start_task_attempt()` / `mark_task_completed()` / `mark_task_failed()`
  - `update_video_task()` 支持 `stage`、`retry_count`、`attempt_no`、`queue_task_name`、失败原因、调试链接等字段
  - `get_task_detail()` 合并 user_task 与 videoinfo，统一返回任务详情视图
- 更新 [app/common/models/schemas.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/models/schemas.py)：
  - 状态改为 `created` / `queued` / `processing` / `completed` / `failed`
  - stage 改为 `enqueue` / `download` / `context` / `asr` / `translate` / `validate_alignment` / `postprocess` / `upload`
  - 详情中补充重试次数、队列任务名、失败信息、英文字幕地址、调试地址等字段

#### 3. 公开 API 行为调整

- [app/api/tasks.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/api/tasks.py) 重构为：
  - 创建任务前先预留每日配额
  - 无法提取 video_id 时释放预留配额
  - 已存在结果时直接复用任务结果
  - 新任务改为立即创建 Cloud Task 入队
  - 入队失败时回滚配额并写入失败状态
  - 任务详情、状态、列表、策略读取统一走 Firestore 聚合视图
- [app/api/subtitles.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/api/subtitles.py)：
  - 下载前校验当前用户与任务归属
  - 仅允许下载已完成任务字幕
  - 字幕路径改为配置驱动
  - 下载实现改为通过统一 bucket 客户端获取
- [app/api/health.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/api/health.py) 版本号改为读取配置。
- [app/api/auth.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/api/auth.py) 修正 `InvalidIdTokenError` 拼写并收敛日志内容。
- [app/common/utils/auth.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/utils/auth.py) 删除旧的开发态注释逻辑。

#### 4. 字幕处理与翻译主链路重构

- [app/worker/processor.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/worker/processor.py) 重构为明确的阶段式流程：
  - 优先尝试 YouTube 自动字幕
  - 失败时回退音频下载 + AssemblyAI ASR
  - 生成上下文提示和翻译策略
  - 执行翻译
  - 执行对齐校验
  - 字幕后处理并上传中英字幕
  - 保存本地和远端调试记录
  - 区分永久失败与临时失败
- [app/common/services/process_ytsub.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/process_ytsub.py)：
  - 长句分割改为走 `sentence_splitter` 配置任务
  - 从直接构造 OpenAI 客户端改为走统一 runtime
  - `max_tokens` 调整为 `8000`
- [app/common/services/translation.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/translation.py) 大幅重构：
  - 调试记录改为按 `video_id` 隔离
  - 新增 alignment 记录、文本报告、JSON 报告、overview 汇总
  - 增加本地调试文件落盘 `save_debug_records_to_local()`
  - debug storage 路径改为配置驱动
  - 下载代理读取环境变量，不再硬编码
  - 翻译调用改为统一走配置化 LLM runtime
  - 引入 chunk 校验、失败重试、拆分恢复链路
  - 引入 validator `observe` / `enforce` 模式

#### 5. 对齐校验能力新增

- 新增 [app/common/services/alignment_validator.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/alignment_validator.py)：
  - 定义对齐问题类型与校验结果模型
  - 自动识别允许重复语义的相邻字幕组
  - 先做行数和 key 结构硬校验
  - 再调用 LLM 执行语义级对齐审查
  - 对可疑的自相矛盾结果执行二次语义复核
  - 暴露 `alignment_mode()`、`fallback_chunk_size()`、`max_content_retries()` 供翻译链路读取
- 保留补充说明文档：
  - [docs/alignment_validator_notes_2026-03-23.md](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/docs/alignment_validator_notes_2026-03-23.md)

## 验证记录

- 2026-03-24 执行 `python3 -m compileall app`，通过。
- 仓库当前没有现成的自动化测试目录，本轮未执行单元测试或集成测试。

## 刻意未纳入提交的本地文件

以下文件仍保留在工作区，但未进入本轮提交，判断为本地调试样本或运行产物：

- `U-TSafAIzXw.en.json3`
- `U-TSafAIzXw.webm`
- `gwW8GKwHB3I.en-splited.srt`
- `gwW8GKwHB3I.en.json3`
- `jIviHI7fqyc.en-splited.srt`
- `jIviHI7fqyc.en.json3`
- `pEAZ5iyKgWE.en-splited.srt`
- `pEAZ5iyKgWE.en.json3`
- `pEAZ5iyKgWE.webm`
- `rHtRWyxVQps.en-splited.srt`
- `rHtRWyxVQps.en.json3`

如果后续希望工作区完全干净，建议单独决定：

- 是否删除这些本地产物
- 或者补充 `.gitignore` 忽略规则

## 回看建议

如果后续要继续追踪这一轮重构，建议优先从以下文件开始：

1. [config/external_services.toml](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/config/external_services.toml)
2. [app/common/models/firestore_models.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/models/firestore_models.py)
3. [app/worker/processor.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/worker/processor.py)
4. [app/common/services/translation.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/translation.py)
5. [app/common/services/alignment_validator.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/alignment_validator.py)
