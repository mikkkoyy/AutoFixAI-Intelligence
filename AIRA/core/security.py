import os
from pathlib import Path
from dotenv import load_dotenv

ENV_FILE = Path(__file__).parent.parent.parent / ".env"


def load_environment():
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)


def get_secret(key: str, default: str = None) -> str:
    return os.getenv(key, default)


def get_ai_api_key() -> str:
    # Preferred project-specific variable.
    key = get_secret("AIRA_AI_API_KEY")

    # Fall back to the configured variable name.
    if not key:
        from AIRA.config import config
        key_env_name = config.get("ai.api_key_env", "AIRA_AI_API_KEY")
        if key_env_name != "AIRA_AI_API_KEY":
            key = get_secret(key_env_name)

    # Standard OpenAI environment variable fallback.
    if not key:
        key = get_secret("OPENAI_API_KEY")

    return key
