import asyncio

from app.common.core.logging import logger
from app.common.core.config import get_task_config
from app.common.models.firestore_models import (
    get_task,
    get_video_task,
    mark_task_completed,
    update_video_task,
)
from app.common.services.download_ytsub import download_auto_subtitle
from app.common.services.infrastructure import get_storage_bucket
from app.common.services.process_ytsub import process_ytsub
from app.common.services.translation import (
    clear_debug_records,
    convert_AssemblyAI_to_srt,
    extract_asr_sentences,
    format_subtitles_v2,
    get_video_context_from_llm,
    get_video_info_and_download,
    map_chinese_to_time_ranges_v2,
    map_marged_sentence_to_timeranges,
    process_video_context_data,
    save_debug_records_to_local,
    save_debug_records_to_storage,
    split_long_chinese_sentence_v4,
    subtitles_to_dict,
    transcribe_audio_with_assemblyai,
    translate_subtitles,
)
from app.common.services.llm_runtime import NonRetryableLLMError
from app.common.utils.executor import executor


class PermanentTaskError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


PERMANENT_ERROR_KEYWORDS = (
    "private video",
    "video unavailable",
    "invalid url",
    "authentication",
    "permission denied",
    "quota exceeded",
    "model not found",
    "无法从 url 提取",
)


def _is_permanent_error(message: str) -> bool:
    lowered = message.lower()
    return any(keyword in lowered for keyword in PERMANENT_ERROR_KEYWORDS)


async def _upload_text(bucket, path: str, content: str, content_type: str = "text/plain") -> str:
    loop = asyncio.get_event_loop()

    def _upload():
        blob = bucket.blob(path)
        blob.upload_from_string(content, content_type=content_type)
        return blob.public_url

    return await loop.run_in_executor(executor, _upload)


async def _load_source_subtitles(
    *,
    loop,
    bucket,
    video_id: str,
    youtube_url: str,
    content_name: str,
    asr_storage_prefix: str,
):
    video_title = content_name
    channel_name = ""

    try:
        yt_sub_path, video_title, channel_name = await loop.run_in_executor(
            executor,
            download_auto_subtitle,
            youtube_url,
        )
    except Exception as exc:
        logger.warning(f"YouTube 字幕下载阶段失败，回退 ASR: {exc}")
        yt_sub_path = None

    if yt_sub_path:
        try:
            srt_text = await loop.run_in_executor(executor, process_ytsub, yt_sub_path)
            english_srt_url = await _upload_text(
                bucket,
                f"{asr_storage_prefix}/{video_id}.srt",
                srt_text,
                "text/plain",
            )
            return {
                "source_type": "youtube_subtitle",
                "srt_text": srt_text,
                "english_srt_url": english_srt_url,
                "video_title": video_title,
                "channel_name": channel_name,
            }
        except Exception as exc:
            logger.warning(f"YouTube 字幕解析阶段失败，回退 ASR: {exc}")

    video_info, filename = await loop.run_in_executor(executor, get_video_info_and_download, youtube_url)
    video_title = video_info.get("title", content_name)
    channel_name = video_info.get("channel", "")
    asr_result = await loop.run_in_executor(executor, transcribe_audio_with_assemblyai, filename)
    srt_text = convert_AssemblyAI_to_srt(asr_result)
    english_srt_url = await _upload_text(
        bucket,
        f"{asr_storage_prefix}/{video_id}.srt",
        srt_text,
        "text/plain",
    )
    return {
        "source_type": "asr",
        "srt_text": srt_text,
        "english_srt_url": english_srt_url,
        "video_title": video_title,
        "channel_name": channel_name,
    }


async def process_translation_task(
    task_id,
    video_id,
    youtube_url,
    user_id,
    content_name,
    special_terms="",
    language="zh-CN",
    model="",
):
    loop = asyncio.get_event_loop()
    bucket = get_storage_bucket()
    subtitle_storage = get_task_config("subtitle_storage")
    asr_storage = get_task_config("asr_storage")
    current_stage = "download"
    trans_strategies = []
    english_srt_url = ""
    video_context_prompt = ""
    video_title = content_name

    clear_debug_records(video_id)

    try:
        update_video_task(video_id, stage="download", progress=0.1)
        source_result = await _load_source_subtitles(
            loop=loop,
            bucket=bucket,
            video_id=video_id,
            youtube_url=youtube_url,
            content_name=content_name,
            asr_storage_prefix=asr_storage["path_prefix"],
        )
        srt_text = source_result["srt_text"]
        english_srt_url = source_result["english_srt_url"]
        video_title = source_result["video_title"]
        channel_name = source_result["channel_name"]

        if source_result["source_type"] == "asr":
            current_stage = "context"
            update_video_task(
                video_id,
                stage=current_stage,
                progress=0.25,
                extra_updates={"video_title": video_title},
            )

            current_stage = "asr"
            update_video_task(video_id, stage=current_stage, progress=0.45, english_srt_url=english_srt_url)

        current_stage = "context"
        update_video_task(video_id, stage=current_stage, progress=0.3)
        video_context_data = await get_video_context_from_llm(video_title, channel_name)
        video_context_prompt, trans_strategies = process_video_context_data(video_context_data)
        update_video_task(
            video_id,
            stage=current_stage,
            progress=0.4,
            translation_strategies=trans_strategies,
            extra_updates={"video_title": video_title},
        )

        current_stage = "translate"
        update_video_task(video_id, stage=current_stage, progress=0.6)
        numbered_sentences = extract_asr_sentences(srt_text)
        translated_result = await translate_subtitles(
            numbered_sentences,
            video_context_prompt,
            model,
            special_terms,
            content_name,
            video_id,
        )

        current_stage = "validate_alignment"
        update_video_task(video_id, stage=current_stage, progress=0.75)

        current_stage = "postprocess"
        subtitles_dict = subtitles_to_dict(srt_text)
        merged_timeranges = map_marged_sentence_to_timeranges(numbered_sentences, subtitles_dict)
        chinese_timeranges = map_chinese_to_time_ranges_v2(translated_result, merged_timeranges)
        short_chinese_subtitles = await split_long_chinese_sentence_v4(chinese_timeranges)
        cn_srt_content = format_subtitles_v2(short_chinese_subtitles)

        current_stage = "upload"
        update_video_task(video_id, stage=current_stage, progress=0.9)
        chinese_srt_url = await _upload_text(
            bucket,
            f"{subtitle_storage['path_prefix']}/{video_id}.srt",
            cn_srt_content,
            "text/plain",
        )

        local_debug_paths = save_debug_records_to_local(video_id)
        logger.info(
            "本地调试文件已保存: debug=%s alignment_json=%s alignment_report=%s alignment_overview=%s",
            local_debug_paths["debug_text_path"],
            local_debug_paths["alignment_json_path"],
            local_debug_paths["alignment_text_path"],
            local_debug_paths["alignment_overview_text_path"],
        )
        debug_url = await save_debug_records_to_storage(video_id, bucket) or ""
        mark_task_completed(
            video_id,
            result_url=chinese_srt_url,
            english_srt_url=english_srt_url,
            debug_url=debug_url,
        )
        return {"video_id": video_id, "result_url": chinese_srt_url}

    except NonRetryableLLMError as exc:
        local_debug_paths = save_debug_records_to_local(video_id)
        logger.info(
            "本地调试文件已保存: debug=%s alignment_json=%s alignment_report=%s alignment_overview=%s",
            local_debug_paths["debug_text_path"],
            local_debug_paths["alignment_json_path"],
            local_debug_paths["alignment_text_path"],
            local_debug_paths["alignment_overview_text_path"],
        )
        debug_url = await save_debug_records_to_storage(video_id, bucket) or ""
        update_video_task(video_id, debug_url=debug_url)
        raise PermanentTaskError(current_stage, str(exc)) from exc
    except Exception as exc:
        local_debug_paths = save_debug_records_to_local(video_id)
        logger.info(
            "本地调试文件已保存: debug=%s alignment_json=%s alignment_report=%s alignment_overview=%s",
            local_debug_paths["debug_text_path"],
            local_debug_paths["alignment_json_path"],
            local_debug_paths["alignment_text_path"],
            local_debug_paths["alignment_overview_text_path"],
        )
        debug_url = await save_debug_records_to_storage(video_id, bucket) or ""
        update_video_task(
            video_id,
            stage=current_stage,
            failure_stage=current_stage,
            failure_message=str(exc),
            error=str(exc),
            debug_url=debug_url,
        )
        if _is_permanent_error(str(exc)):
            raise PermanentTaskError(current_stage, str(exc)) from exc
        raise


async def create_translation_task(
    task_id,
    youtube_url,
    user_id,
    video_id,
    content_name,
    special_terms="",
    language="zh-CN",
    model="",
):
    await process_translation_task(
        task_id=task_id,
        video_id=video_id,
        youtube_url=youtube_url,
        user_id=user_id,
        content_name=content_name,
        special_terms=special_terms,
        language=language,
        model=model,
    )
    return video_id


def get_task_status(task_id):
    return get_task(task_id)


def get_task_translation_strategies(task_id):
    task_data = get_task(task_id)
    if task_data:
        return {"strategies": task_data.get("translation_strategies", [])}
    return None
