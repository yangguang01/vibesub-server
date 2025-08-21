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

from app.common.core.config import REPLICATE_API_TOKEN, DEEPSEEK_API_KEY, RETRY_ATTEMPTS, BATCH_SIZE, MAX_CONCURRENT_TASKS, API_TIMEOUT, OPENAI_API_KEY, ASSEMBLYAI_API_KEY,PROXY_URL
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

# 250403更新：直接使用asr_result中的句子，不再合并短句子。因此删除此函数
# def merge_incomplete_sentences(subtitles):
#     """将英文字幕中的内容合并为完整句子"""
#     # 按行分割字幕文本
    lines = [line.strip() for line in subtitles.split('\n') if line.strip()]

    # 存储合并后的句子
    merged_sentences = []
    current_sentence = ''

    for line in lines:
        if not line.isdigit() and '-->' not in line and line.strip() != '':
            # 添加当前行到当前句子
            current_sentence += ' ' + line if current_sentence else line

            # 检查是否为完整句子
            if any(current_sentence.endswith(symbol) for symbol in ['.', '?', '!']):
                merged_sentences.append(current_sentence)
                current_sentence = ''

    # 确保最后一句也被添加（如果它是完整的）
    if current_sentence:
        merged_sentences.append(current_sentence)

    # 将每个句子转换为字典，并添加序号
    numbered_and_sentences = {i: sentence for i, sentence in enumerate(merged_sentences, start=1)}

    return numbered_and_sentences

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

# 250417更新
async def process_chunk(chunk, custom_prompt, model, client, semaphore, system_prompt_template, video_id="unknown"):
    """处理单个翻译批次"""
    async with semaphore:
        result = {
            'translations': {}
        }
        
        chunk_string = ''.join(f"{number}: {sentence}\n" for number, sentence in chunk)
        check_chunk_string = chunk_string.count('\n')
        first_item_number = chunk[0][0] if chunk else "N/A"
        end_item_number = first_item_number + check_chunk_string - 1
        
        # 格式化系统提示模板
        trans_json_user_prompt = system_prompt_template.format(
            custom_prompt=custom_prompt,
            first_item_number=first_item_number,
            end_item_number=end_item_number,
            check_chunk_string=check_chunk_string
        )
        
        # 准备调试记录的基础信息
        chunk_info = {
            'first': first_item_number,
            'end': end_item_number,
            'expected': check_chunk_string
        }
        input_data = {
            'system_prompt': trans_json_user_prompt,
            'user_content': chunk_string.strip()
        }
        try:
            # 初次API调用
            response = await safe_api_call_async(
                client=client,
                messages=[
                    {"role": "system", "content": trans_json_user_prompt},
                    {"role": "user", "content": chunk_string}
                ],
                model=model
            )

            translated_string = response.choices[0].message.content
            
            try:
                trans_to_json = json.loads(translated_string)
            except json.JSONDecodeError as e:
                # JSON解析失败的调试记录
                output_data = {
                    'raw_response': translated_string[:500] + ("..." if len(translated_string) > 500 else ""),
                    'actual_lines': 'JSON_ERROR',
                    'formatted_output': f'JSON解析失败: {str(e)}'
                }
                result_info = {
                    'success': False,
                    'line_count_match': False,
                    'error_message': f'JSON解析失败: {str(e)}'
                }
                add_debug_record(video_id, chunk_info, input_data, output_data, result_info, "initial")
                raise

            # 行数检查
            check_translated = len(trans_to_json)
            
            # 格式化输出用于调试记录
            formatted_output = '\n'.join(f"{k}: {v}" for k, v in trans_to_json.items())
            
            output_data = {
                'raw_response': translated_string[:500] + ("..." if len(translated_string) > 500 else ""),
                'actual_lines': check_translated,
                'formatted_output': formatted_output
            }
            
            if check_chunk_string == check_translated:
                logger.info(f'编号{first_item_number}一次性通过')
                # 成功的调试记录
                result_info = {
                    'success': True,
                    'line_count_match': True,
                    'error_message': '一次通过'
                }
                add_debug_record(video_id, chunk_info, input_data, output_data, result_info, "initial")
                
                # 正常处理流程
                new_num_dict = process_transdict_num(trans_to_json, first_item_number, end_item_number)
                translated_dict = process_translated_string(new_num_dict)
                result['translations'].update(translated_dict)
            else:
                # 进入重试逻辑，先记录初次失败
                result_info = {
                    'success': False,
                    'line_count_match': False,
                    'error_message': f'行数不匹配，进入重试逻辑'
                }
                add_debug_record(video_id, chunk_info, input_data, output_data, result_info, "initial")
                
                logger.info(f'编号{first_item_number}进入重试逻辑!!!')
                retry_prompt_v2 = f'''
                The previous translation had a mismatch (English: {check_chunk_string} lines, Chinese: {check_translated} lines).

                Please carefully translate each line individually, maintaining a strict one-to-one match between English and Chinese lines (lines {first_item_number}-{end_item_number}, total {check_chunk_string} lines).

                Return your translation in this JSON format:

                {{
                "1": "<Translated line 1>",
                "2": "<Translated line 2>",
                ...
                }}
                '''

                try:
                    # 重试API调用
                    @retry(stop=stop_after_attempt(2))
                    async def retry_call():
                        return await safe_api_call_async(
                            client=client,
                            messages=[
                                {"role": "system", "content": trans_json_user_prompt},
                                {"role": "assistant", "content": translated_string},
                                {"role": "user", "content": retry_prompt_v2}
                            ],
                            model=model
                        )
                        
                    retry_response = await retry_call()
                    
                    retry_translated_string = retry_response.choices[0].message.content

                    # 强制重复验证
                    try:
                        retrytrans_to_json = json.loads(retry_translated_string)
                    except json.JSONDecodeError as e:
                        # 重试JSON解析失败的调试记录
                        retry_output_data = {
                            'raw_response': retry_translated_string[:500] + ("..." if len(retry_translated_string) > 500 else ""),
                            'actual_lines': 'JSON_ERROR',
                            'formatted_output': f'重试JSON解析失败: {str(e)}'
                        }
                        retry_result_info = {
                            'success': False,
                            'line_count_match': False,
                            'error_message': f'重试JSON解析失败: {str(e)}'
                        }
                        retry_input_data = {
                            'system_prompt': trans_json_user_prompt,
                            'user_content': retry_prompt_v2.strip()
                        }
                        add_debug_record(video_id, chunk_info, retry_input_data, retry_output_data, retry_result_info, "retry")
                        logger.error(f"重试响应JSON解析失败: {retry_translated_string}")
                        raise

                    # 重复行数检查
                    check_retry = len(retrytrans_to_json)
                    
                    # 准备重试的调试记录
                    retry_formatted_output = '\n'.join(f"{k}: {v}" for k, v in retrytrans_to_json.items())
                    retry_output_data = {
                        'raw_response': retry_translated_string[:500] + ("..." if len(retry_translated_string) > 500 else ""),
                        'actual_lines': check_retry,
                        'formatted_output': retry_formatted_output
                    }
                    retry_input_data = {
                        'system_prompt': trans_json_user_prompt,
                        'user_content': retry_prompt_v2.strip()
                    }
                    
                    if check_retry == check_chunk_string:
                        # 处理成功重试
                        logger.info(f"编号{first_item_number}重试有效！")
                        
                        # 重试成功的调试记录
                        retry_result_info = {
                            'success': True,
                            'line_count_match': True,
                            'error_message': '重试成功'
                        }
                        add_debug_record(video_id, chunk_info, retry_input_data, retry_output_data, retry_result_info, "retry")
                        
                        # 对翻译后的字符串进行处理
                        new_num_dict = process_transdict_num(retrytrans_to_json, first_item_number, end_item_number)
                        translated_dict = process_translated_string(new_num_dict)
                        
                        result['translations'].update(translated_dict)
                    else:
                        # 重试仍然失败的调试记录
                        retry_result_info = {
                            'success': False,
                            'line_count_match': False,
                            'error_message': f'重试后行数仍不匹配 ({check_retry} vs {check_chunk_string})'
                        }
                        add_debug_record(video_id, chunk_info, retry_input_data, retry_output_data, retry_result_info, "retry")
                        raise ValueError(f"编号{first_item_number}重试后行数仍不匹配 ({check_retry} vs {check_chunk_string})")

                except Exception as retry_error:
                    logger.error(f"重试失败: {str(retry_error)}")
                    result['translations'][first_item_number] = f"翻译失败: {str(retry_error)}"

        except Exception as main_error:
            logger.error(f"主流程错误: {str(main_error)}")
            result['translations'][first_item_number] = f"关键错误: {str(main_error)}"

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


# 给中文翻译添加时间轴，生成未经句子长度优化的初始中文字幕
def map_chinese_to_time_ranges(chinese_content, merged_engsentence_to_subtitles):
    chinese_to_time = {}
    chinese_subtitles = []

    # 与句子合并后的英文字幕做匹配
    for num, chinese_sentence in chinese_content.items():
        if num in merged_engsentence_to_subtitles:
            time_ranges, _ = merged_engsentence_to_subtitles[num]
            chinese_to_time[num] = time_ranges, chinese_sentence

            chinese_subtitles.append([
                num,
                time_ranges,
                chinese_sentence
            ])

    return chinese_to_time

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

    return chinese_to_time


def parse_time(time_str):
    """解析时间字符串为datetime对象"""
    return datetime.strptime(time_str, '%H:%M:%S,%f')


def time_to_str(dt):
    """
    将 datetime 对象格式化为 SRT 字幕时间格式：HH:MM:SS,mmm
    """
    return dt.strftime("%H:%M:%S,%f")[:-3]


async def translate_with_deepseek_async(numbered_sentences_chunks, custom_prompt, special_terms="", content_name="", model='deepseek-chat', video_id="unknown"):
    """
    使用DeepSeek异步并行翻译英文字幕到中文
    """
    items = list(numbered_sentences_chunks.items())
    total_translated_dict = {}

    # 处理特殊术语
    if special_terms:
        special_terms = special_terms.rstrip(".")
        special_terms_list = special_terms.split(", ")

    # 创建信号量
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    
    # 创建异步OpenAI客户端，使用API_TIMEOUT配置超时
    client = AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY, 
        base_url="https://api.deepseek.com",  # 保持原始URL
        timeout=API_TIMEOUT  # 直接使用API_TIMEOUT配置超时
    )

    # 创建批次处理任务
    tasks = []
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i + BATCH_SIZE]
        tasks.append(
            process_chunk(chunk, custom_prompt, model, client, semaphore, "system_prompt_placeholder", video_id)
        )
    
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

    return total_translated_dict

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


#async def split_long_chinese_sentence_v3(chinese_timeranges_dict, model='deepseek-chat'):
#     '''
#     v3版本，先把中文句子按照空格进行分割，然后再对超过40个字的长句子进行分割
#     '''
#     # 先按照空格分割
#     space_split_subtitles = {}
#     space_split_index = 1

#     for index, (time_range, text) in chinese_timeranges_dict.items():
#         start_time, end_time = time_range.split(' --> ')
#         # 使用正则表达式分割中文句子之间的空格
#         parts = re.split(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', text)
#         num_parts = len(parts)
#         total_length = sum(len(part) for part in parts)

#         if num_parts > 1:
#             start_time = parse_time(start_time)
#             end_time = parse_time(end_time)
#             word_per_duration = (end_time - start_time)/total_length
#             current_start_time = start_time

#             for i, part in enumerate(parts):
#                 current_end_time = current_start_time + word_per_duration * len(part)
#                 space_split_subtitles[space_split_index] = (f"{time_to_str(current_start_time)} --> {time_to_str(current_end_time)}", part)
#                 current_start_time = current_end_time
#                 space_split_index += 1
#         else:
#             space_split_subtitles[space_split_index] = (time_range, text)
#             space_split_index += 1

#     # 复制字典
#     split_subtitles_dict = space_split_subtitles.copy()

#     # 找出中文字幕中的长字幕
#     threshold = 40
#     long_subtitles = []
#     for key, (timeranges, subtitles) in space_split_subtitles.items():
#         if len(subtitles) > threshold:
#             long_subtitles.append((key, timeranges, subtitles))

#     # 对长字幕开始进行优化
#     # 循环控制
#     for key, timeranges, subtitles in long_subtitles:
#         # 在循环中提取时间范围
#         start_str, end_str = timeranges.split(' --> ')
#         start_time = parse_time(start_str)
#         end_time = parse_time(end_str)

#         # 在循环中调用api分割句子
#         # api调用，返回api_return_content
#         split_prompt_v2 = f'''
#         Split the long Chinese sentences below, delimited by triple backtick
#         - Only split long sentences, do not alter the content of the sentences
#         - According to your understanding of the sentence, divide the long sentence into several short sentences that are easiest to understand.
#         - When splitting, please keep the "linguistic integrity" together.
#         - Each short sentence should not exceed 20 Chinese characters as much as possible.

#         Provide your translation in json structure like this:{{
#               '1':'<Segmented short sentences 1>',
#               '2':'<Segmented short sentences 2>',
#               }}
#         long Chinese sentences below: ```{subtitles}```
#         '''
        
#         logger.info(f"对长句子进行分割: {subtitles}")
        
#         client = AsyncOpenAI(
#             api_key=DEEPSEEK_API_KEY, 
#             base_url="https://api.deepseek.com",
#             timeout=float(API_TIMEOUT)
#         )
        
#         messages = [
#             {"role": "user", "content": split_prompt_v2},
#         ]
        
#         # 调用API分割长句子
#         try:
#             response = await client.chat.completions.create(
#                 model=model,
#                 response_format={'type': "json_object"},
#                 messages=messages,
#                 temperature=0,
#                 top_p=1,
#                 frequency_penalty=0,
#                 presence_penalty=0,
#             )
#             api_return_content = response.choices[0].message.content
            
#             # 处理api返回的json结果，转为字典
#             api_return_content_todict = json.loads(api_return_content)
#             all_text = ''.join(api_return_content_todict.values())
            
#             # 在循环中完成短句的时间轴计算和匹配
#             # 新建一个列表用来存储分割后的字幕信息
#             split_subtitles = []
#             # 计算总时长
#             duration = (end_time - start_time).total_seconds()
#             # 用返回的句子来计算每个字符的持续时间
#             word_duration = duration / len(all_text)
#             # 起始时间
#             current_start_time = start_time
#             # 为分割后的每个字幕生成时间轴
#             short_subtitle_list = list(api_return_content_todict.values())
#             for short_subtitle in short_subtitle_list:
#                 # 计算时间轴信息
#                 short_subtitle_duration = len(short_subtitle) * word_duration
#                 current_end_time = current_start_time + timedelta(seconds=short_subtitle_duration)
#                 # 存储分割后字幕的时间轴
#                 short_subtitle_time_range = f'{time_to_str(current_start_time)} --> {time_to_str(current_end_time)}'
#                 split_subtitles.append((short_subtitle_time_range, short_subtitle))
#                 # 更新初始时间
#                 current_start_time = current_end_time
            
#             # 在循环中更新split_subtitles_dict字典
#             split_subtitles_dict[key] = split_subtitles
            
#         except Exception as e:
#             logger.error(f"长句分割失败: {str(e)}", exc_info=True)
#             # 如果分割失败，保留原始句子
#             split_subtitles_dict[key] = [(timeranges, subtitles)]

#     logger.info(f'使用的模型：{model}')
    
#     # 处理最终的字典结构，使其符合预期的格式
#     final_dict = {}
#     current_index = 1
    
#     for key, value in split_subtitles_dict.items():
#         if isinstance(value, list):  # 处理被分割的字幕
#             for time_range, text in value:
#                 final_dict[current_index] = (time_range, text)
#                 current_index += 1
#         else:  # 处理未被分割的字幕
#             time_range, text = value
#             final_dict[current_index] = (time_range, text)
#             current_index += 1
    
#     logger.info(f"长句子拆分完成：原始{len(chinese_timeranges_dict)}个条目，拆分后{len(final_dict)}个条目")
#     return final_dict 

# 250403更新
# 全新的长句分割方法。对于无法按照规则分割的句子，调用异步LLM分割
def time_to_str(dt):
    """
    将 datetime 对象格式化为 SRT 字幕时间格式：HH:MM:SS,mmm
    """
    return dt.strftime("%H:%M:%S,%f")[:-3]

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

async def llm_batches_split(long_sentences, model='gpt-4.1-mini'):
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

    # 创建异步OpenAI客户端
    client = AsyncOpenAI(
        api_key=OPENAI_API_KEY,
        #base_url="https://api.deepseek.com"
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
async def translate_subtitles(numbered_sentences_chunks, custom_prompt, model_choice="gpt", special_terms="", content_name="", video_id="unknown"):
    """
    统一的字幕翻译函数，支持不同模型选择
    
    Args:
        numbered_sentences_chunks: 编号的句子块
        custom_prompt: 自定义提示词
        model_choice: 模型选择，可选值为 "deepseek" 或 "gpt"
        special_terms: 特殊术语列表
        content_name: 内容名称
    
    Returns:
        翻译后的字典
    """
    if model_choice.lower() == "deepseek":
        model = 'deepseek-chat'
        return await translate_with_model(
            numbered_sentences_chunks, 
            custom_prompt, 
            model=model,
            api_key=DEEPSEEK_API_KEY,
            special_terms=special_terms, 
            content_name=content_name,
            video_id=video_id
        )
    elif model_choice.lower() == "gpt":
        model = 'gpt-4.1-mini'
        return await translate_with_model(
            numbered_sentences_chunks, 
            custom_prompt, 
            model=model,
            api_key=OPENAI_API_KEY,
            special_terms=special_terms, 
            content_name=content_name,
            video_id=video_id
        )
    else:
        raise ValueError(f"不支持的模型选择: {model_choice}，请选择 'deepseek' 或 'gpt'")

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
    
    # 基于模型类型创建适当的客户端
    if "deepseek" in model.lower():
        client = AsyncOpenAI(
            api_key=api_key, 
            base_url="https://api.deepseek.com",  # DeepSeek的API地址
            timeout=API_TIMEOUT
        )
        logger.info(f"使用DeepSeek API客户端，模型: {model}")
    elif "gpt" in model.lower():
        client = AsyncOpenAI(
            api_key=api_key,
            timeout=API_TIMEOUT
        )
        logger.info(f"使用OpenAI API客户端，模型: {model}")
    else:
        # 处理未知模型类型
        raise ValueError(f"不支持的模型类型: {model}")

    # 获取适合当前模型的提示词
    system_prompt = get_system_prompt_for_model(model)

    # 创建批次处理任务
    tasks = []
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i + BATCH_SIZE]
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

    return total_translated_dict

def get_system_prompt_for_model(model):
    """
    根据不同模型返回适合的系统提示词模板
    """
    if "deepseek" in model.lower():
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
    else:
        # GPT-4.1-mini 的简化提示词
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

        ### Skill 2: Contextual Translation
        - Consider the context of the video to ensure accuracy and coherence in the translation.
        - When slang or implicit information appears in the original text, do not translate it literally. Instead, adapt it to align with natural Chinese communication habits.

        ### Skill 3: Handling Complex Sentences
        - Rearrange word order and adjust wording for complex sentence structures to ensure translations are easily understandable and fluent in Chinese.

        ### Skill 4: Proper Nouns and Special Terms
        - Identify proper nouns and special terms enclosed in angle brackets
         < > within the subtitle text, and retain them in their original English form.

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

# 0505更新 将title和channel name传给LLM，推断视频上下文信息。
@async_retry()
async def get_video_context_from_llm(title, channel_name):
    """
    让LLM通过title 和 channel name 推断视频上下文信息
    """
    try:
        logger.info("开始执行get_video_context_from_llm...")

        client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            timeout=API_TIMEOUT
        )

        messages=[
                    {"role": "system", "content": "步骤1：判断该channel是否是在你的知识库中。如果你了解该channel的相关信息，输出它的相关信息\n例如：channel name ： 3Blue1Brown\n这个频道以动画可视化数学原理闻名\n要求：仔细检查你的知识库，如果你不知道这个channel则诚实的说不知道，不要编造信息。\n\n步骤2：结合video title和步骤1的信息，输出你对视频内容的推断。然后简要描述针对这个视频，该采取什么样的翻译策略。\n要求：如果无法从title和channel name中推断视频内容，请诚实的说不知道，不要编造信息。\n\n步骤3：综合步骤1、2，以第一人称的口吻给出简要的3个翻译策略或注意事项。\n\t1.\t明确本次翻译应采用的话语风格（如：正式、学术、轻松、幽默等），风格应贴合视频内容和目标观众；\n\t2.\t识别该视频中可能包含的专业领域术语，简要列举 2-3 个代表性术语，并指出它们在翻译中应保持准确性或采用贴近母语习惯的表达；\n\t3.\t可补充其他翻译技巧，但不得包含模板化建议，如“术语首次出现时进行注释或举例说明”这类通用表述应避免使用。\n要求：如果无法从步骤1、2推断视频内容，请诚实的说不知道，不要编造信息。\n\n使用中文输出所有内容\n使用如下json格式进行输出\n{\n\"step1\": {\n\"channel_name\": \"string\",\n\"channel_info\": \"string or null\",\n\"can_judge\": true\n},\n\"step2\": {\n\"video_title\": \"string\",\n\"content_inference\": \"string or null\",\n\"can_judge\": true\n},\n\"step3\": {\n\"translation_strategies\": [\n\"string or null\",\n\"string or null\",\n\"special_terms_strategies\"\n],\n\"can_judge\": true\n}"},
                    {"role": "user", "content": f"channel name: {channel_name}\nvideo title: {title}"}
                ]

        response = await client.chat.completions.create(
            model="gpt-4.1",
            response_format={'type': "json_object"},
            messages=messages,
            temperature=1,
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