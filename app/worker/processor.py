import os
import json
import asyncio
from firebase_admin import storage
from app.common.utils.executor import executor
from app.common.utils.firebase_storage_init import *
from app.common.core.logging import logger
from app.common.services.translation import (
    json_to_srt,
    extract_asr_sentences,
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
    save_debug_records_to_storage,
    clear_debug_records,
)
from app.common.models.firestore_models import get_task, create_or_update_video_task, update_video_task, record_successful_request
from app.common.services.storage import bucket
from app.common.services.download_ytsub import download_auto_subtitle
from app.common.services.process_ytsub import process_ytsub


# 全局存储任务状态 (临时存储，后续迁移到Firestore)
tasks_store = {}


async def process_translation_task(video_id, youtube_url, user_id, content_name, special_terms="", language="zh-CN", model=None):
    """
    处理翻译任务的主函数
    
    参数:
        video_id (str): 任务ID
        youtube_url (str): YouTube视频URL
        custom_prompt (str, optional): 定制提示
        special_terms (str, optional): 特殊术语，逗号分隔
        content_name (str, optional): 视频标题
        language (str, optional): 目标语言
        model (str, optional): 选择的模型
        user_id (str, optional): 用户ID
    """

    loop = asyncio.get_event_loop()

    try:
        # 获取配置的默认 bucket
        bucket = storage.bucket()  # 获取上面配置的默认 bucket
        # 初始化翻译策略
        trans_strategies = []
        
        # 清空之前的调试记录，开始记录当前任务
        clear_debug_records()
        logger.info(f"开始处理视频任务 {video_id}，调试记录已初始化")

        # 优先使用yt 自动生成的英文字幕
        try:
            yt_sub_path, video_title, channel_name = download_auto_subtitle(youtube_url)
            if yt_sub_path:
                try:
                    srt_text = process_ytsub(yt_sub_path)
                    # 进度0.3：生成翻译策略
                    logger.info("开始生成翻译策略...")
                    llm_context_task = asyncio.create_task(get_video_context_from_llm(video_title, channel_name))
                    video_context_data = await llm_context_task
                    video_context_prompt, trans_strategies = process_video_context_data(video_context_data)
                    await loop.run_in_executor(executor, update_video_task, video_id, "strategies_ready", 0.3, trans_strategies)
                except Exception as e:
                    logger.error(f"处理YouTube字幕失败: {e}")
                    # 回退到音频下载流程
                    yt_sub_path = None
            
            if not yt_sub_path:
                # 执行音频下载流程
                logger.info("开始下载音频...")
                try:
                    video_info, filename = await loop.run_in_executor(executor, get_video_info_and_download, youtube_url)
                    await loop.run_in_executor(executor, update_video_task, video_id, "processing", 0.2)
                except Exception as e:
                    logger.error(f"音频下载失败: {e}")
                    await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0.1, [], f"音频下载失败: {str(e)}")
                    raise

                # 进度0.4：生成翻译策略
                logger.info("开始生成翻译策略...")
                try:
                    video_title = video_info.get('title', '')
                    channel_name = video_info.get('channel', '')
                    llm_context_task = asyncio.create_task(get_video_context_from_llm(video_title, channel_name))
                    video_context_data = await llm_context_task
                    video_context_prompt, trans_strategies = process_video_context_data(video_context_data)
                    await loop.run_in_executor(executor, update_video_task, video_id, "strategies_ready", 0.3, trans_strategies)
                except Exception as e:
                    logger.error(f"生成翻译策略失败: {e}")
                    await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0.2, [], f"生成翻译策略失败: {str(e)}")
                    raise

                # 进度0.6：进行ASR处理
                logger.info("开始ASR处理...")
                try:
                    asr_result = await loop.run_in_executor(executor, transcribe_audio_with_assemblyai, filename)
                    srt_text = convert_AssemblyAI_to_srt(asr_result)
                    # 异步上传英文字幕到Firebase Storage
                    def upload_srt_to_storage():
                        english_srt_blob = bucket.blob(f"asr_srt/{video_id}.srt")
                        english_srt_blob.upload_from_string(
                            srt_text,
                            content_type="text/plain"
                        )
                        return english_srt_blob.public_url  # 返回上传后的URL
                    # 放入线程池执行
                    upload_url = await loop.run_in_executor(executor, upload_srt_to_storage)
                    await loop.run_in_executor(executor, update_video_task, video_id, "strategies_ready", 0.5)
                except Exception as e:
                    logger.error(f"ASR处理失败: {e}")
                    await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0.3, trans_strategies, f"ASR处理失败: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"字幕获取阶段失败: {e}")
            await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0.1, [], f"字幕获取失败: {str(e)}")
            raise

        # 进度0.8：开始翻译
        try:
            #将短句子合并为长句子
            numbered_sentences_chunks = extract_asr_sentences(srt_text)
            logger.info("开始翻译字幕...")
            #翻译字幕
            llm_trans_result = await translate_subtitles(
                numbered_sentences_chunks,
                video_context_prompt,
                model,
                special_terms,
                content_name,
                video_id
            )
            await loop.run_in_executor(executor, update_video_task, video_id, "strategies_ready", 0.85)
        except Exception as e:
            logger.error(f"翻译处理失败: {e}")
            await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0.6, trans_strategies, f"翻译处理失败: {str(e)}")
            raise

        # 进度0.9：优化字幕长度
        try:
            # 生成原始字幕的字典数据，给长句子匹配时间轴信息
            subtitles_dict = subtitles_to_dict(srt_text)
            marged_timeranges_dict = map_marged_sentence_to_timeranges(numbered_sentences_chunks, subtitles_dict)
            # 给中文翻译匹配时间轴信息
            chinese_timeranges_dict = map_chinese_to_time_ranges_v2(llm_trans_result, marged_timeranges_dict)
            # 使用中文长句子分割为适合字幕显示的短句
            logger.info("开始处理中文长句子拆分...")
            short_chinese_subtitles_dict = await split_long_chinese_sentence_v4(chinese_timeranges_dict)
            # 将字典转为SRT字符串
            cn_srt_content = format_subtitles_v2(short_chinese_subtitles_dict)
            # 异步保存中文字幕到Firebase Storage
            def upload_chinese_srt_to_storage():
                chinese_srt_blob = bucket.blob(f"cn_srt/{video_id}.srt")
                chinese_srt_blob.upload_from_string(
                    cn_srt_content,
                    content_type="text/plain"
                )
                return chinese_srt_blob.public_url  # 可选：返回上传后的URL
            # 放入线程池执行
            chinese_srt_url = await loop.run_in_executor(executor, upload_chinese_srt_to_storage)
        except Exception as e:
            logger.error(f"字幕后处理失败: {e}")
            await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0.85, trans_strategies, f"字幕后处理失败: {str(e)}")
            raise
        
        # 保存调试记录到 Firebase Storage
        debug_url = await save_debug_records_to_storage(video_id, bucket)
        if debug_url:
            logger.info(f"调试记录已保存: {debug_url}")
        
        logger.info("任务完成,更新任务状态")
        await loop.run_in_executor(executor, update_video_task, video_id, "completed", 1)
        logger.info("写入用户请求记录")
        await loop.run_in_executor(executor, record_successful_request, user_id, video_id, video_title)

    except Exception as e:
            logger.error(f"任务 {video_id} 处理失败: {str(e)}", exc_info=True)
            
            # 即使任务失败，也保存调试记录以便排查问题
            try:
                bucket = storage.bucket()
                debug_url = await save_debug_records_to_storage(video_id, bucket)
                if debug_url:
                    logger.info(f"任务失败，调试记录已保存: {debug_url}")
            except Exception as debug_error:
                logger.error(f"保存失败任务的调试记录时出错: {str(debug_error)}")
                
            await loop.run_in_executor(executor, update_video_task, video_id, "failed", 0, trans_strategies, str(e))
        

async def create_translation_task(
    youtube_url, 
    user_id,
    video_id,
    content_name,
    special_terms="", 
    language="zh-CN", 
    model="gpt", 
):
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
        video_id (str, optional): 视频ID，如不提供则生成新的UUID
        
    返回:
        str: 任务ID
    """
    
    # 初始化任务状态
    # 进度0.1：写入视频任务信息到Firestore
    create_or_update_video_task(
        video_id=video_id,
        video_title=content_name,
        youtube_url=youtube_url,
        user_id=user_id,
    )

    
    # 创建异步任务
    await process_translation_task(
            video_id=video_id,
            youtube_url=youtube_url,
            special_terms=special_terms,
            content_name=content_name,
            language=language,
            model=model,
            user_id=user_id
        )
    
    return video_id


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

if __name__ == "__main__":
    # 测试代码
    asyncio.run(create_translation_task("https://www.youtube.com/watch?v=DB9mjd-65gw", "007@qq.com", "DB9mjd-65gw", "Sam Altman on AGI, GPT-5, and what’s next — the OpenAI Podcast Ep. 1"))