# AutoFix AI Studio - Frontend

PySide6 desktop frontend for the existing AutoFix AI Studio FastAPI backend.

Default backend:
http://127.0.0.1:8000

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

## Test

```powershell
python -m pytest -q
```

The frontend uses the current backend endpoints:

- GET /health
- GET /api/v1/status
- POST /api/v1/jobs
- GET /api/v1/jobs/{job_id}
- GET /api/v1/jobs/{job_id}/events
