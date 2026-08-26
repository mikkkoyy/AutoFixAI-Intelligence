# AutoFix AI Studio v0.2

VS Code-inspired desktop IDE with an AI pair: a conversational **AI Chat** brain and an approval-gated **AutoFix** execution engine, backed by a FastAPI service.

## What's inside

UI:
- Project Explorer (right-click any folder → "OpenCode Here")
- Code Editor
- AI Agent Orchestrator
- AI Assistant (Chat / AutoFix modes)
- Bottom Output/Verification panel
- Build/Test/Analyze/Security/Agents toolbar actions
- Dark modern IDE styling

Backend wiring:
- ProjectBuilder
- ProjectVerifier
- AgentOrchestrator

## AI Chat

Chat mode is a full conversational assistant — no API key required to start:

- **Intent routing** (`frontend/app/agents/intent.py`): every message is classified locally BEFORE any provider contact. High-confidence coding/debug/project-modification requests are handed to the AutoFix flow automatically; the mode switch is announced in the transcript.
- **Conversation intelligence** (`chat_intelligence.py`): refined intents, deterministic proposal cards (AWAITING APPROVAL), same-proposal revisions, contextual follow-up resolution against recent history + redacted project memory + prior task records, one clarifying question only when the subject is materially unresolvable.
- **Architecture guardrails**: requests that would break the locked design ("add a second Bulk engine", "let chat bypass the worker router") get a conversational correction — never a proposal.
- **Local fallback**: with no provider key configured, the built-in honest local assistant answers greetings, capability questions and common coding topics. Chat NEVER executes anything.

### Provider auto-detection & fallback

Providers are detected from existing environment configuration — there is nothing to select in the UI (`chat_provider.py::provider_chain`):

| Key(s) | Provider |
| --- | --- |
| `AUTOFIX_PROVIDER` (+ `AUTOFIX_API_KEY`, `AUTOFIX_BASE_URL`, `AUTOFIX_MODEL`) | AutoFix Model (preferred when set) |
| `OPENAI_API_KEY` | GPT |
| `ANTHROPIC_API_KEY` | Claude |
| `DEEPSEEK_API_KEY` | DeepSeek |

- The first usable provider answers; on failure the next configured provider is tried automatically.
- `AUTOFIX_CHAT_PROVIDER` (fuzzy name match, e.g. `claude`) overrides the preference order for chat only.
- If every configured provider fails, you get a clean, credential-free error listing what was tried. The local assistant is used ONLY when no provider is configured at all.

## AutoFix

AutoFix analyzes, plans, executes and verifies development tasks — always behind explicit approval:

- **APPROVE & EXECUTE** button gates every plan. Nothing runs without it.
- Plans are produced by the configured cloud provider (with shared-knowledge context, see below) or by the deterministic local planner.
- Coding CLIs receive large prompts via the task transport (`task_transport.py`) instead of oversized argv.
- Durable task records live in `<workspace>\.autofix\tasks\`; unexpected stops trigger bounded same-task recovery before flagging RECOVERY_REQUIRED. User STOP means CANCELLED — never auto-restarted.

### Internal workers

OpenCode, Copilot CLI and similar coding CLIs are internal execution workers. They run silently inside approved tasks, with automatic failover when one is unavailable or unauthenticated. Worker notifications surface in the output panel as status lines only — they never interrupt chat and are not user-facing agents.

## Project memory vs. Shared AI knowledge

Two strictly separated stores:

| | Project memory | Shared AI knowledge |
| --- | --- | --- |
| Location | `<workspace>\.autofix\memory\` | Your GitHub knowledge repo |
| Scope | This project, private | Cross-project, shared with your team/AI |
| Written by | AutoFix automatically | **Only** via your explicit "Save to GitHub" click |
| Contains | conversations, fixes, errors, decisions, sessions | distilled reusable lessons/patterns/strategies |

Project memory is never published anywhere. Shared knowledge is never created automatically.

### Shared knowledge workflow

1. During a conversation, the engine detects genuinely reusable insight (root causes, proven patterns, validated lessons — `knowledge_detection.py`). Detection is deterministic and local.
2. A non-blocking notification card appears in the chat panel: category, confidence, title, body, source — with **Review**, **Save to GitHub** and **Ignore** buttons. Chat keeps working while the card is up.
3. **Review** lets you inspect/edit the text. The card shows a security notice placeholder instead of raw content if secret-like material was found.
4. **Save to GitHub** runs a security gate (`knowledge_security.py`): hard secrets (private keys, JWTs, session cookies, credentialed connection strings) BLOCK the save entirely; soft secret values are redacted before upload. The save happens off-thread; success/failure is reported honestly in the transcript.
5. **Ignore** discards the suggestion without any network activity. Detection alone never touches GitHub.

### Configuring the knowledge repository

```powershell
# backend/.env or environment
AUTOFIX_KNOWLEDGE_REPO=owner/repo          # or a full github.com URL
GITHUB_TOKEN=ghp_...                       # or AUTOFIX_KNOWLEDGE_TOKEN
# Optional:
AUTOFIX_KNOWLEDGE_API_URL=https://api.github.com
AUTOFIX_KNOWLEDGE_REF=main                 # default branch
AUTOFIX_KNOWLEDGE_DIR=ai-knowledge         # root folder inside the repo
```

Layout: `ai-knowledge/{behavior,planning,reasoning,patterns,lessons}/<slug>.md`.

### How knowledge reaches the AI

Relevant entries (keyword-filtered, fetched on demand) are injected into:

- Chat system context (after project memory)
- AutoFix planning prompts (cloud and local planner)

Every injection carries the priority rule: **project files > project configuration > project memory > shared knowledge > generic defaults.** Shared guidance can inform a plan but can never override actual project state.

## Run on Windows (manual)

```powershell
# Backend
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --reload

# Frontend (separate terminal)
cd frontend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

## Run on Windows (launcher)

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python launcher\launcher.py

# Or build a standalone .exe
.\launcher\build_launcher.ps1
# Then run: launcher\dist\launcher.exe
```

## Test

```powershell
pytest -q                        # root suite (frontend-focused)
cd backend;  pytest -q           # backend suite (own venv)
cd frontend; python -m pytest -q # frontend client suite (own venv)
```
