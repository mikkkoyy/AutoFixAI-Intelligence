from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"
WORKSPACES_DIR = RUNTIME_DIR / "workspaces"
DB_PATH = RUNTIME_DIR / "autofix.db"

PROVIDER = os.getenv("AUTOFIX_PROVIDER", "deterministic").lower()

# OPENAI_API_KEY makes the OpenAI provider first-class: when AUTOFIX_* is not
# configured, an OpenAI key alone selects the OpenAI-compatible provider.
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()

API_KEY = os.getenv("AUTOFIX_API_KEY", "").strip() or _OPENAI_KEY
BASE_URL = (
    os.getenv("AUTOFIX_BASE_URL", "").strip()
    or ("https://api.openai.com/v1" if _OPENAI_KEY else "")
).rstrip("/")
MODEL = os.getenv("AUTOFIX_MODEL", "").strip() or (
    "gpt-4o-mini" if _OPENAI_KEY else ""
)

for p in (RUNTIME_DIR, WORKSPACES_DIR):
    p.mkdir(parents=True, exist_ok=True)
