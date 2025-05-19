import os
import json
import uuid
import asyncio
from pathlib import Path
from datetime import datetime

from app.common.core.logging import logger
from app.common.utils.file_utils import (
    #get_file_paths, 
    cleanup_task_files, 
    cleanup_audio_file, 
    get_file_url
)
from app.common.services.translation import (
    get_video_info,
    transcribe_audio,
    json_to_srt,
    extract_asr_sentences,
    translate_with_deepseek_async,
    subtitles_to_dict,
    map_marged_sentence_to_timeranges,
    map_chinese_to_time_ranges_v2,
    format_subtitles_v2,
    split_long_chinese_sentence_v4,
    generate_custom_prompt,
    get_video_info_and_download,
    translate_subtitles,
    transcribe_audio_with_assemblyai,
    convert_AssemblyAI_to_srt,
    get_video_context_from_llm,
    process_video_context_data,
    get_video_info_and_download_async
)
from app.common.core.config import SUBTITLES_DIR, TRANSCRIPTS_DIR, TMP_DIR, AUDIO_DIR
from app.common.models.firestore_models import save_task, update_task_status, get_task
from app.common.services.storage import bucket


# 全局存储任务状态 (临时存储，后续迁移到Firestore)
tasks_store = {}


async def process_translation_task(task_id, paths, youtube_url, custom_prompt="", special_terms="", content_name="", channel_name="", language="zh-CN", model=None, user_id="anonymous"):
    """
    处理翻译任务的主函数
    
    参数:
        task_id (str): 任务ID
        paths (dict): 任务相关文件路径
        youtube_url (str): YouTube视频URL
        custom_prompt (str, optional): 定制提示
        special_terms (str, optional): 特殊术语，逗号分隔
        content_name (str, optional): 内容名称
        language (str, optional): 目标语言
        model (str, optional): 选择的模型
        user_id (str, optional): 用户ID
    """
    audio_file = None
    try:
        # 更新任务状态为处理中
        tasks_store[task_id]["status"] = "processing"
        update_task_status(task_id, "processing")

        # 1.开始下载音频并获取视频上下文信息
        llm_context_task = asyncio.create_task(get_video_context_from_llm(content_name, channel_name))
        download_task = asyncio.create_task(get_video_info_and_download_async(youtube_url, paths["audio"]))

        # 2.等待视频上下文信息生成
        tasks_store[task_id]["progress"] = 0.1
        update_task_status(task_id, "processing", 0.1)
        video_context_data = await llm_context_task
        video_context_prompt, trans_strategies = process_video_context_data(video_context_data)
        tasks_store[task_id]["trans_strategies"] = trans_strategies
        tasks_store[task_id]["status"] = "strategies_ready"
        
        # 保存翻译策略到Firestore
        update_task_status(task_id, "strategies_ready", 0.15)
        save_task(task_id, {"trans_strategies": trans_strategies})

        # 3. 等待下载完成
        tasks_store[task_id]["progress"] = 0.2
        update_task_status(task_id, "processing", 0.2)
        logger.info("await download_task 开始！")
        video_info = await download_task
        logger.info("await download_task 结束！")
        
        video_id = video_info.get('id', task_id)
        video_channel = video_info.get('channel', task_id)
        video_title = content_name or video_info.get('title', task_id)
        
        tasks_store[task_id]["video_title"] = video_title
        tasks_store[task_id]["video_id"] = video_id
        
        # 保存视频信息到Firestore
        save_task(task_id, {
            "video_title": video_title,
            "video_id": video_id,
            "video_channel": video_channel
        })
        
        audio_file = paths["audio"]

        # 4. 转写音频
        tasks_store[task_id]["progress"] = 0.4
        update_task_status(task_id, "processing", 0.4, None, None)

        # 使用AssemblyAI转写音频
        asr_result = await transcribe_audio_with_assemblyai(audio_file)
        
        # 5. 生成SRT格式字幕
        srt_text = convert_AssemblyAI_to_srt(asr_result)
        
        # 保存英文SRT字幕
        with open(paths["transcript_srt"], "w", encoding="utf-8") as f:
            f.write(srt_text)
            
        # 上传英文字幕到Cloud Storage
        english_srt_blob = bucket.blob(f"transcripts/{task_id}_en.srt")
        english_srt_blob.upload_from_filename(paths["transcript_srt"])
        english_srt_url = english_srt_blob.public_url
        
        # 保存英文字幕URL到Firestore
        save_task(task_id, {"english_srt_url": english_srt_url})
        
        # 6. 将短句子合并为长句子
        tasks_store[task_id]["progress"] = 0.5
        update_task_status(task_id, "processing", 0.5, None, None)
        numbered_sentences_chunks = extract_asr_sentences(srt_text)
        
        # 7. 使用LLM异步并行翻译英文字幕到中文
        tasks_store[task_id]["progress"] = 0.6
        update_task_status(task_id, "processing", 0.6, None, None)
        llm_trans_result = await translate_subtitles(
            numbered_sentences_chunks,
            video_context_prompt,
            model,
            special_terms,
            content_name
        )
        
        # 8. 生成原始字幕的字典数据，给长句子匹配时间轴信息
        tasks_store[task_id]["progress"] = 0.7
        update_task_status(task_id, "processing", 0.7, None, None)
        subtitles_dict = subtitles_to_dict(srt_text)
        marged_timeranges_dict = map_marged_sentence_to_timeranges(numbered_sentences_chunks, subtitles_dict)
        
        # 9. 给中文翻译匹配时间轴信息
        tasks_store[task_id]["progress"] = 0.8
        update_task_status(task_id, "processing", 0.8, None, None)
        chinese_timeranges_dict = map_chinese_to_time_ranges_v2(llm_trans_result, marged_timeranges_dict)
        
        # 10. 使用中文长句子分割为适合字幕显示的短句
        logger.info("开始处理中文长句子拆分...")
        short_chinese_subtitles_dict = await split_long_chinese_sentence_v4(chinese_timeranges_dict)
        
        # 11. 将字典转为SRT字符串
        tasks_store[task_id]["progress"] = 0.9
        update_task_status(task_id, "processing", 0.9, None, None)
        cn_srt_content = format_subtitles_v2(short_chinese_subtitles_dict)
        
        # 12. 保存中文字幕文件
        with open(paths["subtitle"], "w", encoding="utf-8") as f:
            f.write(cn_srt_content)
            
        # 上传中文字幕到Cloud Storage
        chinese_srt_blob = bucket.blob(f"subtitles/{task_id}_zh.srt")
        chinese_srt_blob.upload_from_filename(paths["subtitle"])
        chinese_srt_url = chinese_srt_blob.public_url
        
        # 13. 更新任务状态和结果URL
        result_url = chinese_srt_url
        tasks_store[task_id].update({
            "status": "completed",
            "progress": 1.0,
            "result_url": result_url
        })
        
        # 更新Firestore中的任务状态
        update_task_status(
            task_id=task_id,
            status="completed",
            progress=1.0,
            result_url=result_url
        )
        
        logger.info(f"任务 {task_id} 处理完成: {result_url}")
        
    except Exception as e:
        logger.error(f"任务 {task_id} 处理失败: {str(e)}", exc_info=True)
        tasks_store[task_id].update({
            "status": "failed",
            "error": str(e)
        })
        
        # 更新Firestore中的任务状态
        update_task_status(
            task_id=task_id,
            status="failed",
            error=str(e)
        )
    finally:
        # 无论成功或失败，都清理音频文件和临时文件
        if audio_file and Path(audio_file).exists():
            cleanup_audio_file(Path(audio_file))
        cleanup_task_files(task_id)


def create_translation_task(youtube_url, custom_prompt="", special_terms="", content_name="", channel_name="", language="zh-CN", model=None, user_id="anonymous"):
    """
    创建新的翻译任务
    
    参数:
        youtube_url (str): YouTube视频URL
        custom_prompt (str, optional): 定制提示
        special_terms (str, optional): 特殊术语，逗号分隔
        content_name (str, optional): 内容名称
        language (str, optional): 目标语言
        model (str, optional): 选择的模型
        user_id (str, optional): 用户ID
        
    返回:
        str: 任务ID
    """
    # 生成唯一任务ID
    task_id = str(uuid.uuid4())
    
    # 初始化任务状态
    task_data = {
        "status": "pending",
        "progress": 0,
        "youtube_url": youtube_url,
        "custom_prompt": custom_prompt,
        "special_terms": special_terms,
        "content_name": content_name,
        "channel_name": channel_name,
        "language": language,
        "model": model,
        "user_id": user_id,
        "created_at": datetime.now()
    }
    
    # 保存到内存存储
    tasks_store[task_id] = task_data
    
    # 保存到Firestore
    save_task(task_id, task_data)

    # 生成各类文件路径
    paths = {
        "task_dir": TMP_DIR / task_id,
        "audio": AUDIO_DIR / f"{task_id}.webm",
        "transcript": TRANSCRIPTS_DIR / f"{task_id}.json",
        "transcript_srt": TRANSCRIPTS_DIR / f"{task_id}.srt",
        "subtitle": SUBTITLES_DIR / f"{task_id}.srt",
    }

    # 确保任务目录存在
    paths["task_dir"].mkdir(exist_ok=True)

    
    # 创建异步任务
    asyncio.create_task(
        process_translation_task(
            task_id=task_id,
            paths=paths,
            youtube_url=youtube_url,
            custom_prompt=custom_prompt,
            special_terms=special_terms,
            content_name=content_name,
            channel_name=channel_name,
            language=language,
            model=model,
            user_id=user_id
        )
    )
    
    return task_id


def get_task_status(task_id):
    """
    获取任务状态，首先从内存获取，如内存中不存在则从Firestore获取
    
    参数:
        task_id (str): 任务ID
        
    返回:
        dict: 任务状态信息
    """
    # 先从内存中查找
    if task_id in tasks_store:
        return tasks_store[task_id]
    
    # 如果内存中不存在，从Firestore获取
    return get_task(task_id)


def get_task_translation_strategies(task_id):
    """
    获取任务的翻译策略
    
    参数:
        task_id (str): 任务ID
        
    返回:
        dict: 任务的翻译策略，如果任务不存在或尚未生成策略则返回None
    """
    # 先从内存中查找
    if task_id in tasks_store and "trans_strategies" in tasks_store[task_id]:
        return {"strategies": tasks_store[task_id]["trans_strategies"]}
    
    # 如果内存中不存在，从Firestore获取
    task_data = get_task(task_id)
    if task_data and "trans_strategies" in task_data:
        return {"strategies": task_data["trans_strategies"]}
    
    return None 