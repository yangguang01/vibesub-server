import os
import tomllib
from pathlib import Path
from typing import Any, Dict


class ExternalServiceRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._raw = self._load()
        self.providers: Dict[str, Dict[str, Any]] = self._raw.get("providers", {})
        self.tasks: Dict[str, Dict[str, Any]] = self._raw.get("tasks", {})

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

    def get_task_config(self, name: str, provider_override: str | None = None) -> Dict[str, Any]:
        task = self.get_task(name)
        provider_name = provider_override or task["provider"]
        provider = self.get_provider(provider_name)
        merged = {**provider, **task, "provider_name": provider_name}

        api_key_env = provider.get("api_key_env")
        if api_key_env:
            merged["api_key"] = os.getenv(api_key_env, "")

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
