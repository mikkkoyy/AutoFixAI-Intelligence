import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from AIRA.pc_agent.agent import PCAgent
from AIRA.pc_agent.security import load_or_create_token, verify_token
from AIRA.pc_agent.audit import audit_logger
from AIRA.core.logging import get_logger
from AIRA.config import config as aira_config

logger = get_logger("pc_agent")

_agent: Optional[PCAgent] = None
_auth_token: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _auth_token
    aira_config.load()
    pc_config = aira_config.get("pc_agent", {})
    _agent = PCAgent(pc_config)
    _auth_token = load_or_create_token()
    audit_logger.log_event("AGENT_START", tool="system", result={"success": True, "message": "PC Agent started"})
    logger.info(f"PC Agent started with {len(_agent.tools)} tools")
    yield
    audit_logger.log_event("AGENT_STOP", tool="system")
    logger.info("PC Agent stopped")


app = FastAPI(
    title="AIRA PC Agent",
    version="1.0.0",
    lifespan=lifespan,
)


async def verify_auth(request: Request):
    global _auth_token
    if not _auth_token:
        return True
    auth_header = request.headers.get("Authorization", "")
    x_token = request.headers.get("X-PC-Agent-Token", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif x_token:
        token = x_token
    else:
        token = request.query_params.get("token", "")
    if not verify_token(token, _auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing token")
    return True


class ToolRequest(BaseModel):
    tool: str
    arguments: dict = {}
    auto_approve: bool = False


class ApprovalRequest(BaseModel):
    approval_id: str
    approve: bool


@app.get("/health")
async def health():
    return {
        "status": "online",
        "agent": "AIRA PC Agent",
        "version": "1.0.0",
        "platform": "Windows",
    }


@app.get("/tools")
async def list_tools(authorized: bool = Depends(verify_auth)):
    if not _agent:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    return {"tools": _agent.list_tools()}


@app.get("/tools/enabled")
async def list_enabled_tools(authorized: bool = Depends(verify_auth)):
    if not _agent:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    return {"tools": _agent.get_enabled_tools()}


@app.post("/execute")
async def execute_tool(req: ToolRequest, authorized: bool = Depends(verify_auth)):
    if not _agent:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    result = await _agent.execute_tool(req.tool, req.arguments, auto_approve=req.auto_approve)
    return result


@app.get("/status")
async def agent_status(authorized: bool = Depends(verify_auth)):
    if not _agent:
        return {"status": "offline"}
    return _agent.get_status()


@app.post("/approval/respond")
async def respond_approval(req: ApprovalRequest, authorized: bool = Depends(verify_auth)):
    if not _agent:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    if req.approve:
        return _agent.approve_operation(req.approval_id)
    return _agent.deny_operation(req.approval_id)


@app.get("/approval/pending")
async def pending_approvals(authorized: bool = Depends(verify_auth)):
    if not _agent:
        return {"approvals": []}
    pending = [
        {"id": k, **v}
        for k, v in _agent.approval_queue.items()
        if v["status"] == "pending"
    ]
    return {"approvals": pending}


@app.post("/permission/mode")
async def set_permission_mode(req: dict, authorized: bool = Depends(verify_auth)):
    if not _agent:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    mode = req.get("mode", "")
    from AIRA.pc_agent.permissions import AutonomyMode
    try:
        new_mode = AutonomyMode(mode)
    except ValueError:
        return {"success": False, "error": f"Invalid mode: {mode}. Valid: {[m.value for m in AutonomyMode]}"}
    _agent.permissions.mode = new_mode
    audit_logger.log_event("MODE_CHANGE", tool="permissions", result={"mode": mode})
    return {"success": True, "mode": mode}
