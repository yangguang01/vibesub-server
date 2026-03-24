# Alignment Validator Notes

日期：2026-03-23

## 本次改动

1. 统一配置化
- `translation_main`
- `translation_main_strict`
- `translation_fallback_strong`
- `translation_context`
- `alignment_validator`
- `sentence_splitter`

以上任务都走 [config/external_services.toml](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/config/external_services.toml) 配置，不再在业务代码里硬编码 provider / model / base_url。

2. 新增 `sentence_splitter` 任务
- 用于字幕长句分割
- 现在 `process_ytsub.py` 和 `translation.py` 的分句逻辑都共用这一项配置

3. 修正 `process_ytsub.py` 的 DeepSeek 参数
- 原来 `max_tokens=20000`
- DeepSeek 上限是 `8192`
- 现已改为 `8000`

4. 调整 alignment validator 判定口径
- 只检查是否一一对应
- 只检查有没有跨行串义、借邻行、错位
- 不因为源字幕是残句、ASR 断裂句、不完整句而失败
- 不因为中文翻译本身不完整而失败

## 当前本地调试文件

生成位置：

- `/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/tmp/debug_records/<video_id>_translation_debug.txt`
- `/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/tmp/debug_records/<video_id>_alignment.json`
- `/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/tmp/debug_records/<video_id>_alignment_report.txt`
- `/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/tmp/debug_records/<video_id>_alignment_overview.json`
- `/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/tmp/debug_records/<video_id>_alignment_overview.txt`

各文件用途：

1. `translation_debug.txt`
- 记录翻译 chunk 输入
- 记录模型输出
- 记录行数是否匹配
- 不适合专门看对齐

2. `alignment.json`
- 结构化对齐记录
- 包含每个 chunk 的：
  - `attempt_type`
  - `mode`
  - `source_lines`
  - `translated_lines`
  - `validation.pass`
  - `validation.confidence`
  - `validation.issues`
  - `error_message`
- 不再保留 `summary`

3. `alignment_report.txt`
- 给人工直接阅读的文本版
- 每个 chunk 展示：
  - `PASS`
  - `CONFIDENCE`
  - `ISSUES`
  - `reason`
  - `SOURCE vs TRANSLATED`
- 不再输出 `summary`

4. `alignment_overview.json / txt`
- 汇总统计结果
- 包含：
  - `true / false / error` 数量
  - 每个 chunk 的最终状态
  - 哪些 chunk 在后续尝试中从 `false -> true`

## 当前模式说明

当前默认配置：

```toml
[tasks.alignment_validator]
mode = "observe"
```

含义：
- validator 会运行
- 会生成本地报告
- 但不会触发严格重翻和兜底修正

如果要测试“修改之后哪些 chunk 变好了”，需要切换成：

```toml
mode = "enforce"
```

这样才会进入：
- `translation_main`
- `translation_main_strict`
- `translation_main_strict_split`
- `translation_fallback_strong`

然后 `alignment_overview` 才会出现“恢复成功”的 chunk。

## 已观察到的测试结果

### 视频 `pEAZ5iyKgWE`

当前已有记录统计：

- 记录级别：`true = 16`，`false = 4`，`error = 0`
- chunk 最终结果：`true = 16`，`false = 4`，`error = 0`
- 恢复成功 chunk 数：`0`

原因：
- 当前记录里只有 `attempt_type = translation_main`
- 说明这份数据是在 `observe` 路径下得到的
- 还没有进入 strict / fallback 修正链

## 你当前最适合的测试方式

1. 保持 `observe`
- 看 validator 是否判定合理
- 看 `alignment_report.txt`

2. 切到 `enforce`
- 看哪些 chunk 会进入 strict / fallback
- 看 `alignment_overview.txt`
- 重点看哪些 chunk 从 `false -> true`

## 相关代码位置

- [app/common/services/alignment_validator.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/alignment_validator.py)
- [app/common/services/translation.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/translation.py)
- [app/common/services/process_ytsub.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/common/services/process_ytsub.py)
- [app/worker/processor.py](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/app/worker/processor.py)
- [config/external_services.toml](/Users/yanggaung/Desktop/vibesub/tube-trans-server-byFastAPI-v3/config/external_services.toml)
