from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.agents.coding_agent import CodingResult


@dataclass
class DeepSeekWorker:
    """Internal DeepSeek coding worker.

    This integrates with the existing cloud-provider architecture when a
    DEEPSEEK_API_KEY is configured. If it is absent, the worker reports itself
    unavailable and the AutoFix router may fall back to the next worker.
    """

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None

    def __post_init__(self):
        env = os.environ
        self.api_key = self.api_key or str(env.get("DEEPSEEK_API_KEY", "")).strip() or None
        self.base_url = self.base_url or str(env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.org")).strip() or "https://api.deepseek.org"
        self.model = self.model or str(env.get("DEEPSEEK_MODEL", "deepseek-chat")).strip() or "deepseek-chat"

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _call(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("DeepSeek unavailable")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Surface the status code so the router can classify
            # authentication/configuration problems precisely.
            kind = (
                "authentication/configuration error"
                if exc.code in (401, 403)
                else "API error"
            )
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {kind}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            raise RuntimeError(f"DeepSeek request failed: {exc}") from exc

        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DeepSeek returned an unexpected response") from exc

    def execute(self, prompt: str, workspace: str, on_output=None, timeout: int | None = None) -> CodingResult:
        if not self.is_available():
            return CodingResult(
                backend="deepseek",
                success=False,
                error="DeepSeek configuration required: DEEPSEEK_API_KEY is not set.",
                started=False,
            )
        try:
            output = self._call(prompt)
        except Exception as exc:
            return CodingResult(
                backend="deepseek",
                success=False,
                error=str(exc),
                started=True,
            )
        if on_output is not None:
            on_output(output)
        return CodingResult(backend="deepseek", success=True, output=output, started=True, worker_name="deepseek")
