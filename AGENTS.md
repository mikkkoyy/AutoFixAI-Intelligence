# AGENTS.md

## Quick commands

```powershell
# From repo root — activate the root venv first
.venv\Scripts\Activate.ps1
pytest -q                        # runs tests/ (imports frontend/app/*)
python main.py                   # launches PySide6 GUI
```

```powershell
# Backend only (separate venv)
cd backend
.venv\Scripts\Activate.ps1
pytest -q                        # runs backend/tests/
python -m uvicorn app.main:app --reload   # starts FastAPI on :8000
```

```powershell
# Frontend only (separate venv)
cd frontend
.venv\Scripts\Activate.ps1
python -m app.main               # launches PySide6 GUI
python -m pytest -q              # runs frontend/tests/
```

## Structure

Three overlapping Python packages, each with its own `.venv` and `requirements.txt`:

- **Root** — thin launcher (`main.py`) that imports `frontend.app.ui.main_window`.
- **`frontend/`** — PySide6 desktop app. `app/` contains the GUI, builder, verifier, orchestrator, and dependency checker. Connects to the backend API at `http://127.0.0.1:8000`.
- **`backend/`** — FastAPI REST API. `app/` contains API routes, pipeline services, security, DB, and provider abstraction. Persists to SQLite at `backend/runtime/autofix.db`.
- **`workspace/`** — generated test projects created by the builder.

The root `tests/` directory imports from `frontend/app/` (the `app` package on `sys.path`), not from `backend/`. The backend has its own isolated test suite in `backend/tests/`.

## Gotchas

- **`app` package ambiguity**: root `tests/` resolves `import app.*` against `frontend/app/`, not `backend/app/`. Never assume `app.*` means the backend from the root. Root and `frontend/tests/` overlap — they test the same frontend modules.
- **Backend has its own venv**: `backend/requirements.txt` includes FastAPI/uvicorn/httpx — not installed in the root venv. Activate `backend/.venv` before running backend tests or uvicorn.
- **Checkpoint files**: `backend/app/services/*.checkpoint.py` and `frontend/app/ui/main_window.*.py` are snapshots. Not imported. Do not edit or run them.
- **`backend/runtime/`** is generated (SQLite DB, workspace copies). Created on import by `backend/app/config.py`.
- **`cmd`** is a scratch pad, not a script. Do not execute as-is.
- **Security allowlist**: `backend/app/security.py` only permits `pytest` and `python`. Shell chaining (`&&`, `||`, `;`, `|`, `>`, `<`) is blocked.
- **Runner rewrites pytest**: `backend/app/services/runner.py` resolves `pytest` commands to `sys.executable -m pytest` so subprocesses use the same venv as the backend, not the system PATH.
- **Env config**: backend reads `AUTOFIX_PROVIDER`, `AUTOFIX_API_KEY`, `AUTOFIX_BASE_URL`, `AUTOFIX_MODEL` from `backend/.env`. Default provider is `deterministic` (no API key needed). `AUTOFIX_PROVIDER=openai` (alias of `openai-compatible`) makes OpenAI first-class; when `AUTOFIX_API_KEY`/`AUTOFIX_BASE_URL`/`AUTOFIX_MODEL` are unset, `OPENAI_API_KEY` supplies credentials with the default base URL (`https://api.openai.com/v1`) and model (`gpt-4o-mini`). Never hard-code keys.
- **Frontend provider layer**: `frontend/app/agents/chat_provider.py` is the single provider abstraction for Chat, AutoFix planning and Bulk. Providers: GPT (`OPENAI_API_KEY`), Claude (`ANTHROPIC_API_KEY`), DeepSeek (`DEEPSEEK_API_KEY`) and the backend-style `AUTOFIX_PROVIDER/AUTOFIX_API_KEY/AUTOFIX_BASE_URL`. With no key configured everything falls back to the honest local assistant / deterministic planner. Cloud calls get relevant redacted project memory via `memory_context_block`; `analyze()` powers AutoFix planning through the same layer.
- **APPROVE & EXECUTE**: always visible in every chat mode; the safety gate is its enabled state (disabled until a plan awaits approval, re-disabled after approval). Mode switches never hide it. Nothing executes without it.
- **Large input**: AutoFix auto-detects large pastes inside Chat/AutoFix. Threshold is `AUTOFIX_LARGE_INPUT_THRESHOLD` (characters, default 4000, read by `frontend/app/agents/large_input.py`). Large tasks are persisted (full request + stage results) under `<workspace>\.autofix\memory\` by `frontend/app/agents/task_memory.py` for recovery.
- **Large / multi-line input**: any large request is accepted by the existing AutoFix task flow. The full original request is preserved under `.autofix/tasks` and the task transport, while the short UI label is only a compact display aid. Bulk is not a user-facing mode and never defines a separate execution engine.
- **Chat intent routing**: Chat mode classifies every message deterministically via `frontend/app/agents/intent.py` (`classify_intent`) BEFORE contacting any provider. High-confidence CODING_TASK/DEBUG_TASK/PROJECT_MODIFICATION messages are handed to the existing AutoFix flow (`set_ai_mode("autofix")` + `_handle_autofix_message` with the original text verbatim) and the mode switch is announced in the transcript. No cloud API key is ever required for chat or for routing; without a provider key `LocalAssistant` answers locally (greetings, capability questions, small built-in knowledge base in `chat_provider.py`). Regression tests: `tests/test_chat_intent_routing.py`.
- **Task transport**: coding CLIs never receive huge prompts as one argv element (Windows 32k command-line cap → `opencode exited with code 1`). `frontend/app/agents/task_transport.py` persists the complete request to `<workspace>\.autofix\tasks\<task_id>.json` and passes a compact bootstrap instruction instead. Inline cutoff: `AUTOFIX_INLINE_PROMPT_LIMIT` (chars, default 2000).
- **Persistent task & recovery**: `frontend/app/agents/autofix_task.py` owns the durable task record (`<workspace>\.autofix\tasks\autofix-task-<id>.json`). Rule: process exit ≠ completion — completion requires verification. Unexpected stops trigger same-task recovery via `RecoveryAgent` (orchestrator.py), bounded by `AUTOFIX_MAX_RECOVERY_ATTEMPTS` (default 3) before `RECOVERY_REQUIRED`. User STOP → `CANCELLED`, never auto-restarts.
- **Project memory**: `<workspace>\.autofix\memory\{conversations,tasks,sessions,fixes,errors,decisions,index}` written by `task_memory.py` (`record_memory`/`record_session`); secret-looking values are redacted; retrieval is keyword-based (`retrieve_relevant`); cleanup (`cleanup_memory`/`delete_memory_paths`) can only ever delete files below `.autofix\memory\`.
- **Conversational Chat brain**: `frontend/app/agents/chat_intelligence.py` layers refined intents (greeting/question/analysis/brainstorm/recommendation/plan/proposal/coding/project/change/clarification) on top of `intent.py` — no second intent system. `ChatEngine.handle()` produces deterministic proposal cards (AWAITING APPROVAL), applies revisions to the SAME in-flight proposal, resolves contextual follow-ups against recent history + redacted project memory + prior task records, and asks ONE clarification question only when the subject is materially unresolvable. Architecture-conflict requests ("new Bulk engine", "bypass WorkerRouter") get a conversational correction, never a proposal. Approval responses carry the origin request + final execution prompt. Chat NEVER executes: approval hands `execution_prompt` to the existing `ApprovalPipeline` via `on_approve_plan` (task record keeps verbatim `original_request`, prompt on `approved_prompt`). Large build specs still route straight to AutoFix planning; worker notifications/failover/recovery are untouched.
- **Runtime verification scripts**: `scripts/runtime_verify_failover.py`, `runtime_verify_normal.py`, `runtime_verify_auth_notifications.py`, `runtime_verify_conversation.py` drive real pipelines with injected deterministic worker doubles (never real CLIs). Gotcha for doubles: subtask results containing "error"/"failed" fail `_verify_subtask` — word success output neutrally.

## OpenCode

- OpenCode is installed via npm global: `npm install -g opencode-ai`
- Resolve `opencode` through PATH; do not hard-code the npm directory
- OpenCode integration lives in `frontend/app/agents/opencode/` (discovery, process, workspace modules)
- OpenCode must run with the currently selected Explorer workspace as its working directory
- Current file context is passed when available via the editor tab
- OpenCode process runs via QProcess, not through the backend security layer
- Explorer context menu provides "OpenCode Here" for any folder

## Launcher (.exe)

The `launcher/` directory contains a standalone launcher that starts the backend, waits for its health check, then launches the frontend:

```powershell
# Run the launcher in development mode (no build required)
.venv\Scripts\Activate.ps1
python launcher\launcher.py

# Build the single-file Windows .exe
.\launcher\build_launcher.ps1

# The built .exe lives at:
launcher\dist\launcher.exe     # 43 MB, single-file
```

Key details:
- The launcher resolves the project root by walking up from its own location looking for `backend/app/main.py`
- It starts the backend via `backend/.venv/Scripts/python.exe -m uvicorn app.main:app`
- It polls `GET /health` until the backend responds `{"status": "ok"}`
- It starts the frontend via `frontend/.venv/Scripts/python.exe -m app.main`
- Logs are written to `logs/launcher.log`
- Uses a lock file (`logs/autofix-studio.lock`) to prevent duplicate instances
- Does **not** start or kill OpenCode — that's managed by the frontend's QProcess integration

## Compile check

```powershell
python -m compileall app tests
```

## Test configuration

Three separate pytest suites, each running in its own `.venv`:

```powershell
# Root tests (frontend-oriented, imports frontend/app/*)
.venv\Scripts\Activate.ps1
pytest -q
# Collects only from tests/; excludes backend/, frontend/, workspace/
# Requires frontend/app on pythonpath (configured in pytest.ini)
```

```powershell
# Backend tests (FastAPI, security, pipeline)
cd backend
.venv\Scripts\Activate.ps1
pytest -q
# Collects only from backend/tests/; excludes runtime/
# Requires backend/ on pythonpath (configured in backend/pytest.ini)
```

```powershell
# Frontend tests (client)
cd frontend
.venv\Scripts\Activate.ps1
python -m pytest -q
# Collects only from frontend/tests/
```

`backend/runtime/workspaces/` and `workspace/` are generated project directories and must not be collected by repository-level pytest. Both `pytest.ini` files use `norecursedirs` to exclude them.
