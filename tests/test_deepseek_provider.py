import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AIRA.config import config
from AIRA.core.ai_provider import (
    AIProviderError,
    DeepSeekProvider,
    OllamaProvider,
    create_provider,
)


def _mock_client(handler, api_key="test-key", base_url=None):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=base_url or "https://api.deepseek.com",
        headers={"Authorization": f"Bearer {api_key}"},
    )


@pytest.fixture
def reset_config():
    config.load()
    yield
    config.load()


# ---- configuration ----


def test_deepseek_provider_defaults():
    provider = DeepSeekProvider("test-key")
    assert provider.default_model == "deepseek-v4-flash"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.get_models() == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_deepseek_provider_custom_connection():
    provider = DeepSeekProvider(
        "test-key",
        default_model="deepseek-v4-pro",
        base_url="https://proxy.example.com/",
        timeout=45,
    )
    assert provider.default_model == "deepseek-v4-pro"
    assert provider.base_url == "https://proxy.example.com"
    assert provider.timeout == 45


def test_deepseek_provider_requires_key():
    with pytest.raises(AIProviderError):
        DeepSeekProvider("")


# ---- successful mocked response ----


async def test_deepseek_chat_success():
    def handler(request):
        assert request.url.path == "/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["stream"] is False
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Fixed the bug"}}]},
        )

    provider = DeepSeekProvider("test-key")
    provider.client = _mock_client(handler)
    try:
        result = await provider.chat([{"role": "user", "content": "analyze"}])
        assert result == "Fixed the bug"
    finally:
        await provider.client.aclose()


async def test_deepseek_chat_stream_success():
    def handler(request):
        body = (
            "data: {\"choices\": [{\"delta\": {\"content\": \"Hel\"}}]}\n"
            "data: {\"choices\": [{\"delta\": {\"content\": \"lo\"}}]}\n"
            "data: [DONE]\n"
        )
        return httpx.Response(200, text=body)

    provider = DeepSeekProvider("test-key")
    provider.client = _mock_client(handler)
    try:
        chunks = [c async for c in provider.chat_stream([{"role": "user", "content": "hi"}])]
        assert "".join(chunks) == "Hello"
    finally:
        await provider.client.aclose()


async def test_deepseek_is_available():
    def handler(request):
        assert request.url.path == "/models"
        return httpx.Response(200, json={"data": []})

    provider = DeepSeekProvider("test-key")
    provider.client = _mock_client(handler)
    try:
        assert await provider.is_available() is True
    finally:
        await provider.client.aclose()


# ---- authentication failure ----


async def test_deepseek_auth_failure():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    provider = DeepSeekProvider("bad-key")
    provider.client = _mock_client(handler)
    try:
        with pytest.raises(AIProviderError) as excinfo:
            await provider.chat([{"role": "user", "content": "hi"}])
        assert "authenticat" in str(excinfo.value).lower()
    finally:
        await provider.client.aclose()


# ---- timeout ----


async def test_deepseek_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    provider = DeepSeekProvider("test-key", timeout=5)
    provider.client = _mock_client(handler)
    try:
        with pytest.raises(AIProviderError) as excinfo:
            await provider.chat([{"role": "user", "content": "hi"}])
        assert "timed out" in str(excinfo.value)
    finally:
        await provider.client.aclose()


# ---- connection failure ----


async def test_deepseek_connection_failure():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    provider = DeepSeekProvider("test-key")
    provider.client = _mock_client(handler)
    try:
        assert await provider.is_available() is False
        with pytest.raises(AIProviderError) as excinfo:
            await provider.chat([{"role": "user", "content": "hi"}])
        assert "not reachable" in str(excinfo.value)
    finally:
        await provider.client.aclose()


# ---- invalid response ----


async def test_deepseek_invalid_response():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = DeepSeekProvider("test-key")
    provider.client = _mock_client(handler)
    try:
        with pytest.raises(AIProviderError) as excinfo:
            await provider.chat([{"role": "user", "content": "hi"}])
        assert "unexpected response" in str(excinfo.value)
    finally:
        await provider.client.aclose()


# ---- model failure ----


async def test_deepseek_model_failure():
    def handler(request):
        return httpx.Response(404, json={"error": {"message": "Model Not Exists"}})

    provider = DeepSeekProvider("test-key", default_model="deepseek-not-real")
    provider.client = _mock_client(handler)
    try:
        with pytest.raises(AIProviderError) as excinfo:
            await provider.chat([{"role": "user", "content": "hi"}])
        message = str(excinfo.value)
        assert "deepseek-not-real" in message
        assert "not found" in message
    finally:
        await provider.client.aclose()


# ---- provider selection ----


def test_create_provider_selects_deepseek(reset_config):
    os.environ["DEEPSEEK_API_KEY"] = "test-key"
    try:
        config.set("ai.provider", "deepseek")
        provider = create_provider(config)
        assert isinstance(provider, DeepSeekProvider)
        assert provider.default_model == "deepseek-v4-flash"
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_create_provider_deepseek_custom_config(reset_config):
    os.environ["DEEPSEEK_API_KEY"] = "test-key"
    try:
        config.set("ai.provider", "deepseek")
        config.set("ai.deepseek.base_url", "https://proxy.example.com")
        config.set("ai.deepseek.model", "deepseek-v4-pro")
        config.set("ai.deepseek.timeout", 40)
        provider = create_provider(config)
        assert isinstance(provider, DeepSeekProvider)
        assert provider.base_url == "https://proxy.example.com"
        assert provider.default_model == "deepseek-v4-pro"
        assert provider.timeout == 40
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_create_provider_deepseek_needs_key(reset_config):
    os.environ.pop("DEEPSEEK_API_KEY", None)
    config.set("ai.provider", "deepseek")
    provider = create_provider(config)
    assert isinstance(provider, OllamaProvider)


# ---- Ollama provider compatibility ----


def test_create_provider_selects_ollama_unchanged(reset_config):
    os.environ.pop("DEEPSEEK_API_KEY", None)
    config.set("ai.provider", "ollama")
    provider = create_provider(config)
    assert isinstance(provider, OllamaProvider)
    assert provider.host == "http://127.0.0.1:11434"


def test_ollama_provider_interface_unchanged(reset_config):
    provider = OllamaProvider("llama3.1")
    assert isinstance(provider, OllamaProvider)
    assert provider.default_model == "llama3.1"