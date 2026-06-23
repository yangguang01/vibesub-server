"""P000 真实视频端到端测试 harness（连真实 DeepSeek + 真实 YouTube 字幕）。

不是 pytest 用例（文件名无 test_ 前缀，默认 pytest 不会收集），用 venv 直接跑：

    cd vibesub-server
    .venv/bin/python tests/p000/e2e_real.py --section short --limit 2

依赖：
  - vibesub-server/.env 里的 DEEPSEEK_API_KEY（翻译用）。
  - 本机 yt-dlp（抓 YouTube 自动字幕）。失败时可在 .env 里加 PROXY_URL。

它做什么（专为测 P000 翻译对齐）：
  1. 抓真实英文自动字幕 → process_ytsub(max_words=极大，跳过它内部的 OpenAI 拆句) → 英文 SRT。
     （production 默认 max_words=100 会用 OpenAI 拆长英文句；我们没有 OpenAI key，
       用大 max_words 跳过它。结果是源行等长或更长 → 对齐测试等价或更严格。）
  2. extract_asr_sentences → translate_subtitles(deepseek)。
  3. 录下每个批次的原始 LLM 返回（cassette），用于"旧逻辑 vs 新逻辑"回放对比。
  4. 量化指标：覆盖率、占位行、首批 count-mismatch 批次（旧代码会塌缩丢行）、修复触发数；
     断言结构不变量（输出行号 ⊆ 输入行号、无重复、无静默错位）。
  5. 产出每个视频的双语对照文件，供人工 eyeball 校验"中文第 N 行 = 英文第 N 行"。

产物写到 tests/p000/_artifacts/<video_id>/ 和 cassettes/。
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

# 本机开发常有 Clash 等本地 socks 代理泄漏进 httpx（all_proxy=socks5://...），
# 导致连 DeepSeek 报 "socksio 未安装"。DeepSeek 在国内可直连，这里：
#   - 移除 socks all_proxy（避免 httpx 构造 socks 传输需要 socksio）
#   - 把 api.deepseek.com 加进 no_proxy（这条 API 直连）
#   - 保留 http_proxy/https_proxy 给 yt-dlp 连 YouTube（被墙时需要）
os.environ.pop("all_proxy", None)
os.environ.pop("ALL_PROXY", None)
for _k in ("no_proxy", "NO_PROXY"):
    os.environ[_k] = (os.environ.get(_k, "") + ",api.deepseek.com").lstrip(",")

# 让脚本能 import app.*（等价 PYTHONPATH=仓库根）
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import app.common.services.translation as T
from app.common.services.translation import (
    extract_asr_sentences,
    translate_subtitles,
    align_translation_by_key,
    audit_translation_alignment,
    process_transdict_num,
    subtitles_to_dict,
    map_marged_sentence_to_timeranges,
    map_chinese_to_time_ranges_v2,
    generate_custom_prompt,
    PLACEHOLDER,
)
from app.common.services.download_ytsub import download_auto_subtitle
from app.common.services.process_ytsub import process_ytsub
from app.common.core.config import TRANSLATE_BATCH_SIZE, DEEPSEEK_API_KEY

URL_FILE = ROOT.parent / "yt_url_list.txt"          # 外层根目录
ARTIFACT_DIR = Path(__file__).resolve().parent / "_artifacts"
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"
REPAIR_SIG = "You missed or left empty"

# T3 之前的"老 prompt"：用相对编号 1..K（自相矛盾——输入是绝对行号，却让它从 1 编号）。
# 仅供 --legacy-prompt before/after 实验：复现旧系统(老prompt+大批次)在真实视频上的漏行/塌缩率。
OLD_RELATIVE_PROMPT = """
# Role
You are a skilled translator specializing in converting English subtitles into natural and fluent Chinese while maintaining the original meaning.

# Background information of the translation content
{custom_prompt}
Please leverage domain-specific knowledge to deliver an accurate translation.

## Skills
### Skill 1: Line-by-Line Translation
- Translate each subtitle line individually based on the input content.

## Constraints
- For punctuation requirements: Do not add a period when the sentence ends
- The provided subtitles range from line {first_item_number} to line {end_item_number}, totaling {check_chunk_string} lines.
- Provide the Chinese translation in the specified JSON format:
  ```
  {{
  "1": "<Translation of subtitle line 1>",
  "2": "<Translation of subtitle line 2>",
  "3": "<Translation of subtitle line 3>",
  ...
  }}
  ```
"""

# ----------------------------- 录制 LLM 原始返回（cassette） -----------------------------
RECORDS = []  # 每个视频跑前清空：[{messages, content|error}]


class _RecordingCompletions:
    def __init__(self, real):
        self._real = real

    async def create(self, **kwargs):
        try:
            resp = await self._real.create(**kwargs)
            content = resp.choices[0].message.content
            RECORDS.append({"messages": kwargs.get("messages"), "content": content})
            return resp
        except Exception as e:  # 把 API 错误也记下来，便于排查
            RECORDS.append({"messages": kwargs.get("messages"), "error": repr(e)})
            raise


class _RecordingChat:
    def __init__(self, real):
        self.completions = _RecordingCompletions(real.completions)


class _RecordingClient:
    def __init__(self, real):
        self._real = real
        self.chat = _RecordingChat(real.chat)


def _install_recorder():
    """把 translation.AsyncOpenAI 换成会录制 create() 的包装。"""
    real = T.AsyncOpenAI

    def factory(*args, **kwargs):
        return _RecordingClient(real(*args, **kwargs))

    T.AsyncOpenAI = factory
    return real


# ----------------------------- 旧逻辑复刻（仅用于 before/after 对比） -----------------------------

def _parse_expected_from_user(content):
    """从批次的 user 消息里解析出本批的绝对行号（"N: text" 行）。"""
    nums = []
    for line in (content or "").splitlines():
        head = line.split(":", 1)[0].strip()
        if head.isdigit():
            nums.append(int(head))
    return nums


def _old_outcome(content, expected):
    """复刻旧 process_chunk：JSON 坏 / 行数不等 → 整批塌缩成 first 一个键（其余真实译文丢失）；
    行数相等 → process_transdict_num 按位置重编号。返回 (kind, lost_lines)。
    lost_lines = 旧代码在这一批会"丢失的真实译文行数"（塌缩时即整批）。"""
    first = expected[0]
    try:
        j = json.loads(content)
    except Exception:
        return ("collapse", len(expected))
    if not isinstance(j, dict) or len(j) != len(expected):
        return ("collapse", len(expected))
    return ("renumber", 0)


# ----------------------------- 主流程 -----------------------------

def load_urls(section):
    short, long = [], []
    cur = None
    for line in URL_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("时长短"):
            cur = "short"; continue
        if s.startswith("时长较长") or s.startswith("时长长"):
            cur = "long"; continue
        if s.startswith("http"):
            (short if cur == "short" else long).append(s)
    return {"short": short, "long": long, "all": short + long}[section]


def video_id_of(url):
    m = re.search(r"[?&]v=([\w-]+)", url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-11:]


async def run_one(url, max_words):
    vid = video_id_of(url)
    out_dir = ARTIFACT_DIR / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = {"url": url, "video_id": vid}
    RECORDS.clear()

    # ---- 1. 抓英文字幕 ----
    t0 = time.time()
    try:
        sub_path, title, channel = download_auto_subtitle(url)
    except Exception as e:
        rec["error"] = f"download_auto_subtitle 失败: {e!r}"
        return rec
    if not sub_path:
        rec["error"] = "未找到英文自动字幕"
        return rec
    rec["title"], rec["channel"] = title, channel

    try:
        srt_text = process_ytsub(sub_path, max_words=max_words)
    except Exception as e:
        rec["error"] = f"process_ytsub 失败: {e!r}"
        return rec
    numbered = extract_asr_sentences(srt_text)
    rec["input_lines"] = len(numbered)
    rec["fetch_secs"] = round(time.time() - t0, 1)
    if not numbered:
        rec["error"] = "解析出 0 行英文字幕"
        return rec
    (out_dir / "english.srt").write_text(srt_text, encoding="utf-8")

    # ---- 2. 真实 DeepSeek 翻译 ----
    custom_prompt = generate_custom_prompt(title, channel, "")
    t1 = time.time()
    try:
        result = await translate_subtitles(numbered, custom_prompt, "deepseek", "", title, vid)
    except Exception as e:
        rec["error"] = f"translate_subtitles 失败: {e!r}"
        return rec
    rec["translate_secs"] = round(time.time() - t1, 1)

    # ---- 3. 新代码真实结果：覆盖率 / 占位 / 结构不变量 ----
    input_keys = set(numbered.keys())
    output_keys = set(result.keys())
    missing = sorted(input_keys - output_keys)
    extra = sorted(output_keys - input_keys)            # 应为空（不会冒出输入没有的行号）
    placeholders = sorted(k for k, v in result.items() if v == PLACEHOLDER)
    translated_ok = len(input_keys) - len(missing) - len(placeholders)
    rec["coverage"] = round(translated_ok / max(1, len(input_keys)), 4)
    rec["missing_keys"] = missing
    rec["extra_keys"] = extra
    rec["placeholder_count"] = len(placeholders)
    rec["placeholder_keys"] = placeholders[:50]
    audit_translation_alignment(list(input_keys), result, vid)

    # 结构不变量（P000 核心保证）：输出行号 ⊆ 输入行号、无静默丢键
    rec["invariant_output_subset_of_input"] = (len(extra) == 0)
    rec["invariant_no_missing_keys"] = (len(missing) == 0)

    # ---- 4. 从 cassette 统计首批表现 + 旧逻辑会怎样 ----
    cassette = list(RECORDS)
    (CASSETTE_DIR / f"{vid}.json").write_text(
        json.dumps(cassette, ensure_ascii=False, indent=2), encoding="utf-8")
    first_pass = {}   # expected_tuple -> content（取每个批次的首个 initial 返回）
    repair_calls = 0
    api_errors = 0
    for r in cassette:
        msgs = r.get("messages") or []
        is_repair = any(REPAIR_SIG in (m.get("content") or "") for m in msgs)
        if "error" in r:
            api_errors += 1
            continue
        if is_repair:
            repair_calls += 1
            continue
        user = next((m.get("content") for m in msgs if m.get("role") == "user"), "")
        expected = _parse_expected_from_user(user)
        if not expected:
            continue
        key = (expected[0], expected[-1], len(expected))
        first_pass.setdefault(key, (r["content"], expected))

    batches = len(first_pass)
    count_mismatch_batches = 0
    nonabsolute_batches = 0
    old_lost_lines = 0
    for content, expected in first_pass.values():
        kind, lost = _old_outcome(content, expected)
        if kind == "collapse":
            count_mismatch_batches += 1
            old_lost_lines += lost
        # 新逻辑视角：首批 key 是否就是干净的绝对行号
        aligned, miss = align_translation_by_key(json.loads(content) if _is_json(content) else {}, expected)
        if set(aligned.keys()) != set(expected):
            nonabsolute_batches += 1

    rec["batches"] = batches
    rec["first_pass_count_mismatch_batches"] = count_mismatch_batches
    rec["first_pass_nonabsolute_batches"] = nonabsolute_batches
    rec["repair_calls"] = repair_calls
    rec["api_errors"] = api_errors
    rec["OLD_estimated_lost_lines"] = old_lost_lines     # 旧代码会塌缩丢失的真实译文行数
    rec["NEW_silent_shifts"] = 0 if (len(extra) == 0 and len(missing) == 0) else "VIOLATED"

    # ---- 5. 双语对照文件（人工 eyeball：中文第 N 行 = 英文第 N 行？）----
    try:
        subtitles_dict = subtitles_to_dict(srt_text)
        merged = map_marged_sentence_to_timeranges(numbered, subtitles_dict)
        zh_timed = map_chinese_to_time_ranges_v2(result, merged)
        lines = []
        for n in sorted(numbered):
            tr = zh_timed.get(n, {}).get("time_range", "")
            lines.append(f"[{n}] {tr}\n  EN: {numbered[n]}\n  ZH: {result.get(n, '<<MISSING>>')}")
        (out_dir / "bilingual_check.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        rec["bilingual_error"] = repr(e)

    (out_dir / "metrics.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def _is_json(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


async def main_async(args):
    if not DEEPSEEK_API_KEY:
        print("❌ 没有 DEEPSEEK_API_KEY（请把 key 写进 vibesub-server/.env）", file=sys.stderr)
        sys.exit(2)
    _install_recorder()
    if args.legacy_prompt:
        T.get_system_prompt_for_model = lambda model: OLD_RELATIVE_PROMPT
        print("⚙️  --legacy-prompt: 使用 T3 之前的相对编号老 prompt（复现旧系统）\n")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

    urls = load_urls(args.section)
    if args.limit:
        urls = urls[: args.limit]
    print(f"== P000 真实测试：section={args.section} 共 {len(urls)} 个视频，"
          f"TRANSLATE_BATCH_SIZE={TRANSLATE_BATCH_SIZE} ==\n")

    summary = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        rec = await run_one(url, args.max_words)
        summary.append(rec)
        if "error" in rec:
            print(f"   ⚠️  {rec['error']}\n")
            continue
        print(f"   行数={rec['input_lines']} 覆盖率={rec['coverage']*100:.2f}% "
              f"占位={rec['placeholder_count']} 批次={rec['batches']} "
              f"首批count不符批次={rec['first_pass_count_mismatch_batches']} "
              f"修复调用={rec['repair_calls']} "
              f"旧代码估计丢行={rec['OLD_estimated_lost_lines']} "
              f"新代码静默错位={rec['NEW_silent_shifts']}")
        print(f"   不变量: 输出⊆输入={rec['invariant_output_subset_of_input']} "
              f"无缺键={rec['invariant_no_missing_keys']} "
              f"（用时 抓字幕{rec.get('fetch_secs')}s + 翻译{rec.get('translate_secs')}s）\n")

    out = ARTIFACT_DIR / f"summary_{args.section}_{int(time.time())}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"== 汇总写入 {out} ==")
    # 总体结论
    ok = [r for r in summary if "error" not in r]
    if ok:
        worst_cov = min(r["coverage"] for r in ok)
        any_shift = any(r["NEW_silent_shifts"] != 0 for r in ok)
        total_old_lost = sum(r["OLD_estimated_lost_lines"] for r in ok)
        print(f"成功 {len(ok)}/{len(summary)} 个视频；最低覆盖率={worst_cov*100:.2f}%；"
              f"新代码静默错位={'有(!!)' if any_shift else '0'}；"
              f"旧代码在这些视频上估计会塌缩丢失 {total_old_lost} 行。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", choices=["short", "long", "all"], default="short")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个（0=全部）")
    ap.add_argument("--max-words", type=int, default=100000,
                    help="process_ytsub 长句阈值；设大以跳过其内部 OpenAI 拆句")
    ap.add_argument("--legacy-prompt", action="store_true",
                    help="用 T3 之前的相对编号老 prompt（before/after 实验：复现旧系统）")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
