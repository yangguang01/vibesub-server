import asyncio
import json
import re
import traceback

from openai import AsyncOpenAI
import openai

from app.common.core.config import (
    API_TIMEOUT,
    DEEPSEEK_API_KEY,
    MAX_CONCURRENT_TASKS,
    RETRY_ATTEMPTS,
    TRANSLATE_BATCH_SIZE,
)
from app.common.core.logging import logger
from app.common.services.debug_recorder import (
    add_debug_record,
    clear_debug_records,
    get_debug_records_text,
    save_debug_records_to_storage,
)
from app.common.services.sentence_splitter import split_long_chinese_sentence_v4
from app.common.services.subtitle_timing import (
    format_subtitles_v2,
    format_time,
    map_chinese_to_time_ranges_v2,
    map_marged_sentence_to_timeranges,
    parse_time_range,
    subtitles_to_dict,
    time_to_str,
)
from app.common.services.transcription import (
    convert_AssemblyAI_to_srt,
    extract_asr_sentences,
    format_time_AssemblyAI,
    json_to_srt,
    transcribe_audio_with_assemblyai,
)
from app.common.services.video_download import (
    download_audio_webm,
    get_video_info,
    get_video_info_and_download,
    get_video_info_and_download_async,
)
from app.common.utils.retry import async_retry, async_retry_always

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
@async_retry_always()
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