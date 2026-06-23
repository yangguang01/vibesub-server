"""
pytest 公共夹具 / 导入桩。

目的：让 P0-timeout 相关的单元测试能在没有真实 Firebase / GCP 凭证的环境下运行。
- `app.api.cloud_tasks` 顶部会 `from app.worker.processor import create_translation_task`，
  而 `app.worker.processor` 在导入时就初始化 Firebase（需要真实证书文件）。
  我们在测试里用一个轻量假模块替换 `app.worker.processor`，避免触发 Firebase 初始化。
- Firestore 等真正的读写调用在各测试里用 monkeypatch 打桩。
"""
import sys
import types


def _install_fake_processor():
    """用假的 app.worker.processor 替换真实模块，绕开 Firebase 导入时初始化。"""
    if "app.worker.processor" in sys.modules:
        return

    fake = types.ModuleType("app.worker.processor")

    async def create_translation_task(**kwargs):  # 默认实现：什么都不做
        return kwargs.get("video_id")

    def get_task_status(task_id):
        return None

    def get_task_translation_strategies(task_id):
        return None

    fake.create_translation_task = create_translation_task
    fake.get_task_status = get_task_status
    fake.get_task_translation_strategies = get_task_translation_strategies
    sys.modules["app.worker.processor"] = fake


# 在任何测试模块 import app.api.cloud_tasks / app.api.tasks 之前先装好假模块
_install_fake_processor()
