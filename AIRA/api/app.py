from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
from contextlib import asynccontextmanager

from AIRA.core.brain import brain
from AIRA.pc_agent.security import load_or_create_token
from AIRA.pc_agent.audit import audit_logger as pc_audit_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    await brain.initialize()
    yield
    await brain.shutdown()


app = FastAPI(
    title="AIRA - Autonomous Intelligent Reasoning Assistant",
    version="1.0.0",
    lifespan=lifespan,
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class ConversationRequest(BaseModel):
    title: Optional[str] = None


class KnowledgeRequest(BaseModel):
    title: str
    category: str = "general"
    problem: Optional[str] = None
    solution: str
    tags: list[str] = []


class MemoryRequest(BaseModel):
    key: str
    category: str = "general"
    content: str
    importance: float = 0.5


class PCToolRequest(BaseModel):
    tool: str
    arguments: dict = {}
    auto_approve: bool = False


class PCApprovalRequest(BaseModel):
    approval_id: str
    approve: bool


class PCPermissionRequest(BaseModel):
    mode: str


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
async def get_status():
    return await brain.get_status()


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not brain.is_ready:
        return JSONResponse({"error": "AIRA is initializing"}, status_code=503)

    if req.conversation_id and req.conversation_id != brain.conversation.current_conversation_id:
        await brain.conversation.load_conversation(req.conversation_id)

    response = await brain.chat(req.message)
    return {
        "response": response,
        "conversation_id": brain.conversation.current_conversation_id,
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    if not brain.is_ready:
        return JSONResponse({"error": "AIRA is initializing"}, status_code=503)

    if req.conversation_id and req.conversation_id != brain.conversation.current_conversation_id:
        await brain.conversation.load_conversation(req.conversation_id)

    async def event_generator():
        async for chunk in brain.chat_stream(req.message):
            yield f"data: {chunk}\n\n"
        yield f"data: [DONE]\n\n"
        yield f"data: {brain.conversation.current_conversation_id}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/conversations")
async def create_conversation(req: ConversationRequest):
    conv_id = await brain.conversation.start_conversation(req.title)
    return {"conversation_id": conv_id}


@app.get("/api/conversations")
async def list_conversations():
    convs = await brain.conversation.list_conversations()
    return {"conversations": convs}


@app.get("/api/conversations/{conv_id}/messages")
async def get_messages(conv_id: str):
    await brain.conversation.load_conversation(conv_id)
    messages = await brain.conversation.get_history()
    return {"messages": messages}


@app.post("/api/memory/store")
async def store_memory(req: MemoryRequest):
    result = await brain.tools.get("memory").execute(
        action="store",
        key=req.key,
        category=req.category,
        content=req.content,
        importance=req.importance,
    )
    return result.to_dict()


@app.get("/api/memory/search")
async def search_memory(q: str = "", category: str = "", limit: int = 10):
    results = await brain.tools.get("memory").execute(
        action="search", query=q or None, category=category or None, limit=limit,
    )
    return results.to_dict()


@app.get("/api/memory/stats")
async def memory_stats():
    stats = await brain.tools.get("memory").execute(action="stats")
    return stats.to_dict()


@app.post("/api/knowledge/store")
async def store_knowledge(req: KnowledgeRequest):
    result = await brain.tools.get("memory").execute(
        action="store_knowledge",
        title=req.title,
        category=req.category,
        solution=req.solution,
        problem=req.problem,
        tags=req.tags,
    )
    return result.to_dict()


@app.get("/api/knowledge/search")
async def search_knowledge(q: str = "", category: str = "", limit: int = 10):
    results = await brain.tools.get("memory").execute(
        action="search_knowledge", query=q or None, category=category or None, limit=limit,
    )
    return results.to_dict()


@app.get("/api/tools")
async def list_tools():
    return {"tools": brain.tools.list_tools()}


@app.get("/api/agents")
async def list_agents():
    return {"agents": brain.agents.list_agents()}


@app.get("/api/skills")
async def list_skills():
    return {"skills": brain.skill_manager.list_skills()}


@app.post("/api/improvement/analyze")
async def analyze_improvement(req: ChatRequest):
    result = await brain.agents.route(req.task, agent_name="improvement")
    return result


@app.post("/api/intelligence/sync")
async def sync_intelligence():
    result = await brain.intelligence_sync.sync_knowledge_to_repo()
    return result


@app.get("/api/intelligence/status")
async def intelligence_status():
    return await brain.intelligence_sync.get_repo_status()


@app.get("/api/pc/status")
async def pc_agent_status():
    if not brain.pc_agent:
        return {"available": False, "status": "offline"}
    return {"available": True, **brain.pc_agent.get_status()}


@app.get("/api/pc/tools")
async def pc_agent_tools():
    if not brain.pc_agent:
        return {"tools": []}
    return {"tools": brain.pc_agent.list_tools()}


@app.get("/api/pc/tools/enabled")
async def pc_agent_enabled_tools():
    if not brain.pc_agent:
        return {"tools": []}
    return {"tools": brain.pc_agent.get_enabled_tools()}


@app.post("/api/pc/execute")
async def pc_agent_execute(req: PCToolRequest):
    if not brain.pc_agent:
        return JSONResponse({"error": "PC Agent not available"}, status_code=503)
    result = await brain.pc_agent.execute_tool(req.tool, req.arguments, auto_approve=req.auto_approve)
    return result


@app.get("/api/pc/approval/pending")
async def pc_agent_pending_approvals():
    if not brain.pc_agent:
        return {"approvals": []}
    pending = [
        {"id": k, **v}
        for k, v in brain.pc_agent.approval_queue.items()
        if v["status"] == "pending"
    ]
    return {"approvals": pending}


@app.post("/api/pc/approval/respond")
async def pc_agent_respond_approval(req: PCApprovalRequest):
    if not brain.pc_agent:
        return JSONResponse({"error": "PC Agent not available"}, status_code=503)
    if req.approve:
        return brain.pc_agent.approve_operation(req.approval_id)
    return brain.pc_agent.deny_operation(req.approval_id)


@app.post("/api/pc/permission/mode")
async def pc_agent_set_mode(req: PCPermissionRequest):
    if not brain.pc_agent:
        return JSONResponse({"error": "PC Agent not available"}, status_code=503)
    from AIRA.pc_agent.permissions import AutonomyMode
    try:
        new_mode = AutonomyMode(req.mode)
    except ValueError:
        return {"success": False, "error": f"Invalid mode: {req.mode}"}
    brain.pc_agent.permissions.mode = new_mode
    pc_audit_logger.log_event("MODE_CHANGE", tool="permissions", result={"mode": req.mode})
    return {"success": True, "mode": req.mode}
