import os
import yaml
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path(__file__).parent
PROJECT_ROOT = CONFIG_DIR.parent.parent


class Config:
    _instance = None
    _data: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self, local_path: Optional[str] = None):
        defaults_path = CONFIG_DIR / "defaults.yaml"
        with open(defaults_path, "r") as f:
            self._data = yaml.safe_load(f) or {}

        if local_path:
            p = Path(local_path)
        else:
            p = CONFIG_DIR / "config_local.yaml"
        if p.exists():
            with open(p, "r") as f:
                local = yaml.safe_load(f) or {}
            self._deep_merge(self._data, local)

        env_overrides = {
            "ai.provider": os.getenv("AIRA_AI_PROVIDER"),
            "ai.model": os.getenv("AIRA_AI_MODEL"),
            "ai.api_key_env": os.getenv("AIRA_AI_API_KEY_ENV"),
            "ai.ollama.host": os.getenv("AIRA_OLLAMA_HOST"),
            "ai.ollama.model": os.getenv("AIRA_OLLAMA_MODEL"),
            "ai.deepseek.base_url": os.getenv("AIRA_DEEPSEEK_BASE_URL"),
            "ai.deepseek.model": os.getenv("AIRA_DEEPSEEK_MODEL"),
            "ai.deepseek.timeout": os.getenv("AIRA_DEEPSEEK_TIMEOUT"),
        }
        for key, val in env_overrides.items():
            if val is not None:
                self.set(key, val)

        return self

    def _deep_merge(self, base: dict, override: dict):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def set(self, key: str, value: Any):
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            if k not in d:
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    @property
    def data(self) -> dict:
        return self._data


config = Config()
