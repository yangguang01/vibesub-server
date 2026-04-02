import os
import tomllib
from pathlib import Path
from typing import Any, Dict


LLM_TASK_SLOTS = {
    "translation_main": ("translation", "main"),
    "translation_main_strict": ("translation", "strict"),
    "translation_fallback_strong": ("translation", "fallback"),
    "translation_context": ("context", None),
    "alignment_validator": ("alignment", None),
    "sentence_splitter": ("sentence_splitter", None),
}

TRUE_VALUES = {"true", "1", "yes", "on"}
FALSE_VALUES = {"false", "0", "no", "off"}


class ExternalServiceRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._raw = self._load()
        self.providers: Dict[str, Dict[str, Any]] = self._raw.get("providers", {})
        self.tasks: Dict[str, Dict[str, Any]] = self._raw.get("tasks", {})
        self.llm_slots: Dict[str, Dict[str, Any]] = self._raw.get("llm", {})
        self.runtime: Dict[str, Any] = self._raw.get("runtime", {})

    def _load(self) -> Dict[str, Any]:
        with self.path.open("rb") as handle:
            return tomllib.load(handle)

    def get_provider(self, name: str) -> Dict[str, Any]:
        if name not in self.providers:
            raise KeyError(f"未知 provider: {name}")
        return dict(self.providers[name])

    def get_task(self, name: str) -> Dict[str, Any]:
        if name not in self.tasks:
            raise KeyError(f"未知 task: {name}")
        return dict(self.tasks[name])

    def get_llm_slot(self, name: str) -> Dict[str, Any]:
        if name not in self.llm_slots:
            raise KeyError(f"未知 llm slot: {name}")
        return dict(self.llm_slots[name])

    def get_runtime_config(self) -> Dict[str, Any]:
        return dict(self.runtime)

    def _coerce_env_value(self, raw_value: str, template: Any) -> Any:
        if isinstance(template, bool):
            lowered = raw_value.lower()
            if lowered in TRUE_VALUES:
                return True
            if lowered in FALSE_VALUES:
                return False
            raise ValueError(f"无法解析布尔环境变量值: {raw_value}")
        if isinstance(template, int) and not isinstance(template, bool):
            return int(raw_value)
        if isinstance(template, float):
            return float(raw_value)
        return raw_value

    def _apply_provider_env(self, provider: Dict[str, Any], merged: Dict[str, Any]) -> Dict[str, Any]:
        api_key_env = provider.get("api_key_env")
        if api_key_env:
            merged["api_key"] = os.getenv(api_key_env, "")

        base_url_env = provider.get("base_url_env")
        if base_url_env:
            merged["base_url"] = os.getenv(base_url_env, merged.get("base_url", ""))

        project_env = provider.get("project_env")
        if project_env:
            merged["project"] = os.getenv(project_env, "")

        credentials_env = provider.get("credentials_env")
        if credentials_env:
            merged["credentials"] = os.getenv(credentials_env, "")

        target_url_env = provider.get("target_url_env")
        if target_url_env:
            merged["target_url"] = os.getenv(target_url_env, "")

        proxy_env = provider.get("proxy_env")
        if proxy_env:
            merged["proxy"] = os.getenv(proxy_env, "")

        bucket_env = provider.get("bucket_env")
        if bucket_env:
            merged["bucket"] = os.getenv(bucket_env, merged.get("bucket", ""))

        return merged

    def _apply_llm_env_overrides(self, slot_name: str, slot_config: Dict[str, Any]) -> Dict[str, Any]:
        updated = dict(slot_config)
        env_prefix = f"LLM_{slot_name.upper()}_".replace("-", "_")
        for key, current_value in list(updated.items()):
            env_name = f"{env_prefix}{key.upper()}".replace("-", "_")
            env_value = os.getenv(env_name)
            if env_value in {None, ""}:
                continue
            updated[key] = self._coerce_env_value(env_value, current_value)
        return updated

    def get_llm_task_config(self, task_name: str) -> Dict[str, Any]:
        if task_name not in LLM_TASK_SLOTS:
            raise KeyError(f"未知 llm task: {task_name}")

        slot_name, variant = LLM_TASK_SLOTS[task_name]
        slot_config = self._apply_llm_env_overrides(slot_name, self.get_llm_slot(slot_name))
        provider_name = str(slot_config["provider"]).strip()
        provider = self.get_provider(provider_name)
        merged = {**provider, **slot_config, "provider_name": provider_name, "llm_slot": slot_name}

        if variant:
            merged["llm_variant"] = variant
            merged["temperature"] = slot_config.get(f"temperature_{variant}", merged.get("temperature"))
            merged["top_p"] = slot_config.get(f"top_p_{variant}", merged.get("top_p"))

        return self._apply_provider_env(provider, merged)

    def get_runtime_value(self, key: str, env_name: str | None = None, default: Any = None) -> Any:
        if key not in self.runtime and default is None:
            raise KeyError(f"未知 runtime 配置: {key}")

        value = self.runtime.get(key, default)
        if env_name:
            env_value = os.getenv(env_name)
            if env_value not in {None, ""}:
                return self._coerce_env_value(env_value, value)
        return value

    def get_task_config(self, name: str, provider_override: str | None = None) -> Dict[str, Any]:
        if name in LLM_TASK_SLOTS:
            return self.get_llm_task_config(name)

        task = self.get_task(name)
        provider_name = provider_override or task["provider"]
        provider = self.get_provider(provider_name)
        merged = {**provider, **task, "provider_name": provider_name}
        return self._apply_provider_env(provider, merged)
