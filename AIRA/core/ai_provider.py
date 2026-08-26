import httpx
import json
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional
from AIRA.core.logging import get_logger

logger = get_logger("ai")


class AIProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], model: str = None,
                   temperature: float = None, max_tokens: int = None) -> str:
        pass

    @abstractmethod
    async def chat_stream(self, messages: list[dict], model: str = None,
                          temperature: float = None, max_tokens: int = None) -> AsyncGenerator[str, None]:
        pass

    @abstractmethod
    async def is_available(self) -> bool:
        pass

    @abstractmethod
    def get_models(self) -> list[str]:
        pass


class OpenAIProvider(AIProvider):
    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, api_key: str, default_model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.default_model = default_model
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    async def chat(self, messages: list[dict], model: str = None,
                   temperature: float = None, max_tokens: int = None) -> str:
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature or 0.7,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            resp = await self.client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"OpenAI chat error: {e}")
            raise

    async def chat_stream(self, messages: list[dict], model: str = None,
                          temperature: float = None, max_tokens: int = None) -> AsyncGenerator[str, None]:
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature or 0.7,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0].get("delta", {})
                            if "content" in delta:
                                yield delta["content"]
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"OpenAI stream error: {e}")
            raise

    async def is_available(self) -> bool:
        try:
            resp = await self.client.get("/models")
            return resp.status_code == 200
        except Exception:
            return False

    def get_models(self) -> list[str]:
        return [
            "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4",
            "gpt-3.5-turbo", "o1", "o1-mini", "o1-pro",
        ]


class AnthropicProvider(AIProvider):
    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, api_key: str, default_model: str = "claude-3-5-sonnet-20241022"):
        self.api_key = api_key
        self.default_model = default_model
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=120.0,
        )

    async def chat(self, messages: list[dict], model: str = None,
                   temperature: float = None, max_tokens: int = None) -> str:
        model = model or self.default_model
        system_msg = ""
        chat_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat_messages.append({"role": m["role"], "content": m["content"]})

        payload = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens or 2048,
            "temperature": temperature or 0.7,
        }
        if system_msg:
            payload["system"] = system_msg

        try:
            resp = await self.client.post("/messages", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]
        except Exception as e:
            logger.error(f"Anthropic chat error: {e}")
            raise

    async def chat_stream(self, messages: list[dict], model: str = None,
                          temperature: float = None, max_tokens: int = None) -> AsyncGenerator[str, None]:
        model = model or self.default_model
        system_msg = ""
        chat_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat_messages.append({"role": m["role"], "content": m["content"]})

        payload = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens or 2048,
            "temperature": temperature or 0.7,
            "stream": True,
        }
        if system_msg:
            payload["system"] = system_msg

        try:
            async with self.client.stream("POST", "/messages", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data.get("type") == "content_block_delta":
                            yield data.get("delta", {}).get("text", "")
        except Exception as e:
            logger.error(f"Anthropic stream error: {e}")
            raise

    async def is_available(self) -> bool:
        return bool(self.api_key)

    def get_models(self) -> list[str]:
        return [
            "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229", "claude-3-haiku-20240307",
        ]


class OllamaProvider(AIProvider):
    BASE_URL = "http://localhost:11434/api"

    def __init__(self, default_model: str = "llama3.1"):
        self.default_model = default_model
        self.client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=120.0)

    async def chat(self, messages: list[dict], model: str = None,
                   temperature: float = None, max_tokens: int = None) -> str:
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature or 0.7},
        }
        try:
            resp = await self.client.post("/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except Exception as e:
            logger.error(f"Ollama chat error: {e}")
            raise

    async def chat_stream(self, messages: list[dict], model: str = None,
                          temperature: float = None, max_tokens: int = None) -> AsyncGenerator[str, None]:
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature or 0.7},
        }
        try:
            async with self.client.stream("POST", "/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        data = json.loads(line)
                        if "message" in data:
                            yield data["message"].get("content", "")
        except Exception as e:
            logger.error(f"Ollama stream error: {e}")
            raise

    async def is_available(self) -> bool:
        try:
            resp = await self.client.get("/tags")
            return resp.status_code == 200
        except Exception:
            return False

    def get_models(self) -> list[str]:
        try:
            import httpx as hx
            resp = hx.get(f"{self.BASE_URL}/tags")
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            pass
        return [self.default_model]


class FallbackProvider(AIProvider):
    def __init__(self, providers: list[AIProvider]):
        self.providers = providers

    async def chat(self, messages, model=None, temperature=None, max_tokens=None):
        for p in self.providers:
            try:
                return await p.chat(messages, model, temperature, max_tokens)
            except Exception as e:
                logger.warning(f"Provider {type(p).__name__} failed: {e}")
                continue
        raise RuntimeError("All AI providers failed")

    async def chat_stream(self, messages, model=None, temperature=None, max_tokens=None):
        for p in self.providers:
            try:
                async for chunk in p.chat_stream(messages, model, temperature, max_tokens):
                    yield chunk
                return
            except Exception as e:
                logger.warning(f"Provider {type(p).__name__} stream failed: {e}")
                continue
        raise RuntimeError("All AI providers failed")

    async def is_available(self):
        for p in self.providers:
            if await p.is_available():
                return True
        return False

    def get_models(self):
        models = []
        for p in self.providers:
            models.extend(p.get_models())
        return models


def create_provider(config) -> AIProvider:
    from AIRA.core.security import get_ai_api_key

    provider_name = config.get("ai.provider", "openai")
    model = config.get("ai.model", "gpt-4o-mini")

    providers = []

    if provider_name == "openai" or provider_name == "any":
        key = get_ai_api_key()
        if key:
            providers.append(OpenAIProvider(key, model))

    if provider_name == "anthropic" or provider_name == "any":
        key = get_ai_api_key()
        if key:
            providers.append(AnthropicProvider(key, model))

    if provider_name == "ollama" or provider_name == "any":
        providers.append(OllamaProvider(model))

    if not providers:
        providers.append(OllamaProvider(model))

    if len(providers) == 1:
        return providers[0]
    return FallbackProvider(providers)
