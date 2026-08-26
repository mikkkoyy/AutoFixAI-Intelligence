import secrets
import hashlib
from pathlib import Path
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")

TOKEN_DIR = Path(__file__).parent.parent.parent / ".pc_agent"
TOKEN_FILE = TOKEN_DIR / "auth_token"


def generate_auth_token() -> str:
    return secrets.token_hex(32)


def load_or_create_token() -> str:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    token = generate_auth_token()
    TOKEN_FILE.write_text(token, encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    logger.info("Generated new PC Agent authentication token")
    return token


def verify_token(provided_token: str, stored_token: str) -> bool:
    if not provided_token or not stored_token:
        return False
    return secrets.compare_digest(provided_token, stored_token)


def hash_for_log(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
