import os
import json
import asyncio
import re
import yt_dlp
import datetime
import threading

from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from openai import AsyncOpenAI
import aiohttp
from functools import wraps
import openai
import httpx
import assemblyai as aai

from app.common.core.config import DEEPSEEK_API_KEY, RETRY_ATTEMPTS, BATCH_SIZE, TRANSLATE_BATCH_SIZE, MAX_CONCURRENT_TASKS, API_TIMEOUT, ASSEMBLYAI_API_KEY, PROXY_URL
from app.common.core.logging import logger

# 全局调试记录存储
debug_records = []
debug_lock = threading.Lock()

def add_debug_record(video_id, chunk_info, input_data, output_data, result_info, attempt_type="initial"):
    """
    添加调试记录到全局列表
    
    Args:
        video_id: 视频ID
        chunk_info: 块信息 (first_item_number, end_item_number, expected_lines)
        input_data: 输入数据 (system_prompt, user_content)
        output_data: 输出数据 (raw_response, parsed_json, actual_lines)
        result_info: 结果信息 (success, line_count_match, error_message)
        attempt_type: 尝试类型 ("initial", "retry")
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    record = {
        'timestamp': timestamp,
        'video_id': video_id,
        'chunk_first': chunk_info['first'],
        'chunk_end': chunk_info['end'],
        'attempt_type': attempt_type,
        'content': f"""[{timestamp}] VIDEO: {video_id} | CHUNK: {chunk_info['first']}-{chunk_info['end']} | ATTEMPT: {attempt_type}
INPUT_LINES: {chunk_info['expected']}
{input_data['user_content']}
---
OUTPUT_LINES: {output_data.get('actual_lines', 'ERROR')} (Expected: {chunk_info['expected']})
{output_data.get('formatted_output', 'ERROR')}
---
RESULT: {'SUCCESS' if result_info['success'] else 'FAILED'} ({chunk_info['expected']}→{output_data.get('actual_lines', '?')}) {result_info.get('error_message', '')}
{'='*80}
"""
    }
    
    with debug_lock:
        debug_records.append(record)

def get_debug_records_text():
    """
    获取所有调试记录的文本内容，按CHUNK编号排序
    
    Returns:
        str: 格式化的调试记录文本
    """
    with debug_lock:
        if not debug_records:
            return "No debug records found.\n"
        
        # 按CHUNK编号排序：先按chunk_first，再按attempt_type (initial在前，retry在后)
        sorted_records = sorted(debug_records, key=lambda x: (
            x['chunk_first'], 
            0 if x['attempt_type'] == 'initial' else 1,  # initial排在retry前面
            x['timestamp']
        ))
        
        header = f"""=== LLM Translation Debug Records ===
Generated: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Total Records: {len(debug_records)}
Sorted by: CHUNK number, then attempt type (initial → retry)
{'='*80}

"""
        return header + '\n'.join(record['content'] for record in sorted_records)

def clear_debug_records():
    """清空调试记录"""
    with debug_lock:
        debug_records.clear()

async def save_debug_records_to_storage(video_id, bucket):
    """
    将调试记录保存到 Firebase Storage
    
    Args:
        video_id: 视频ID
        bucket: Firebase Storage bucket 实例
    
    Returns:
        str: 上传后的文件URL，如果没有记录则返回None
    """
    try:
        debug_text = get_debug_records_text()
        
        # 如果没有调试记录，直接返回
        if debug_text.strip() == "No debug records found.":
            logger.info("没有调试记录需要保存")
            return None
        
        # 上传到 Firebase Storage
        debug_blob = bucket.blob(f"debug_records/{video_id}_translation_debug.txt")
        debug_blob.upload_from_string(
            debug_text,
            content_type="text/plain; charset=utf-8"
        )
        
        logger.info(f"调试记录已保存到 Firebase Storage: debug_records/{video_id}_translation_debug.txt")
        return debug_blob.public_url
        
    except Exception as e:
        logger.error(f"保存调试记录到 Firebase Storage 失败: {str(e)}", exc_info=True)
        return None


def download_audio_webm(url, file_path):
    """
    从指定 URL 下载音频（仅下载 webm 格式的音频流）
    
    参数:
        url (str): 媒体资源的 URL
        file_path (Path): 保存音频的路径
        
    返回:
        Path: 下载后的音频文件路径
    """
    try:
        logger.info(f"开始下载视频: {url}")
        
        ydl_opts = {
            'format': 'bestaudio[ext=webm]',
            'outtmpl': str(file_path),
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
            'force_ipv4': True,
            'proxy': 'socks5://8t4v58911-region-US-sid-JaboGcGm-t-5:wl34yfx7@us2.cliproxy.io:443'
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        logger.info(f"视频下载完成: {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"下载失败: {str(e)}", exc_info=True)
        raise


def get_video_info(url):
    """
    获取YouTube视频信息
    
    参数:
        url (str): YouTube URL
        
    返回:
        dict: 视频信息字典
    """
    try:
        logger.info(f"获取视频信息: {url}")
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'forcejson': True,
            'force_ipv4': True,
            'proxy': 'socks5://8t4v58911-region-US-sid-JaboGcGm-t-5:wl34yfx7@us2.cliproxy.io:443',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        # 确保返回的信息中包含视频ID
        video_data = {
            'title': info.get('title', 'Unknown'),
            'id': info.get('id', ''),  # 提取视频ID
            'channel': info.get('channel', 'Unknown'),
            'duration': info.get('duration', 0),
            # 其他需要的信息...
        }
        
        logger.info(f"获取视频信息成功: {video_data['title']}, ID: {video_data['id']}")
        return video_data
    except Exception as e:
        logger.error(f"获取视频信息失败: {str(e)}", exc_info=True)
        raise

# 250403更新：新增get_video_info_and_download函数，同时获取信息并下载音频
async def get_video_info_and_download_async(url, file_path):
    """
    异步获取YouTube视频信息并下载

    参数:
        url (str): YouTube URL
        file_path (str/Path): 目标文件路径

    返回:
        dict: 视频信息字典
    """
    logger.info("任务开始! 音频下载中...")
    
    # 定义一个同步函数用于在线程中执行
    def download_video():
        logger.info(f"开始在单独线程中下载视频: {url}")
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio[ext=webm]',
            'outtmpl': str(file_path),
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            },
            'force_ipv4': True,
            #'proxy': 'socks5://8t4v58911-region-US-sid-JaboGcGm-t-5:wl34yfx7@us2.cliproxy.io:443',
        }
        if PROXY_URL:
            ydl_opts['proxy'] = PROXY_URL
            logger.info(f"使用代理: {PROXY_URL}")

        print(file_path)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        logger.info(f"视频下载完成: {file_path}")
        return info
    
    # 使用asyncio.to_thread在单独的线程中执行下载操作
    logger.info("开始在单独线程中执行下载操作")
    info = await asyncio.to_thread(download_video)
    
    # 处理并返回视频信息
    video_data = {
        'title': info.get('title', 'Unknown'),
        'id': info.get('id', ''),  # 提取视频ID
        'channel': info.get('channel', 'Unknown'), 
        'duration': info.get('duration', 0),
        # 其他需要的信息...
    }
    
    return video_data


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((yt_dlp.utils.DownloadError, OSError, ConnectionError))
)
def get_video_info_and_download(url):
    """
    从 YouTube 下载音频到当前工作目录，文件名为 <video_id>.webm，
    并返回视频信息和下载后的文件名。

    Args:
        url (str): YouTube 视频链接

    Returns:
        tuple:
            video_data (dict): 包含 title, id, channel
            filename (str): 下载到本地的文件名 (例如 "abc123.webm")
    """
    logger.info("任务开始！音频下载中...")

    # 直接在当前目录下，以视频 ID 作为文件名，后缀由 format 决定（这里固定 webm）
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio[abr<=128]/bestaudio',
        'outtmpl': '%(id)s.%(ext)s',
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
        },
        'force_ipv4': True,
        # 🔥 添加超时控制
        'socket_timeout': 60,  # 60秒连接超时
        'retries': 2,  # yt-dlp内部重试2次
    }
    if PROXY_URL:
        ydl_opts['proxy'] = PROXY_URL
        logger.info(f"使用代理: {PROXY_URL}")

    logger.info(f"开始下载视频: {url}")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 🔥 先提取信息，检查视频可用性
            info = ydl.extract_info(url, download=False)
            
            # 检查视频状态
            if info.get('is_live'):
                raise ValueError("不支持直播视频")
            if info.get('availability') in ['private', 'premium_only', 'subscriber_only']:
                raise ValueError(f"视频不可访问: {info.get('availability')}")
            
            # 提取并下载
            info = ydl.extract_info(url, download=True)
            # ydl.prepare_filename 会使用 outtmpl 规则，返回实际写入的文件路径
            filepath = ydl.prepare_filename(info)

        video_id = info.get('id', '')
        # filepath 可能包含路径，这里只取文件名
        filename = os.path.basename(filepath)

        video_data = {
            'title': info.get('title', 'Unknown'),
            'id': video_id,
            'channel': info.get('channel', 'Unknown'),
        }

        logger.info(f"视频下载完成: {filename}")
        return video_data, filename
        
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"yt-dlp下载错误: {error_msg}")
        
        # 🔥 分类处理不同类型的下载错误
        if any(keyword in error_msg.lower() for keyword in [
            "bytes missing", "eoferror", "connection reset", "timeout",
            "http error 5", "temporary failure"
        ]):
            # 这些是临时性网络错误，可以重试
            logger.warning(f"检测到临时性网络错误，将重试: {error_msg}")
            raise  # 让tenacity重试
        else:
            # 永久性错误，不重试
            logger.error(f"检测到永久性错误，不重试: {error_msg}")
            raise ValueError(f"视频下载失败: {error_msg}")
            
    except Exception as e:
        logger.error(f"视频下载异常: {str(e)}")
        raise


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


def format_time(seconds):
    """将秒数转换为 SRT 格式的时间字符串，格式为 hh:mm:ss,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


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

# 250403更新：新增extract_asr_sentences函数
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


# 通用的异步重试装饰器
def async_retry(max_attempts=None, exceptions=None):
    """智能异步函数重试装饰器"""
    if max_attempts is None:
        max_attempts = RETRY_ATTEMPTS
    if exceptions is None:
        exceptions = (Exception,)
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    error_msg = str(e).lower()
                    
                    # 🔥 智能错误分类：某些错误不值得重试
                    non_retryable_errors = [
                        "invalid api key", "authentication failed", "permission denied",
                        "model not found", "invalid request", "quota exceeded",
                        "content policy violation", "invalid json", "malformed request"
                    ]
                    
                    if any(err in error_msg for err in non_retryable_errors):
                        logger.error(f"检测到不可重试错误: {str(e)}")
                        raise e
                    
                    # 🔥 动态调整等待时间
                    if "rate limit" in error_msg or "too many requests" in error_msg:
                        # 限流错误：更长等待时间
                        wait_time = min(5 * (2 ** attempt), 60)
                    elif "timeout" in error_msg or "connection" in error_msg:
                        # 网络错误：标准等待时间
                        wait_time = min(2 * (2 ** attempt), 16)
                    else:
                        # 其他错误：快速重试
                        wait_time = min(1 * (2 ** attempt), 8)
                    
                    if attempt < max_attempts - 1:  # 不是最后一次尝试
                        logger.warning(f"尝试 {attempt+1}/{max_attempts} 失败: {str(e)}，等待 {wait_time}秒后重试")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"所有重试已用尽，最终失败: {str(e)}")
                        
            # 所有重试都失败了
            raise last_exception or Exception("最大重试次数已用尽")
        return wrapper
    return decorator


@async_retry()
async def safe_api_call_async(client, messages, model):
    """安全的异步API调用，内置重试机制"""
    api_type = "OpenAI" if "gpt" in model.lower() else "DeepSeek"
    
    try:
        logger.info(f"开始调用{api_type} API, 模型:{model}")
        
        # 使用传入客户端发送请求
        response = await client.chat.completions.create(
            model=model,
            response_format={'type': "json_object"},
            messages=messages,
            temperature=0.3,
            top_p=0.7,
            frequency_penalty=0,
            presence_penalty=0,
        )

        # 检查响应结构
        if not hasattr(response, 'choices') or len(response.choices) == 0:
            logger.error(f"无效的API响应结构: {response}")
            raise ValueError("无效的API响应结构")

        message = response.choices[0].message
        if not hasattr(message, 'content'):
            logger.error(f"响应中缺少翻译内容: {message}")
            raise ValueError("响应中缺少翻译内容")

        # 预验证JSON格式
        try:
            json_content = json.loads(message.content)
            logger.debug(f"API调用成功返回有效JSON")
        except json.JSONDecodeError as e:
            logger.error(f"JSON预验证失败: {message.content}")
            raise

        return response

    except openai.APIConnectionError as e:
        # 记录连接错误详情
        import traceback
        
        # 获取错误代码和HTTP状态码
        status_code = getattr(e, 'status_code', 'unknown')
        error_code = getattr(e, 'code', 'unknown')
        
        # 获取底层异常详情
        cause = e.__cause__ if hasattr(e, '__cause__') else None
        cause_type = type(cause).__name__ if cause else 'None'
        cause_str = str(cause) if cause else 'None'
        
        # 输出详细错误信息
        logger.error(f"{api_type} API连接错误详情: {str(e)}")
        logger.error(f"状态码: {status_code}, 错误码: {error_code}")
        logger.error(f"底层异常: {cause_type}: {cause_str}")
        logger.error(f"堆栈跟踪: {traceback.format_exc()}")
        
        # 重新抛出异常
        raise
        
    except openai.APITimeoutError as e:
        logger.error(f"{api_type} API超时: {str(e)}")
        logger.error(f"超时详情: {traceback.format_exc()}")
        raise
        
    except openai.RateLimitError as e:
        # 记录限流错误详情
        status_code = getattr(e, 'status_code', 'unknown')
        error_code = getattr(e, 'code', 'unknown')
        
        logger.error(f"{api_type} API速率限制: {str(e)}")
        logger.error(f"状态码: {status_code}, 错误码: {error_code}")
        raise
        
    except openai.APIResponseValidationError as e:
        # 记录响应验证错误详情
        status_code = getattr(e, 'status_code', 'unknown')
        error_code = getattr(e, 'code', 'unknown')
        
        logger.error(f"{api_type} API响应验证错误: {str(e)}")
        logger.error(f"状态码: {status_code}, 错误码: {error_code}")
        raise
        
    except openai.AuthenticationError as e:
        # 记录验证错误详情
        status_code = getattr(e, 'status_code', 'unknown')
        error_code = getattr(e, 'code', 'unknown')
        
        logger.error(f"{api_type} API验证错误: {str(e)}")
        logger.error(f"状态码: {status_code}, 错误码: {error_code}")
        raise
        
    except openai.BadRequestError as e:
        # 记录请求错误详情
        status_code = getattr(e, 'status_code', 'unknown')
        error_code = getattr(e, 'code', 'unknown')
        param = getattr(e, 'param', 'unknown')
        
        logger.error(f"{api_type} API请求错误: {str(e)}")
        logger.error(f"状态码: {status_code}, 错误码: {error_code}, 参数: {param}")
        raise
        
    except Exception as e:
        # 记录其他异常
        logger.error(f"异步API调用失败: {str(e)}")
        logger.error(f"异常类型: {type(e).__name__}")
        logger.error(f"堆栈跟踪: {traceback.format_exc()}")
        raise


def generate_custom_prompt(video_title: str, channel_name: str, custom_prompt: str) -> str:
    """
    根据视频标题和频道名生成自定义提示
    
    参数:
        video_title (str): 视频标题
        channel_name (str): 频道名称
        
    返回:
        str: 格式化的提示字符串
    """
    if custom_prompt:
        full_custom_prompt = f"{custom_prompt}\n\nvideo title: {video_title}\nchannel name: {channel_name}"
    else:
        full_custom_prompt = f"video title: {video_title}\nchannel name: {channel_name}"
    return full_custom_prompt


# ============ P000 翻译对齐核心（确定性，零额外 LLM 调用） ============
# 修复前两个错位源：process_transdict_num 按位置重编号、失败兜底把整批塌缩成一个 key。
# 修复后保证：英文第 N 行的译文只会来自 LLM 返回里 key 为 N 的那一项；
# LLM 没给 N 就标占位、不前移、不塌缩。详见 FIXES.md 的 P000 一节。
PLACEHOLDER = "[未翻译]"


def _is_valid_translation(value):
    """是否为一条有效译文：非空字符串。空/缺失/非字符串都算没翻出来。"""
    return isinstance(value, str) and value.strip() != ""


def _safe_json_loads(raw):
    """把 LLM 返回解析成 dict；解析失败或不是 dict 就返回空 dict（当作整批漏译，交给修复/占位）。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _try_relative_alignment(llm_json, expected_numbers):
    """确定性兜底：LLM 没用绝对行号、而是用了 1..K 相对编号且数量恰好吻合时，
    按相对位置一一映射回绝对行号。结构必须完全吻合（key 恰为 1..K 且每条有效），
    否则返回 None（绝不靠猜，宁可走 LLM 修复）。"""
    k = len(expected_numbers)
    try:
        keys = sorted(int(str(key).strip()) for key in llm_json.keys())
    except (ValueError, TypeError):
        return None
    if keys != list(range(1, k + 1)):
        return None
    aligned = {}
    for offset, n in enumerate(expected_numbers, start=1):
        value = llm_json.get(str(offset), llm_json.get(offset))
        if not _is_valid_translation(value):
            return None
        aligned[n] = value
    return aligned


def align_translation_by_key(llm_json, expected_numbers):
    """按 LLM 实际返回的 key 把译文对齐到期望的绝对行号（确定性，不调用 LLM）。

    参数:
        llm_json: LLM 返回并解析后的 dict（key 可能漏、可能多、可能跳号/相对编号）。
        expected_numbers: 本批期望的绝对行号列表（int，升序）。

    返回:
        (aligned, missing)
        aligned: dict[int, str]，命中且有效的行号 → 译文。
        missing: list[int]，没拿到有效译文的行号（按 expected 顺序）。

    保证：绝不把某行译文挪到别的行号下；漏的行只会进 missing，不会被后面的行顶替。
    """
    norm = {}
    if isinstance(llm_json, dict):
        for key, value in llm_json.items():
            norm[str(key).strip()] = value

    aligned = {}
    missing = []
    for n in expected_numbers:
        value = norm.get(str(n))
        if _is_valid_translation(value):
            aligned[n] = value
        else:
            missing.append(n)

    # 主对齐一条都没命中，但可能是 LLM 整体用了相对编号 → 确定性救回
    if aligned == {} and missing and isinstance(llm_json, dict):
        relative = _try_relative_alignment(llm_json, expected_numbers)
        if relative is not None:
            logger.warning(
                f"对齐兜底：LLM 用了相对编号 1..{len(expected_numbers)}，"
                f"已按位置映射回 {expected_numbers[0]}..{expected_numbers[-1]}"
            )
            return relative, []

    return aligned, missing


def _build_repair_prompt(missing_numbers, first_item_number, end_item_number, total):
    """缺行时给 LLM 的纠错反馈：整批重译，并点名上次漏了哪些行号、必须用绝对行号当 key。"""
    nums = ", ".join(str(n) for n in missing_numbers)
    return (
        f"Your previous response did not cover all lines correctly. "
        f"You missed or left empty these line numbers: {nums}.\n\n"
        f"Translate ALL subtitle lines from {first_item_number} to {end_item_number} again "
        f"({total} lines total). Use the EXACT original line number shown before each English "
        f"line as the JSON key — do NOT renumber starting from 1. Your JSON must contain exactly "
        f"{total} keys, covering every line number from {first_item_number} to {end_item_number} "
        f"with no omission and no merging."
    )


def _record_align(video_id, chunk_info, input_data, raw, aligned, missing, attempt_type):
    """把一次（对齐后的）翻译尝试写进调试记录，沿用 add_debug_record 的结构。"""
    raw_text = raw if isinstance(raw, str) else str(raw)
    formatted = '\n'.join(f"{k}: {aligned[k]}" for k in sorted(aligned))
    output_data = {
        'raw_response': raw_text[:500] + ("..." if len(raw_text) > 500 else ""),
        'actual_lines': len(aligned),
        'formatted_output': formatted,
    }
    result_info = {
        'success': len(missing) == 0,
        'line_count_match': len(aligned) == chunk_info.get('expected'),
        'error_message': '对齐通过' if not missing else f"缺{len(missing)}行: {missing[:20]}",
    }
    add_debug_record(video_id, chunk_info, input_data, output_data, result_info, attempt_type)


def _record_align_error(video_id, chunk_info, input_data, err, attempt_type):
    """LLM 调用本身抛异常时的调试记录。"""
    add_debug_record(
        video_id, chunk_info, input_data,
        {'raw_response': '', 'actual_lines': 'ERROR', 'formatted_output': f'调用失败: {err}'},
        {'success': False, 'line_count_match': False, 'error_message': f'调用失败: {err}'},
        attempt_type,
    )


def audit_translation_alignment(expected_numbers, translated_dict, video_id="unknown"):
    """全局对齐审计（可见性兜底）：翻译全部完成后，检查每个输入行号是否都有译文、
    哪些是占位 PLACEHOLDER。把问题收集成一句清晰日志，让作者一眼看到"这个视频第 X 行
    没翻出来"，而不是默默错位等看视频时才发现。

    返回 dict: {total, translated, missing_keys, placeholder_keys}
    """
    expected_set = set(expected_numbers)
    got_set = set(translated_dict.keys())
    missing_keys = sorted(expected_set - got_set)  # 修复后理论上应为空（process_chunk 已保证逐批补齐/占位）
    placeholder_keys = sorted(k for k, v in translated_dict.items() if v == PLACEHOLDER)
    total = len(expected_set)
    ok = total - len(missing_keys) - len(placeholder_keys)
    summary = {
        'total': total,
        'translated': ok,
        'missing_keys': missing_keys,
        'placeholder_keys': placeholder_keys,
    }
    if missing_keys or placeholder_keys:
        logger.warning(
            f"[对齐审计] 视频 {video_id}: 共 {total} 行，成功 {ok} 行；"
            f"缺键 {len(missing_keys)} 行: {missing_keys[:30]}；"
            f"占位 {len(placeholder_keys)} 行: {placeholder_keys[:30]}"
        )
    else:
        logger.info(f"[对齐审计] 视频 {video_id}: 共 {total} 行，全部对齐成功")
    return summary


# 250417更新
async def process_chunk(chunk, custom_prompt, model, client, semaphore, system_prompt_template, video_id="unknown"):
    """处理单个翻译批次（P000 修复版）。

    保证（由代码构造保证，不依赖 LLM 老实）：
      返回 translations 的 key 集合 == 本批期望行号集合；
      每个行号要么是真译文、要么是占位符 PLACEHOLDER；
      绝不塌缩成单键，绝不让译文前移顶替别的行号。

    流程：调 LLM → 按 key 确定性对齐 → 仍缺行则"整批带纠错反馈重译"(RETRY_ATTEMPTS 次内) →
    仍缺的逐行标占位。"重译"只在代码已确认缺行时触发，不是对每批都做的全量验证层。
    """
    async with semaphore:
        result = {'translations': {}}

        expected_numbers = [number for number, _ in chunk]
        if not expected_numbers:
            return result

        chunk_string = ''.join(f"{number}: {sentence}\n" for number, sentence in chunk)
        check_chunk_string = len(expected_numbers)
        first_item_number = expected_numbers[0]
        end_item_number = expected_numbers[-1]

        # 格式化系统提示模板
        trans_json_user_prompt = system_prompt_template.format(
            custom_prompt=custom_prompt,
            first_item_number=first_item_number,
            end_item_number=end_item_number,
            check_chunk_string=check_chunk_string
        )

        chunk_info = {'first': first_item_number, 'end': end_item_number, 'expected': check_chunk_string}
        input_data = {'system_prompt': trans_json_user_prompt, 'user_content': chunk_string.strip()}

        aligned = {}
        missing = list(expected_numbers)
        last_raw = ''

        # ---------- 初次翻译 ----------
        try:
            response = await safe_api_call_async(
                client=client,
                messages=[
                    {"role": "system", "content": trans_json_user_prompt},
                    {"role": "user", "content": chunk_string}
                ],
                model=model
            )
            last_raw = response.choices[0].message.content
            aligned, missing = align_translation_by_key(_safe_json_loads(last_raw), expected_numbers)
            if not missing:
                logger.info(f'编号{first_item_number}一次性通过')
            else:
                logger.info(f'编号{first_item_number}首次缺{len(missing)}行，进入纠错重译')
            _record_align(video_id, chunk_info, input_data, last_raw, aligned, missing, "initial")
        except Exception as main_error:
            logger.error(f"编号{first_item_number}初次翻译调用失败: {str(main_error)}")
            _record_align_error(video_id, chunk_info, input_data, str(main_error), "initial")

        # ---------- 缺行 → 整批带纠错反馈重译（确定性触发，非全量验证） ----------
        attempt = 0
        while missing and attempt < RETRY_ATTEMPTS:
            attempt += 1
            repair_prompt = _build_repair_prompt(missing, first_item_number, end_item_number, check_chunk_string)
            repair_user = chunk_string + "\n\n" + repair_prompt
            repair_input = {'system_prompt': trans_json_user_prompt, 'user_content': repair_user}
            try:
                repair_resp = await safe_api_call_async(
                    client=client,
                    messages=[
                        {"role": "system", "content": trans_json_user_prompt},
                        {"role": "user", "content": repair_user}
                    ],
                    model=model
                )
                last_raw = repair_resp.choices[0].message.content
                repair_aligned, _ = align_translation_by_key(_safe_json_loads(last_raw), expected_numbers)
                # 只补此前仍缺的行，不覆盖已对齐好的译文
                for n in list(missing):
                    if n in repair_aligned:
                        aligned[n] = repair_aligned[n]
                missing = [n for n in expected_numbers if n not in aligned]
                _record_align(video_id, chunk_info, repair_input, last_raw, aligned, missing, f"repair{attempt}")
                logger.info(f"编号{first_item_number}第{attempt}次纠错重译后仍缺{len(missing)}行")
            except Exception as repair_error:
                logger.error(f"编号{first_item_number}第{attempt}次纠错重译调用失败: {str(repair_error)}")
                _record_align_error(video_id, chunk_info, repair_input, str(repair_error), f"repair{attempt}")
                break

        # ---------- 仍缺的逐行占位（永不塌缩、永不前移） ----------
        if missing:
            logger.warning(f"编号{first_item_number}最终仍有{len(missing)}行未翻译，标占位: {missing[:20]}")
        for n in missing:
            aligned[n] = PLACEHOLDER

        # ---------- 标点清洗后写回（key 为 int，集合恰等于 expected_numbers） ----------
        cleaned = process_translated_string({str(n): aligned[n] for n in expected_numbers})
        result['translations'].update(cleaned)
        return result


# 处理翻译之后的字符串
def process_translated_string(translated_json):
    # 定义用于匹配中文标点的正则表达式
    chinese_punctuation = r"[\u3000-\u303F\uFF01-\uFFEF<>]"

    # 重新构建带序号的句子格式
    translated_dict = {}

    for number, sentence in translated_json.items():
        # 删除中文标点符号
        sentence = re.sub(chinese_punctuation, ' ', sentence)

        number = int(number)
        # 最后保存成字典
        translated_dict[number] = sentence
    return translated_dict


# 处理翻译之后的字典编号，避免LLM输出的字典编号有误
def process_transdict_num(input_dict, start_num, end_num):
    processed_dict = {}
    for i, (key, value) in enumerate(input_dict.items(), start=start_num):
        new_key = str(i)
        if i <= end_num:
            processed_dict[new_key] = value
        else:
            break
    return processed_dict


# 将原始英文字幕转为字典
def subtitles_to_dict(subtitles):
    """
    Parse subtitles that include a number, a time range, and text.
    Returns a dictionary with numbers as keys and a tuple (time range, text) as values.
    """
    subtitles_dict = {}
    lines = subtitles.strip().split("\n")
    current_number = None
    current_time_range = ""
    current_text = ""

    for line in lines:
        if line.isdigit():
            if current_number is not None:
                subtitles_dict[current_number] = (current_time_range, current_text.strip())
            current_number = int(line)
        elif '-->' in line:
            current_time_range = line
            current_text = ""
        else:
            current_text += line + " "

    subtitles_dict[current_number] = (current_time_range, current_text.strip())

    return subtitles_dict


# 将合并后的英文句子与原始英文字幕做匹配，给合并后的英文添加上时间戳
def map_marged_sentence_to_timeranges(merged_content, subtitles):
    """
    For each merged sentence, find the corresponding subtitles and their time ranges by concatenating
    the subtitles sentences until they match the merged sentence, and merge the time ranges accordingly.
    This version correctly handles multiple merged sentences.
    """
    merged_to_subtitles = {}
    subtitle_index = 0  # Keep track of the current position in the subtitles

    for num, merged_sentence in merged_content.items():
        corresponding_subtitles = []
        start_time = None
        end_time = None
        temp_sentence = ""

        while subtitle_index < len(subtitles):
            sub_num, (time_range, subtitle) = list(subtitles.items())[subtitle_index]
            if start_time is None:
                start_time = time_range.split(' --> ')[0]  # Set the start time of the first subtitle

            temp_sentence += subtitle + " "
            end_time = time_range.split(' --> ')[1]  # Update the end time with each subtitle added
            corresponding_subtitles.append(subtitle)

            # Check if the concatenated subtitles match the merged sentence
            if temp_sentence.strip() == merged_sentence:
                merged_time_range = f"{start_time} --> {end_time}"
                merged_to_subtitles[num] = (merged_time_range, temp_sentence)
                subtitle_index += 1  # Move to the next subtitle for the next iteration
                break

            subtitle_index += 1

    return merged_to_subtitles


def map_chinese_to_time_ranges_v2(chinese_content, merged_engsentence_to_subtitles):
    """
    给中文翻译添加时间轴，生成未经句子长度优化的初始中文字幕。

    参数:
        chinese_content (dict): 字典，key 为编号，value 为中文翻译字符串。
        merged_engsentence_to_subtitles (dict): 字典，key 为编号，value 为一个元组，格式为 (time_range, subtitle)。

    返回:
        dict: key 为编号，value 为一个字典，包含以下键:
              - "time_range": 原始时间区间字符串
              - "text": 对应的中文翻译
    """
    chinese_to_time = {}

    for num, chinese_sentence in chinese_content.items():
        # 如果当前编号在英文字幕合并结果中存在
        if num in merged_engsentence_to_subtitles:
            time_range, _ = merged_engsentence_to_subtitles[num]
            # 用自描述的字典结构保存信息
            chinese_to_time[num] = {
                "time_range": time_range,
                "text": chinese_sentence
            }

    # 防御性日志：修复 P000 后，正常情况下每行中文都应能匹配到时间轴。
    # 若仍有中文行被丢弃，说明上游行号与时间轴字典不一致，记日志以免静默丢行。
    if len(chinese_to_time) != len(chinese_content):
        dropped = sorted(set(chinese_content) - set(chinese_to_time))
        logger.warning(
            f"[时间轴映射] {len(dropped)} 行中文未匹配到时间轴被丢弃: {dropped[:30]}"
        )

    return chinese_to_time


def time_to_str(dt):
    """
    将 datetime 对象格式化为 SRT 字幕时间格式：HH:MM:SS,mmm
    """
    return dt.strftime("%H:%M:%S,%f")[:-3]


# 050403更新
# 使用分割后输入的字典内容
def format_subtitles_v2(subtitles_dict):
    formatted_str = ""
    num_counter = 1  # 初始化计数器
    for key in sorted(subtitles_dict.keys()):
        subtitle = subtitles_dict[key]
        formatted_str += f"{num_counter}\n"
        formatted_str += f"{subtitle['time_range']}\n"
        formatted_str += f"{subtitle['text']}\n\n"
        num_counter += 1
    return formatted_str

# 250403更新：发现两个没有用的函数
# async def robust_transcribe(file_path, max_attempts=3):
#     """
#     带有重试机制的音频转写函数，处理各种超时和网络错误（异步版本）
    
#     参数:
#         file_path (Path): 音频文件路径
#         max_attempts (int): 最大重试次数
        
#     返回:
#         dict: 转写结果
#     """
#     # 定义可以重试的异常类型
#     retriable_exceptions = (
#         httpx.ReadTimeout, 
#         httpx.ConnectTimeout,
#         httpx.ReadError,
#         httpx.NetworkError,
#         ConnectionError,
#         TimeoutError
#     )
    
#     # 重试装饰器（异步版本）
#     current_attempt = 0
#     last_exception = None
    
#     while current_attempt < max_attempts:
#         try:
#             logger.info(f"开始转写尝试 {current_attempt+1}/{max_attempts}...")
#             return await transcribe_audio(file_path)
#         except retriable_exceptions as e:
#             current_attempt += 1
#             last_exception = e
#             wait_time = min(2 ** current_attempt, 60)  # 指数退避
#             logger.info(f"第 {current_attempt}/{max_attempts} 次尝试失败，等待 {wait_time} 秒后重试...")
#             await asyncio.sleep(wait_time)
#         except Exception as e:
#             # 非重试类型异常，直接抛出
#             logger.error(f"转写失败，遇到非重试类型异常: {str(e)}", exc_info=True)
#             raise
    
#     # 如果所有尝试都失败
#     logger.error(f"所有转写尝试均失败: {str(last_exception)}", exc_info=True)
#     # 重新抛出异常，让调用者处理
#     raise last_exception or Exception("最大重试次数已用尽")

# 250403更新：发现两个没有用的函数
# 修改处理音频接口的调用方式
# async def process_audio(audio_path, output_dir, content_name, custom_prompt="", special_terms=""):
#     """
#     处理音频文件，包括转写和翻译
    
#     参数:
#         audio_path (Path): 音频文件路径
#         output_dir (Path): 输出目录
#         content_name (str): 内容名称
#         custom_prompt (str): 自定义提示
#         special_terms (str): 特殊术语
        
#     返回:
#         dict: 处理结果
#     """
#     try:
#         # 使用带重试功能的转写函数
#         transcription = await robust_transcribe(audio_path, max_attempts=3)
                
#         # 继续后续处理...
#         # ...
        
#         # 后续代码保持不变
#         # ...
        
#     except Exception as e:
#         logger.error(f"处理音频失败: {str(e)}", exc_info=True)
#         raise 


# 250403更新
# 全新的长句分割方法。对于无法按照规则分割的句子，调用异步LLM分割
# 注：原此处有一份与上方 time_to_str 完全相同的重复定义，已删除（重构 T2.2，行为不变）。
# 保留的唯一实现见本文件上方 time_to_str。

def parse_time_range(time_range_str):
    """
    解析形如 "HH:MM:SS,mmm --> HH:MM:SS,mmm" 的时间区间字符串，
    返回起始时间和结束时间对应的 datetime 对象。
    此处以 1900-01-01 为基础日期。
    """
    try:
        start_str, end_str = time_range_str.split(" --> ")
        base_date = datetime.date(1900, 1, 1)
        start_dt = datetime.datetime.strptime(f"{base_date} {start_str}", "%Y-%m-%d %H:%M:%S,%f")
        end_dt = datetime.datetime.strptime(f"{base_date} {end_str}", "%Y-%m-%d %H:%M:%S,%f")
        return start_dt, end_dt
    except Exception as e:
        logger.error(f"解析时间范围错误: {time_range_str}, 错误: {str(e)}")
        raise

def split_sentence(text):
    """
    对输入的中文句子进行分割：
    1. 只有长度大于20个字符的句子才进行分割（正好20个字符的不处理）；
    2. 以空格为分割标志，但仅当空格两边都是中文字符时进行分割；
    3. 分割后每一部分必须至少有5个字符（允许恰好5个字符）；
    4. 对长句子采用递归方式处理所有符合条件的分割点。
    """
    if len(text) <= 20:
        return [text]

    # pattern = re.compile(
    # r'(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9\u4e00-\u9fff])'
    # r'|(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])')
    #250506 修改分割规则，只分割中文字符之间的空格
    pattern = re.compile(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])')
    matches = list(pattern.finditer(text))

    for match in matches:
        left = text[:match.start()]
        right = text[match.end():]
        if len(left) >= 6 and len(right) >= 6:
            return [left] + split_sentence(right)

    return [text]

def assign_time_ranges(start_time, end_time, segments):
    """
    根据起始时间、结束时间和文本片段列表，计算每个片段对应的时间区间。
    返回列表中每个元素为元组：(起始时间字符串, 结束时间字符串, 文本片段)
    """
    total_duration = (end_time - start_time).total_seconds()
    total_chars = sum(len(seg) for seg in segments)
    
    if total_chars == 0:
        logger.warning("分配时间区间时发现总字符数为零，返回空列表")
        return []
    per_char_duration = total_duration / total_chars

    assigned_ranges = []
    current_time = start_time
    for seg in segments:
        seg_duration = len(seg) * per_char_duration
        new_end = current_time + datetime.timedelta(seconds=seg_duration)
        assigned_ranges.append((time_to_str(current_time), time_to_str(new_end), seg))
        current_time = new_end
    return assigned_ranges

async def split_long_chinese_sentence_v4(chinese_timeranges_dict):
    """
    处理 chinese_timeranges_dict 中的长文本，分两个阶段：

    第一阶段：初步处理
      - 使用 split_sentence 和 assign_time_ranges 对每条字幕进行分割，
      - 生成初步字幕字典 initial_subtitles。

    第二阶段：批量进一步处理
      - 筛选出初步字幕字典中需要进一步分割的条目（文本长度大于25）；
      - 批量调用 LLM 分割接口（batch_llm_process，占位符实现），
      - 对于每个需要处理的条目，依据其原始时间区间重新计算分割后的多个片段对应的时间区间，
      - 将原条目拆分为多条新的字幕，生成最终字幕字典。
    """
    logger.info(f"开始执行长句分割(v4)，输入字典大小: {len(chinese_timeranges_dict)}条")
    
    # 第一阶段：初步处理
    logger.info("第一阶段：使用规则分割和时间区间分配")
    initial_subtitles = {}
    new_index = 1
    
    phase1_split_count = 0  # 记录第一阶段分割的数量
    
    for key, item in chinese_timeranges_dict.items():
        time_range = item[0] if isinstance(item, tuple) else item.get("time_range")
        text = item[1] if isinstance(item, tuple) else item.get("text", "")
        
        logger.debug(f"处理字幕 #{key}: '{text[:30]}{'...' if len(text) > 30 else ''}', 时间范围: {time_range}")
        
        try:
            start_dt, end_dt = parse_time_range(time_range)
            segments = split_sentence(text)
            
            if len(segments) > 1:
                phase1_split_count += 1
                logger.debug(f"字幕 #{key} 被规则分割为 {len(segments)} 段")
            
            assigned_segments = assign_time_ranges(start_dt, end_dt, segments)
            
            for start_time_str, end_time_str, seg_text in assigned_segments:
                initial_subtitles[new_index] = {
                    "time_range": f"{start_time_str} --> {end_time_str}",
                    "text": seg_text
                }
                new_index += 1
        except Exception as e:
            logger.error(f"处理字幕 #{key} 时出错: {str(e)}")
            # 保留原始字幕，避免丢失内容
            initial_subtitles[new_index] = {
                "time_range": time_range,
                "text": text
            }
            new_index += 1

    logger.info(f"第一阶段完成: 处理 {len(chinese_timeranges_dict)} 条字幕，通过规则分割了 {phase1_split_count} 条，生成 {len(initial_subtitles)} 条初步字幕")

    # 第二阶段：批量处理需要进一步分割的字幕
    logger.info("第二阶段：使用LLM进一步分割长句子")
    keys_to_process = []
    texts_to_process = []
    
    # 这里以文本长度大于20作为需要进一步分割的条件
    for key, value in initial_subtitles.items():
        if len(value["text"]) > 20:
            keys_to_process.append(key)
            texts_to_process.append(value["text"])
    
    logger.info(f"需要通过LLM进一步分割的字幕: {len(keys_to_process)} 条")
    
    if texts_to_process:
        try:
            texts_to_llm = {str(i+1): text for i, text in enumerate(texts_to_process)}
            logger.info(f"开始调用LLM批量分割长句，共 {len(texts_to_llm)} 条")
            
            llm_results = await llm_batches_split(texts_to_llm)
            logger.info(f"LLM分割完成，返回 {len(llm_results.get('results', []))} 条结果")
            
            # 构建最终的字幕字典，拆分后的多条字幕需要重新计算时间区间
            final_subtitles = {}
            final_index = 1
            llm_split_count = 0  # 记录LLM成功分割的条目数
            
            # 遍历初步字幕字典，对需要进一步处理的条目做处理
            for key, value in initial_subtitles.items():
                if key in keys_to_process:
                    # 从当前字幕中获取原始文本
                    original_text = value["text"]
                    # 通过匹配 "original" 字段查找对应的 LLM 处理结果
                    matched_result = None
                    for result in llm_results.get("results", []):
                        if result.get("original") == original_text:
                            matched_result = result
                            break
                    
                    # 如果没有匹配到，直接使用原始文本作为唯一分割项
                    if matched_result is None:
                        logger.warning(f"未找到字幕 #{key} 的LLM分割结果，保持原样: '{original_text[:30]}{'...' if len(original_text) > 30 else ''}'")
                        segmented_texts = [original_text]
                    else:
                        segmented_texts = matched_result.get("segmented", [original_text])
                        if len(segmented_texts) > 1:
                            llm_split_count += 1
                            logger.debug(f"字幕 #{key} 被LLM分割为 {len(segmented_texts)} 段")

                    # 使用原始时间区间重新分配新的时间
                    try:
                        original_time_range = value["time_range"]
                        start_dt, end_dt = parse_time_range(original_time_range)
                        new_assigned_segments = assign_time_ranges(start_dt, end_dt, segmented_texts)
                        
                        for start_time_str, end_time_str, seg_text in new_assigned_segments:
                            final_subtitles[final_index] = {
                                "time_range": f"{start_time_str} --> {end_time_str}",
                                "text": seg_text
                            }
                            final_index += 1
                    except Exception as e:
                        logger.error(f"处理LLM分割结果时发生错误 (字幕 #{key}): {str(e)}")
                        # 保留原始字幕作为回退选项
                        final_subtitles[final_index] = value
                        final_index += 1
                else:
                    final_subtitles[final_index] = value
                    final_index += 1
            
            logger.info(f"LLM成功分割了 {llm_split_count}/{len(keys_to_process)} 条字幕")
            initial_subtitles = final_subtitles
            
        except Exception as e:
            logger.error(f"LLM批量分割过程中发生错误: {str(e)}")
            # 出错时保留第一阶段的结果
            logger.warning("由于LLM分割错误，保留第一阶段的分割结果")

    logger.info(f"长句分割(v4)完成: 输入 {len(chinese_timeranges_dict)} 条字幕，输出 {len(initial_subtitles)} 条分割后的字幕")
    return initial_subtitles

# 250403更新
# 异步LLM分割相关函数
# 通用的异步重试装饰器
def async_retry(max_attempts=None, exceptions=None):
    """异步函数的重试装饰器"""
    if max_attempts is None:
        max_attempts = RETRY_ATTEMPTS  # 替换为直接使用RETRY_ATTEMPTS配置，而不是CONFIG字典
    if exceptions is None:
        exceptions = (aiohttp.ClientError, json.JSONDecodeError, Exception)  # 修改为合适的异常类型
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    # 指数退避策略
                    wait_time = min(1 * (2 ** attempt), 8)  # 使用固定的退避策略参数
                    logger.warning(f"尝试 {attempt+1}/{max_attempts} 失败: {str(e)}，等待 {wait_time}秒后重试")
                    await asyncio.sleep(wait_time)
            # 所有重试都失败了
            logger.error(f"达到最大重试次数 {max_attempts}，最后错误: {str(last_exception)}")
            raise last_exception or Exception("最大重试次数已用尽")
        return wrapper
    return decorator

async def llm_batches_split(long_sentences, model='deepseek-chat'):
    """
    使用LLM分割长句子,创建异步任务

    参数：
    long_sentences: 需要分割的长句子字典
    model: 使用的模型名称

    返回：
    dict:分割结果字典,格式为
    {
    "results": [
        {"original": 原句1, "segmented": [句子1, 句子2]},
        {"original": 原句2, "segmented": [句子1, 句子2]}
    ]
    }
    """
    logger.info(f"开始使用LLM批量分割长句, 使用模型: {model}, 输入句子数: {len(long_sentences)}")
    
    total_segment_dict = {
        'results':[]
    }
    items = list(long_sentences.items())

    # 创建锁和信号量
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)  # 使用MAX_CONCURRENT_TASKS配置

    # 创建异步客户端（DeepSeek）
    client = AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )

    # 创建批次处理任务
    tasks = []
    for i in range(0, len(items), BATCH_SIZE):  # 使用导入的BATCH_SIZE
        chunk = items[i:i + BATCH_SIZE]
        tasks.append(
            split_process_chunk(chunk, model, client, semaphore)
        )
    
    logger.info(f"创建了 {len(tasks)} 个并行任务进行长句分割")

    # 并行执行所有任务
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 处理结果
    success_count = 0
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"批次处理失败: {str(result)}")
            continue
        
        # 更新分割结果
        if 'results' in result:
            total_segment_dict['results'].extend(result.get('results',[]))
            success_count += 1

    logger.info(f"LLM批量分割完成: {success_count}/{len(tasks)} 批次成功, 共处理 {len(total_segment_dict['results'])} 条句子")
    return total_segment_dict

@async_retry()
async def split_safe_api_call_async(client, messages, model, temperature, top_p, frequency_penalty, presence_penalty):
    """安全的异步API调用,内置重试机制"""
    try:
        response = await client.chat.completions.create(
            model=model,
            response_format={'type': "json_object"},
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
        )

        # 检查响应结构
        if not hasattr(response, 'choices') or len(response.choices) == 0:
            logger.error("API响应缺少choices字段")
            raise ValueError("无效的API响应结构")

        message = response.choices[0].message
        if not hasattr(message, 'content'):
            logger.error("API响应缺少content字段")
            raise ValueError("响应中缺少翻译内容")

        # 预验证JSON格式
        try:
            json.loads(message.content)
        except json.JSONDecodeError as e:
            logger.error(f"JSON预验证失败: {message.content[:100]}...")
            raise

        return response

    except openai.APIConnectionError as e:
        logger.error(f"API连接错误: {str(e)}")
        raise
    except openai.APITimeoutError as e:
        logger.error(f"API超时错误: {str(e)}")
        raise
    except openai.RateLimitError as e:
        logger.error(f"API速率限制错误: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"异步API调用失败: {str(e)}")
        raise

async def split_process_chunk(chunk, model, client, semaphore):
    """
    处理单个分割批次
    """
    async with semaphore:
        logger.debug(f"开始处理批次, 包含 {len(chunk)} 条句子")
        result = {'results': []}

        chunk_str = json.dumps(chunk, ensure_ascii=False)
        logger.debug(f"批次数据样本: {chunk_str[:100]}...")
        
        segment_prompt = f'''
                        请按以下规则处理三重反引号内的中文长句集合：
                        1. 输入格式示例：
                        {{"1":"句子1",
                        "2":"句子2"}}

                        2. 智能分割：
                        - 只拆分长句，不要改变句子的内容
                        - 分割时，请保持"语言完整性"
                        - 优先在空格处拆分，保持术语完整（如"NASA"、"5G NR"）
                        - 每短句10-15个字符，最多不要超过20个字符
                        3. 使用json格式输出：
                        {{
                        "results": [
                            {{
                            "original": "原句1",
                            "segmented": ["短句1", "短句2"]
                            }},
                            {{
                            "original": "原句2",
                            "segmented": ["短句1", "短句2"]
                            }}
                        ],
                        }}

                        需要处理的长句：```{chunk_str}```
                        '''

    response = await split_safe_api_call_async(
        client=client,
        messages=[
            {"role": "user", "content": segment_prompt}
        ],
        model=model,
        temperature=0,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0,
    )

    segment_results = response.choices[0].message.content
    result_to_json = json.loads(segment_results)
    result.update(result_to_json)

    return result

# 250403更新
async def translate_subtitles(numbered_sentences_chunks, custom_prompt, model_choice="deepseek", special_terms="", content_name="", video_id="unknown"):
    """
    统一的字幕翻译函数，支持不同模型选择
    
    Args:
        numbered_sentences_chunks: 编号的句子块
        custom_prompt: 自定义提示词
        model_choice: 已废弃/被忽略——模型对用户透明，全程固定 DeepSeek（保留形参仅为兼容旧签名）
        special_terms: 特殊术语列表
        content_name: 内容名称
    
    Returns:
        翻译后的字典
    """
    # 模型对用户透明：全程固定走 DeepSeek，不再按 model_choice 路由。
    # 保留 model_choice 形参仅为兼容旧调用签名，其值被忽略（OpenAI 路径已废弃，留 T7 清理）。
    return await translate_with_model(
        numbered_sentences_chunks,
        custom_prompt,
        model='deepseek-chat',
        api_key=DEEPSEEK_API_KEY,
        special_terms=special_terms,
        content_name=content_name,
        video_id=video_id
    )

async def translate_with_model(numbered_sentences_chunks, custom_prompt, model, api_key, special_terms="", content_name="", video_id="unknown"):
    """
    统一的模型翻译实现函数
    """
    items = list(numbered_sentences_chunks.items())
    total_translated_dict = {}

    # 处理特殊术语
    if special_terms:
        special_terms = special_terms.rstrip(".")
        special_terms_list = special_terms.split(", ")

    # 创建信号量
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    
    # 全程固定走 DeepSeek（模型对用户透明），不再按模型类型路由。
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",  # DeepSeek的API地址
        timeout=API_TIMEOUT
    )
    logger.info(f"使用DeepSeek API客户端，模型: {model}")

    # 获取适合当前模型的提示词
    system_prompt = get_system_prompt_for_model(model)

    # 创建批次处理任务
    tasks = []
    for i in range(0, len(items), TRANSLATE_BATCH_SIZE):
        chunk = items[i:i + TRANSLATE_BATCH_SIZE]
        tasks.append(
            process_chunk(chunk, custom_prompt, model, client, semaphore, system_prompt, video_id)
        )
        logger.debug(f"任务序号: {i}")
    
    # 并行执行所有任务
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 处理结果
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"批次处理失败: {str(result)}")
            continue
        
        # 更新翻译结果
        translations = result.get('translations', {})
        total_translated_dict.update(translations)

    logger.info(f'使用的模型：{model}')

    # P000 全局对齐审计：确认每个输入行号都有译文/占位，缺失或占位都记日志（可见性兜底）
    expected_numbers = [number for number, _ in items]
    audit_translation_alignment(expected_numbers, total_translated_dict, video_id)

    return total_translated_dict

def get_system_prompt_for_model(model):
    """
    返回 DeepSeek 的系统提示词模板（模型对用户透明，全程固定 DeepSeek）。
    """
    # DeepSeek 的提示词包含处理分割句子的详细说明
    return """
        # Role
        You are a skilled translator specializing in converting English subtitles into natural and fluent Chinese while maintaining the original meaning.

        # Background information of the translation content
        {custom_prompt}
        Please identify the professional domain of the video content based on the information provided above, and leverage domain-specific knowledge and terminology to deliver an accurate and contextually appropriate translation.

        ## Skills
        ### Skill 1: Line-by-Line Translation
        - Emphasize the importance of translating English subtitles line by line to ensure accuracy and coherence.
        - Strictly follow the rule of translating each subtitle line individually based on the input content.
        In cases where a sentence is split, such as:
        125.    But what's even more impressive is their U.S.
        126.    commercial revenue projection.

        Step 1: Merge the split sentence into a complete sentence.
        Step 2: Translate the merged sentence into Chinese.
        Step 3: When outputting the Chinese translation, insert the translated result into both of the original split sentence positions.

        Example of this process:
          125.    But what's even more impressive is their U.S.
        但更令人印象深刻的是他们的美国业务
          126.    commercial revenue projection.
        但更令人印象深刻的是他们的美国业务

        ### Skill 2: Contextual Translation
        - Consider the context of the video to ensure accuracy and coherence in the translation.
        - When slang or implicit information appears in the original text, do not translate it literally. Instead, adapt it to align with natural Chinese communication habits.

        ### Skill 3: Handling Complex Sentences
        - Rearrange word order and adjust wording for complex sentence structures to ensure translations are easily understandable and fluent in Chinese.

        ### Skill 4: Proper Nouns and Special Terms
        - Identify proper nouns and special terms enclosed in angle brackets < > within the subtitle text, and retain them in their original English form.

        ### Skill 5: Ignore spelling errors
        - The English content is automatically generated by ASR and may contain spelling errors. Please ignore such errors and translate normally when encountered.

        ## Constraints
        - For punctuation requirements: Do not add a period when the sentence ends
        - The provided subtitles range from line {first_item_number} to line {end_item_number}, totaling {check_chunk_string} lines.
        - CRITICAL — line numbering: each input line is prefixed with its own absolute line number (e.g. "{first_item_number}: ..."). Use that EXACT number as the JSON key; do NOT renumber starting from 1. Return exactly {check_chunk_string} keys so that every line number from {first_item_number} to {end_item_number} appears exactly once. If one sentence is split across two lines, put the same translation under both line numbers (still output both keys).
        - Provide the Chinese translation as a JSON object whose keys are the original line numbers, for example:
          ```
          {{
          "{first_item_number}": "<Chinese translation of line {first_item_number}>",
          "{end_item_number}": "<Chinese translation of line {end_item_number}>"
          }}
          ```
        """

# 0505更新 将title和channel name传给LLM，推断视频上下文信息。
@async_retry()
async def get_video_context_from_llm(title, channel_name):
    """
    让LLM通过title 和 channel name 推断视频上下文信息
    """
    try:
        logger.info("开始执行get_video_context_from_llm...")

        client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
            timeout=API_TIMEOUT
        )

        messages=[
                    {"role": "system", "content": "步骤1：判断该channel是否是在你的知识库中。如果你了解该channel的相关信息，输出它的相关信息\n例如：channel name ： 3Blue1Brown\n这个频道以动画可视化数学原理闻名\n要求：仔细检查你的知识库，如果你不知道这个channel则诚实的说不知道，不要编造信息。\n\n步骤2：结合video title和步骤1的信息，输出你对视频内容的推断。然后简要描述针对这个视频，该采取什么样的翻译策略。\n要求：如果无法从title和channel name中推断视频内容，请诚实的说不知道，不要编造信息。\n\n步骤3：综合步骤1、2，以第一人称的口吻给出简要的3个翻译策略或注意事项。\n\t1.\t明确本次翻译应采用的话语风格（如：正式、学术、轻松、幽默等），风格应贴合视频内容和目标观众；\n\t2.\t识别该视频中可能包含的专业领域术语，简要列举 2-3 个代表性术语，并指出它们在翻译中应保持准确性或采用贴近母语习惯的表达；\n\t3.\t可补充其他翻译技巧，但不得包含模板化建议，如“术语首次出现时进行注释或举例说明”这类通用表述应避免使用。\n要求：如果无法从步骤1、2推断视频内容，请诚实的说不知道，不要编造信息。\n\n使用中文输出所有内容\n使用如下json格式进行输出\n{\n\"step1\": {\n\"channel_name\": \"string\",\n\"channel_info\": \"string or null\",\n\"can_judge\": true\n},\n\"step2\": {\n\"video_title\": \"string\",\n\"content_inference\": \"string or null\",\n\"can_judge\": true\n},\n\"step3\": {\n\"translation_strategies\": [\n\"string or null\",\n\"string or null\",\n\"special_terms_strategies\"\n],\n\"can_judge\": true\n}"},
                    {"role": "user", "content": f"channel name: {channel_name}\nvideo title: {title}\n\n重要：无论频道名和视频标题是什么语言，step1、step2、step3 的所有输出文本（含 channel_info、content_inference、translation_strategies）都必须用简体中文。"}
                ]

        response = await client.chat.completions.create(
            model="deepseek-chat",
            response_format={'type': "json_object"},
            messages=messages,
            temperature=0.3,
            top_p=0.7
        )

        result = response.choices[0].message.content

        logger.info(f"get_video_context_from_llm 结果: {result}")


        # 检查响应结构
        if not hasattr(response, 'choices') or len(response.choices) == 0:
            logger.error("API响应缺少choices字段")
            raise ValueError("无效的API响应结构")

        message = response.choices[0].message
        if not hasattr(message, 'content'):
            logger.error("API响应缺少content字段")
            raise ValueError("响应中缺少翻译内容")

        # 预验证JSON格式
        try:
            json.loads(message.content)
        except json.JSONDecodeError as e:
            logger.error(f"JSON预验证失败: {message.content[:100]}...")
            raise

        return result

    except openai.APIConnectionError as e:
        logger.error(f"API连接错误: {str(e)}")
        raise
    except openai.APITimeoutError as e:
        logger.error(f"API超时错误: {str(e)}")
        raise
    except openai.RateLimitError as e:
        logger.error(f"API速率限制错误: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"异步API调用失败: {str(e)}")
        raise


def process_video_context_data(json_data):
    # 处理 get_video_context_from_llm 的输出
    if isinstance(json_data, str):
        data = json.loads(json_data)
    else:
        data = json_data
    
    # 任务1: 提取can_judge为true的字段，转为纯文本
    text_output = []
    
    # 遍历每个step
    for step_key, step_value in data.items():
        # 检查是否包含can_judge且为true
        if step_value.get("can_judge", False) == True:
            # 提取不同类型的字段
            if "channel_info" in step_value:
                text_output.append(step_value["channel_info"])
            if "content_inference" in step_value:
                text_output.append(step_value["content_inference"])
            if "translation_strategies" in step_value and isinstance(step_value["translation_strategies"], list):
                for strategy in step_value["translation_strategies"]:
                    text_output.append(strategy)
    
    # 将文本列表转换为换行分隔的字符串
    formatted_text = "\n".join(text_output)
    logger.info(f"process_video_context_data 任务1结果: {formatted_text}")
    # 任务2: 提取step3中的translation_strategies
    # 直接返回策略列表，避免不必要的嵌套
    translation_strategies = []
    if "step3" in data and "translation_strategies" in data["step3"]:
        translation_strategies = data["step3"]["translation_strategies"]
    logger.info(f"process_video_context_data 任务2结果: {translation_strategies}")
    
    # 直接返回两个独立的变量，而不是字典
    return formatted_text, translation_strategies