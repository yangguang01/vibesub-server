# -*- coding: utf-8 -*-
"""
脚本：srv3_json3_to_srt_llm_split_B.py
=====================================
使用 **方案 B**：
- 向 LLM 发送 **原句文本**（每批最多 `CHUNK_SIZE` 条长句）；
- LLM 返回包含分割符号（###）的文本；
- 本地将分割后的文本顺序映射回单词索引，再计算精准时间轴，合并输出 SRT。

全部注释为中文，方便直接阅读与二次开发。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from openai import OpenAI

from app.common.core.logging import logger


# ──────────────────────────────── 默认配置 ─────────────────────────────── #
DEFAULT_LLM_MODEL    = "gpt-4.1"                                      # 可换更大模型
DEFAULT_MAX_WORDS    = 100     # 单句超过多少词视为"长句"
DEFAULT_CHUNK_SIZE   = 10     # 每次发送给 LLM 的长句条数
DEFAULT_MIN_DURATION = 100    # 每条短句最少时长（ms）
TOKEN_CLEAN  = re.compile(r"[^\w\s]")  # 清洗用：去所有非字母数字字符

# ─────────────────────────────── 数据结构 ───────────────────────────── #
@dataclass
class Word:
    text: str
    start: int  # 毫秒

@dataclass
class Sentence:
    words: List[Word]
    start: int
    end:   int

    def as_text(self) -> str:
        """把句子还原为原文本，保留原始空格与标点。"""
        return " ".join(w.text for w in self.words)

# ─────────────────────────────── 解析器示例 ─────────────────────────── #

def parse_json3(path: Path) -> List[Word]:
    """
    解析 YouTube json3 (events/words) 并返回时间轴级别的 Word 列表。

    处理要点
    --------
    1. 兼容两种顶层结构:
       - events: 官方 YouTube json3
       - words : Deepgram/Faster-Whisper 之类的 word-level JSON
    2. events 结构里, `tOffsetMs` 是 **相对当前 event 起点** 的绝对时间,
       直接 `tStartMs + tOffsetMs` 即可; 不做累加。
    3. words 结构里, `tOffsetMs` 通常是增量 -> 需要累加游标。
    4. 自动跳过纯换行事件 (`aAppend == 1`) 与空白文本。
    """
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    words: List[Word] = []

    # -------- ① events 结构 --------
    if "events" in data:
        for ev in data["events"]:
            # 跳过仅追加换行的事件
            if ev.get("aAppend") == 1:
                continue

            ev_start = ev.get("tStartMs")
            if ev_start is None:
                # 极少见, 防御性处理
                continue

            for seg in ev.get("segs", []):
                txt = seg.get("utf8", "")
                if not txt.strip() or txt == "\n":
                    continue

                start = ev_start + seg.get("tOffsetMs", 0)
                # 去掉前后空格, 但保留内部空格
                words.append(Word(text=txt.strip(), start=start))

    # -------- ② words 结构 --------
    elif "words" in data:
        abs_t = 0
        for w in data["words"]:
            txt = (w.get("w") or w.get("utf8") or w.get("word") or "")
            if not txt.strip():
                continue

            abs_t += w.get("tOffsetMs", 0)  # 增量累加
            words.append(Word(text=txt.strip(), start=abs_t))

    else:
        raise ValueError("未知 json3 结构: 既无 'events' 也无 'words'")

    # 统一按时间排序, 防止跨 event 顺序错乱
    words.sort(key=lambda w: w.start)
    return words


def parse_srv3(path: Path) -> List[Word]:
    """示例 SRV3 解析，命名空间与字段请按实际调整"""
    ns = {"tt": "http://www.w3.org/ns/ttml"}
    root = ET.parse(path).getroot()
    words: List[Word] = []
    for p in root.iterfind(".//tt:p", ns):
        p_start = int(float(p.attrib["begin"].rstrip("s")) * 1000)
        for s in p.iterfind("tt:s", ns):
            for w in s.iterfind("tt:t", ns):
                abs_start = p_start + int(float(w.attrib["t"].rstrip("s")) * 1000)
                words.append(Word(text=w.text, start=abs_start))
    return words

# ─────────────────────────────── 初步断句 ──────────────────────────── #
PUNCT = re.compile(r"[.!?]$")

def initial_sentences(words: List[Word]) -> List[Sentence]:
    sentences: List[Sentence] = []
    buff: List[Word] = []
    for idx, wd in enumerate(words):
        buff.append(wd)
        if PUNCT.search(wd.text) or idx == len(words) - 1:
            start = buff[0].start
            end   = words[idx + 1].start if idx < len(words) - 1 else wd.start + 800
            sentences.append(Sentence(words=buff, start=start, end=end))
            buff = []
    return sentences

# ─────────────────────────────── LLM 调用（方案 B） ─────────────────── #
SYSTEM_PROMPT = """
Segment multiple English texts into shorter sentences without altering the original text, retaining any errors or repetitions present.

- Do not modify the original text, maintaining any spelling errors, grammatical mistakes, or repetition.
- Try to limit the length of each sentence to fewer than 40 words while preserving the complete meaning.
- Use the separator `###` to split long texts.
- Process all input sentences and return them in the SAME ORDER.
- If a sentence is already short enough (under 40 words), return it unchanged without separators.

# Input Format

The input will be a JSON object with a key "sentences" containing an array of texts to process.

# Steps

1. For each sentence in the input array:
   - If it's longer, identify natural break points and insert `###` separators
2. Ensure all segments are under 40 words while preserving meaning
3. Return all sentences in the same order as received

# Output Format

Return a JSON object with a key "results" containing an array of processed texts (with or without ### separators) in the same order as the input.

# Examples

**Input:**
```json
{
    "sentences": [
        "around the time like in September we had like an internal hackathon and everyone was free to build basically whatever they wanted to build but it turns out everyone just built an MCP and it was it was crazy like everyone's ideas were oh but what if we made this an MCP server thank you",
        "But this one is really long and contains multiple complex ideas that should be separated for better readability and understanding of the content which makes it easier to follow along"
    ]
}
```

**Output:**
```json
{
    "results": [
        "around the time like in September ### we had like an internal hackathon ### and everyone was free to build basically whatever they wanted to build ### but it turns out everyone just built an MCP ### and it was it was crazy ### like everyone's ideas were oh but what if we made this an MCP server ### thank you",
        "But this one is really long and contains multiple complex ideas ### that should be separated for better readability ### and understanding of the content which makes it easier to follow along"
    ]
}
```

# Important Notes

- The number of items in "results" MUST equal the number of items in "sentences"
- Maintain the exact order of sentences
- Only use ### for sentences that need splitting
- Each segment should be a complete, meaningful unit
"""


def chunks(lst: List, size: int):
    """把列表按 size 切片为子列表生成器"""
    for i in range(0, len(lst), size):
        yield lst[i:i+size]


def call_llm_batch(batch_sent: List[Sentence], llm_model: str = DEFAULT_LLM_MODEL, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Dict[str, List[str]]:
    """
    使用 client.responses.create 调 LLM。
    每批 <= CHUNK_SIZE 条长句。
    返回 { original_text: [短句1, 短句2, ...] }
    """
    # 1) 把 batch_sent 变成 sentences 数组
    sentence_texts = [s.as_text() for s in batch_sent]
    user_payload = json.dumps({"sentences": sentence_texts}, ensure_ascii=False)

    # 2) 构造 input 消息列表 —— 仅需 system + user 两条
    inputs = [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_payload}],
        },
    ]

    # 3) 调用 responses.create
    client = OpenAI()

    raw_json = client.responses.create(
        model=llm_model,                 # 如 "gpt-4o-mini" / "gpt-4.1"
        input=inputs,
        text={"format": {"type": "json_object"}},  # 强制 JSON
        temperature=0,
        max_output_tokens=20000,          # 依需要调整
    ).output_text

    data = json.loads(raw_json)
    
    # 4) 处理返回的数据 - 基于顺序对应
    result_dict = {}
    results = data.get("results", [])
    
    # 确保返回结果数量与输入匹配
    if len(results) != len(sentence_texts):
        logger.warning(f"LLM返回数量不匹配：期望{len(sentence_texts)}，实际{len(results)}")
        # 尽可能处理已返回的结果
        
    for i, (original_sent, segmented_text) in enumerate(zip(sentence_texts, results)):
        # 如果句子包含 ###，说明被分割了
        if "###" in segmented_text:
            segments = [seg.strip() for seg in segmented_text.split("###") if seg.strip()]
        else:
            # 没有 ### 说明句子不需要分割
            segments = [segmented_text.strip()] if segmented_text.strip() else []
        
        result_dict[original_sent] = segments
    
    return result_dict

# ─────────────────────────────── 字符串短句 → 索引 ───────────────────── #

def seg_texts_to_spans(sentence: Sentence, segments: List[str]) -> List[Tuple[int,int]]:
    """把 ['短句1','短句2',…] 映射为 [(起,止), …]"""
    words_clean = [TOKEN_CLEAN.sub("", w.text.lower()) for w in sentence.words]
    ptr = 0
    spans: List[Tuple[int, int]] = []

    for seg in segments:
        tokens = [TOKEN_CLEAN.sub("", t.lower()) for t in seg.split() if t]
        target = " ".join(tokens)
        
        if not target:
            raise ValueError("空子句")

        # 滑窗搜索：尝试在 words_clean 中找一段连续的窗口，其拼接等于 target
        found = False
        for i in range(ptr, len(words_clean)):
            joined = ""
            for j in range(i, len(words_clean)):
                joined = " ".join(words_clean[i:j+1])
                
                if joined == target:
                    spans.append((i, j))
                    ptr = j + 1
                    found = True
                    break
            if found:
                break

        if not found:
            logger.error(f"错误对齐 target: '{target}'")
            logger.error(f"从 ptr={ptr} 开始的 words_clean: {words_clean[ptr:]}")
            raise ValueError(f"子句与原句无法对齐：'{seg}'")

    return spans

# ─────────────────────────────── 索引 → 新短句 (带时间轴) ────────────── #

def spans_to_subs(sentence: Sentence, spans: List[Tuple[int,int]], min_duration: int = DEFAULT_MIN_DURATION) -> List[Sentence]:
    if not spans:
        return [sentence]
    new_subs: List[Sentence] = []
    for i, (s_i, e_i) in enumerate(spans):
        seg_words = sentence.words[s_i:e_i+1]
        start_ms  = seg_words[0].start
        if i + 1 < len(spans):
            next_start = sentence.words[spans[i+1][0]].start
            end_ms = max(start_ms + min_duration, next_start - 1)
        else:
            end_ms = sentence.end
        new_subs.append(Sentence(words=seg_words, start=start_ms, end=end_ms))
    return new_subs

# ─────────────────────────────── 时间格式 ───────────────────────────── #

def ms_to_srt(ms: int) -> str:
    td = timedelta(milliseconds=ms)
    h, r = divmod(td.seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02},{td.microseconds//1000:03}"

# ─────────────────────────────── 主处理函数 ────────────────────────── #

def process_ytsub(
    input_file: str | Path,
    output_file: Optional[str | Path] = None,
    llm_model: str = DEFAULT_LLM_MODEL,
    max_words: int = DEFAULT_MAX_WORDS,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    min_duration: int = DEFAULT_MIN_DURATION
) -> str:
    """
    处理字幕文件，将长句分割成短句并生成SRT格式字符串
    
    Args:
        input_file: 输入文件路径（支持 .json3 或 .srv3 格式）
        output_file: 输出SRT文件路径（可选，默认为输入文件名+"-splited.srt"）
        llm_model: LLM模型名称
        max_words: 单句超过多少词视为"长句"
        chunk_size: 每次发送给LLM的长句条数
        min_duration: 每条短句最少时长（毫秒）
    
    Returns:
        str: SRT格式的字符串内容
        
    Raises:
        ValueError: 当文件格式不支持时
        FileNotFoundError: 当输入文件不存在时
    """
    input_path = Path(input_file)
    
    # 检查文件是否存在
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")
    
    # 自动生成输出文件路径
    if output_file is None:
        output_path = input_path.parent / f"{input_path.stem}-splited.srt"
    else:
        output_path = Path(output_file)
    
    logger.info(f"处理文件: {input_path}")
    logger.info(f"输出到: {output_path}")
    
    # ① 解析文件
    if input_path.suffix.lower() == ".json3":
        words = parse_json3(input_path)
    elif input_path.suffix.lower() == ".srv3":
        words = parse_srv3(input_path)
    else:
        raise ValueError(f"不支持的文件格式: {input_path.suffix}。仅支持 .json3 和 .srv3")
    
    logger.info(f"解析到 {len(words)} 个单词")

    # ② 初步切句
    sentences = initial_sentences(words)
    logger.info(f"初步切分为 {len(sentences)} 个句子")

    # ③ 找出长句并分批发送给 LLM
    long_indices = [i for i, s in enumerate(sentences) if len(s.words) > max_words]
    logger.info(f"需要分割的长句: {len(long_indices)} 个")
    
    if long_indices:
        logger.debug(f"长句索引示例: {long_indices[:10]}{'...' if len(long_indices) > 10 else ''}")
    
    long_map: Dict[int, List[Sentence]] = {}

    for group in chunks(long_indices, chunk_size):
        batch_sent = [sentences[i] for i in group]
        mapping = call_llm_batch(batch_sent, llm_model, chunk_size)  # {原句: seg_list}
        for idx in group:
            original_text = sentences[idx].as_text()
            segs = mapping.get(original_text)
            if not segs:
                logger.warning(f"LLM 未返回句子：{original_text[:60]}... 保留原句")
                continue
            try:
                spans = seg_texts_to_spans(sentences[idx], segs)
                long_map[idx] = spans_to_subs(sentences[idx], spans, min_duration)
            except ValueError as e:
                logger.warning(f"对齐失败，保留原句：{e}")

    # ④ 合并普通句 + 已拆分短句
    final_subs: List[Sentence] = []
    for i, s in enumerate(sentences):
        # 若该句被 LLM 拆分，取拆分结果；否则保留原句
        final_subs.extend(long_map.get(i, [s]))

    # ⑤ 生成 SRT 字符串内容
    final_subs.sort(key=lambda x: x.start)             # 保险起见再排一次时间
    
    # 构建SRT字符串
    srt_content = ""
    for n, sub in enumerate(final_subs, 1):
        srt_content += f"{n}\n"
        srt_content += f"{ms_to_srt(sub.start)} --> {ms_to_srt(sub.end)}\n"
        srt_content += f"{sub.as_text()}\n\n"
    
    # 保存到文件（方便测试）
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt_content)
    
    logger.info(f"已生成 {output_path}（{len(final_subs)} 行字幕）")
    return srt_content

# ─────────────────────────────── 兼容性主函数 ────────────────────────── #

def main():
    """为了向后兼容保留的main函数"""
    # 使用原有的硬编码路径进行测试
    input_path = Path("LCEmiRjPEtQ.en.json3")
    if input_path.exists():
        srt_content = process_ytsub(input_path)
        
    else:
        logger.info("请使用 process_ytsub() 函数并提供有效的输入文件路径")

if __name__ == "__main__":
    main()