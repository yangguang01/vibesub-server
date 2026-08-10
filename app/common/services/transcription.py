import os
import re

import assemblyai as aai

from app.common.core.config import ASSEMBLYAI_API_KEY
from app.common.core.logging import logger
from app.common.services.subtitle_timing import format_time


def transcribe_audio_with_assemblyai(filename: str) -> list:
    """
    使用 AssemblyAI 转录当前工作目录下的音频文件，
    文件名直接传入（例如 'abc123.webm'），返回句子列表。

    Args:
        filename (str): 当前目录下的音频文件名

    Returns:
        List[Sentence]: AssemblyAI 返回的句子对象列表
    """
    # 1. 检查文件是否存在
    filepath = os.path.abspath(filename)
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"找不到音频文件: {filepath}")

    # 2. 获取并设置 API Key
    api_key = ASSEMBLYAI_API_KEY
    if not api_key:
        raise ValueError(f"未找到环境变量 请确保已设置API密钥")

    logger.info(f"开始使用 AssemblyAI 转录音频: {filename}")

    # 3. 新建转录器并上传文件
    transcriber = aai.Transcriber()
    try:
        # 直接把文件路径传给 SDK，让它处理上传和转写
        transcript = transcriber.transcribe(filepath)
    except Exception as e:
        logger.error(f"转写失败: {e}", exc_info=True)
        raise

    logger.info("音频转写完成")

    # 4. 提取并返回句子列表
    try:
        sentences = transcript.get_sentences()
    except AttributeError:
        # 如果 SDK 版本稍有不同，也可尝试 transcript.sentences
        sentences = getattr(transcript, "sentences", [])
    return sentences


def convert_AssemblyAI_to_srt(sentences):
    """
    AssemblyAI配套函数
    将句子列表转换为SRT格式的字幕

    参数:
        sentences: 包含text, start和end属性的句子对象列表

    返回:
        SRT格式的字符串
    """
    srt_content = ""

    for i, sentence in enumerate(sentences, 1):
        # 将毫秒转换为SRT时间格式 (HH:MM:SS,mmm)
        start_time = format_time_AssemblyAI(sentence.start)
        end_time = format_time_AssemblyAI(sentence.end)

        # 创建SRT条目
        srt_content += f"{i}\n"
        srt_content += f"{start_time} --> {end_time}\n"
        srt_content += f"{sentence.text}\n\n"

    return srt_content.strip()


def format_time_AssemblyAI(milliseconds):
    """
    将毫秒转换为SRT时间格式 (HH:MM:SS,mmm)

    参数:
        milliseconds: 毫秒数

    返回:
        格式化的时间字符串
    """
    # 转换为合适的单位
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    # 返回格式化的时间字符串
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def json_to_srt(data):
    """从 JSON 数据中提取 segments 字段，转换为 SRT 格式的文本"""
    srt_lines = []
    for idx, segment in enumerate(data.get("segments", []), start=1):
        start_time = format_time(segment["start"])
        end_time = format_time(segment["end"])
        text = segment["text"]
        srt_lines.append(str(idx))
        srt_lines.append(f"{start_time} --> {end_time}")
        srt_lines.append(text)
        srt_lines.append("")  # 添加空行分隔不同字幕段
    return "\n".join(srt_lines)


def extract_asr_sentences(srt_content):
  """
  从 SRT 格式的字幕文本中提取英文句子，并将其存储在一个带有序号的字典中。

  Args:
    srt_content: SRT 格式的字幕文本字符串。

  Returns:
    一个字典，键是句子序号，值是对应的英文句子。
  """
  sentences = {}
  pattern = r"(\d+)\n.*? --> .*?\n(.*?)\n"  # 正则表达式匹配句子序号和内容
  matches = re.findall(pattern, srt_content, re.DOTALL)
  for match in matches:
      sentences[int(match[0])] = match[1].strip()
  return sentences


def sentences_to_plain_text(sentences):
    """将按编号存储的英文字幕转为供 LLM 使用的纯文本。

    句子按编号排序，去掉首尾空白并跳过空句，相邻句子之间保留一个空行。
    """
    return "\n\n".join(
        text.strip()
        for _, text in sorted(sentences.items())
        if text and text.strip()
    )
