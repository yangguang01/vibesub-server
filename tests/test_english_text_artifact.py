"""英文原文 TXT 副产品测试。

通过加载真实 processor.py 并打桩所有外部服务，验证 YouTube 字幕与
AssemblyAI 两条路径都写入同一种纯文本副产品，且上传失败不阻断翻译。
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


SAMPLE_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:02,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:02,000 --> 00:00:04,000\n"
    "Second line\n"
    "\n"
)


class FakeBlob:
    def __init__(self, path, uploads, fail_english=False):
        self.path = path
        self.uploads = uploads
        self.fail_english = fail_english
        self.public_url = f"https://storage.invalid/{path}"

    def upload_from_string(self, content, content_type=None):
        if self.fail_english and self.path.startswith("english_text/"):
            raise RuntimeError("模拟 Firebase 上传失败")
        self.uploads.append((self.path, content, content_type))


class FakeBucket:
    def __init__(self, fail_english=False):
        self.uploads = []
        self.fail_english = fail_english

    def blob(self, path):
        return FakeBlob(path, self.uploads, self.fail_english)


def load_real_processor():
    """测试环境默认把 processor 替换为假模块；这里在独立模块名下加载真实代码。"""
    processor_path = Path(__file__).resolve().parents[1] / "app/worker/processor.py"
    module_name = "_real_processor_for_english_text_tests"

    firebase_init_name = "app.common.utils.firebase_storage_init"
    storage_service_name = "app.common.services.storage"
    originals = {
        firebase_init_name: sys.modules.get(firebase_init_name),
        storage_service_name: sys.modules.get(storage_service_name),
    }

    sys.modules[firebase_init_name] = types.ModuleType(firebase_init_name)
    fake_storage_service = types.ModuleType(storage_service_name)
    fake_storage_service.bucket = None
    sys.modules[storage_service_name] = fake_storage_service

    try:
        spec = importlib.util.spec_from_file_location(module_name, processor_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def configure_common_mocks(monkeypatch, processor, bucket):
    translated = {"called": False}

    monkeypatch.setattr(processor.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(processor, "clear_debug_records", lambda: None)
    monkeypatch.setattr(processor, "update_video_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(processor, "record_successful_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(processor, "get_video_context_from_llm", _video_context)
    monkeypatch.setattr(processor, "process_video_context_data", lambda data: ("prompt", []))
    monkeypatch.setattr(processor, "translate_subtitles", _translate(translated))
    monkeypatch.setattr(processor, "split_long_chinese_sentence_v4", _keep_subtitles)
    monkeypatch.setattr(processor, "save_debug_records_to_storage", _save_no_debug_records)
    return translated


async def _video_context(title, channel):
    return {}


def _translate(translated):
    async def translate(*args, **kwargs):
        translated["called"] = True
        return {1: "你好世界", 2: "第二行"}

    return translate


async def _keep_subtitles(subtitles):
    return subtitles


async def _save_no_debug_records(video_id, bucket):
    return None


def run_task(processor, video_id="video-123"):
    asyncio.run(processor.process_translation_task(
        video_id=video_id,
        youtube_url="https://www.youtube.com/watch?v=video-123",
        user_id="user-1",
        content_name="Test video",
        model="deepseek",
    ))


def assert_english_text_upload(bucket, video_id="video-123"):
    english_uploads = [item for item in bucket.uploads if item[0].startswith("english_text/")]
    assert english_uploads == [(
        f"english_text/{video_id}.txt",
        "Hello world\n\nSecond line",
        "text/plain; charset=utf-8",
    )]
    assert all(not path.startswith("asr_srt/") for path, _, _ in bucket.uploads)


def test_youtube_subtitles_upload_plain_text(monkeypatch):
    processor = load_real_processor()
    bucket = FakeBucket()
    translated = configure_common_mocks(monkeypatch, processor, bucket)
    monkeypatch.setattr(
        processor,
        "download_auto_subtitle",
        lambda url: (Path("video.en.json3"), "Test video", "Test channel"),
    )
    monkeypatch.setattr(processor, "process_ytsub", lambda path: SAMPLE_SRT)

    run_task(processor)

    assert translated["called"] is True
    assert_english_text_upload(bucket)


def test_assemblyai_subtitles_upload_plain_text(monkeypatch):
    processor = load_real_processor()
    bucket = FakeBucket()
    translated = configure_common_mocks(monkeypatch, processor, bucket)
    monkeypatch.setattr(
        processor,
        "download_auto_subtitle",
        lambda url: (None, "Test video", "Test channel"),
    )
    monkeypatch.setattr(
        processor,
        "get_video_info_and_download",
        lambda url: ({"title": "Test video", "channel": "Test channel"}, "audio.webm"),
    )
    monkeypatch.setattr(
        processor,
        "transcribe_audio_with_assemblyai",
        lambda filename: [
            SimpleNamespace(text="Hello world", start=0, end=2000),
            SimpleNamespace(text="Second line", start=2000, end=4000),
        ],
    )
    # 本测试只验证 processor 的 AssemblyAI 分支与统一 TXT 上传。
    # convert_AssemblyAI_to_srt 与 extract_asr_sentences 的既有末行问题不属于本次改动。
    monkeypatch.setattr(processor, "convert_AssemblyAI_to_srt", lambda sentences: SAMPLE_SRT)

    run_task(processor)

    assert translated["called"] is True
    assert_english_text_upload(bucket)


def test_english_text_upload_failure_does_not_block_translation(monkeypatch):
    processor = load_real_processor()
    bucket = FakeBucket(fail_english=True)
    translated = configure_common_mocks(monkeypatch, processor, bucket)
    monkeypatch.setattr(
        processor,
        "download_auto_subtitle",
        lambda url: (Path("video.en.json3"), "Test video", "Test channel"),
    )
    monkeypatch.setattr(processor, "process_ytsub", lambda path: SAMPLE_SRT)

    run_task(processor)

    assert translated["called"] is True
    assert all(not path.startswith("asr_srt/") for path, _, _ in bucket.uploads)
