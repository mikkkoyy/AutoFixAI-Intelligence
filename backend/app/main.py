from fastapi import FastAPI
from app.api.routes import router
from app.db import init_db

init_db()

app = FastAPI(
    title="AutoFix AI Studio Backend",
    version="1.0.0",
    description="AI coding workflow backend with verification gates.",
)
app.include_router(router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "autofix-ai-studio-backend", "version": "1.0.0"}
