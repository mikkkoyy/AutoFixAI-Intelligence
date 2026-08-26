# AutoFix AI Studio Backend v1.0

Production-oriented backend foundation for an AI coding agent.

Workflow:
REQUEST -> PLAN -> GENERATE -> TEST -> DIAGNOSE -> FIX -> RETEST -> VERIFY -> CHECKPOINT

Included:
- FastAPI REST API
- SQLite persistence
- Job/event history
- Provider abstraction
- Offline deterministic provider
- Optional OpenAI-compatible HTTP provider
- Workspace isolation and path validation
- Safe command allowlist
- File snapshot/rollback
- Project scanner
- Test execution with timeout
- Structured diagnostics
- Retry loop
- Verification gate
- Checkpoints
- API and security tests

This is a complete backend foundation, but production deployment should still add
OS/container sandboxing and real provider credentials/configuration before allowing
untrusted code execution.

Windows quick start:
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pytest -q
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
