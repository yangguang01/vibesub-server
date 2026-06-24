import asyncio
import datetime
import json
import re

from openai import AsyncOpenAI
import openai

from app.common.core.config import BATCH_SIZE, DEEPSEEK_API_KEY, MAX_CONCURRENT_TASKS
from app.common.core.logging import logger
from app.common.services.subtitle_timing import parse_time_range, time_to_str
from app.common.utils.retry import async_retry_always


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


@async_retry_always()
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
