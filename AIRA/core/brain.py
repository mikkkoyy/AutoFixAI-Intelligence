from pathlib import Path
from AIRA.config import config
from AIRA.core.security import load_environment
from AIRA.core.logging import setup_logging, get_logger
from AIRA.core.memory_db import memory_db
from AIRA.core.ai_provider import create_provider
from AIRA.core.conversation import ConversationHandler
from AIRA.tools.tool_registry import create_default_registry
from AIRA.agents.agent_orchestrator import AgentOrchestrator
from AIRA.tools.git_sync import IntelligenceSync
from AIRA.skills.skill_manager import SkillManager
from AIRA.pc_agent.agent import PCAgent

logger = get_logger("application")


class AIRABrain:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.ai_provider = None
        self.conversation = None
        self.tools = None
        self.agents = None
        self.intelligence_sync = None
        self.skill_manager = None
        self.pc_agent = None
        self._ready = False

    async def initialize(self):
        logger.info("Initializing AIRA Brain...")

        load_environment()
        config.load()
        setup_logging(config.get("logging.level", "INFO"))

        logger.info(f"AIRA v{config.get('aira.version', '1.0.0')} starting...")

        await memory_db.initialize()

        self.ai_provider = create_provider(config)
        logger.info(f"AI Provider: {config.get('ai.provider', 'unknown')}")

        self.conversation = ConversationHandler(self.ai_provider)

        self.tools = create_default_registry()

        self.agents = AgentOrchestrator(self.ai_provider, self.tools)

        self.intelligence_sync = IntelligenceSync(
            config.get("github.intelligence_repo")
        )

        self.skill_manager = SkillManager()
        await self.skill_manager.discover_skills()

        pc_config = config.get("pc_agent", {})
        if pc_config.get("enabled", False):
            self.pc_agent = PCAgent(pc_config)
            logger.info(f"PC Agent initialized with {len(self.pc_agent.tools)} tools")

        self._ready = True
        logger.info("AIRA Brain initialized successfully")

        await memory_db.store_long_term(
            key="AIRA system startup",
            category="system",
            content=f"AIRA v{config.get('aira.version', '1.0.0')} initialized",
            importance=0.8,
            source="system",
        )

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def chat(self, message: str) -> str:
        if not self._ready:
            return "AIRA is not ready. Please wait for initialization."
        if self.pc_agent and self._is_pc_request(message):
            return await self._handle_pc_request(message)
        return await self.conversation.send_message(message)

    async def chat_stream(self, message: str):
        if not self._ready:
            yield "AIRA is not ready. Please wait for initialization."
            return
        async for chunk in self.conversation.send_message_stream(message):
            yield chunk

    def _is_pc_request(self, message: str) -> bool:
        pc_keywords = [
            "check my", "list files", "read file", "open ", "run ",
            "check git", "git status", "system info", "what's my",
            "check my pc", "check my computer", "my python version",
            "my node version", "my git version", "list running",
            "my network", "my ip", "screenshot",
        ]
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in pc_keywords)

    async def _handle_pc_request(self, message: str) -> str:
        if not self.pc_agent:
            return "PC Agent is not available. Enable it in config to use PC control features."
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in ["system info", "check my pc", "check my computer", "what's my"]):
            result = await self.pc_agent.execute_tool("system.info", {})
            return self._format_pc_result("System Information", result)
        if any(kw in msg_lower for kw in ["check git", "git status"]):
            result = await self.pc_agent.execute_tool("git.status", {})
            return self._format_pc_result("Git Status", result)
        if any(kw in msg_lower for kw in ["list files", "check my"]):
            path_match = None
            for word in message.split():
                if len(word) > 2 and (":" in word or "/" in word or "\\" in word):
                    path_match = word.rstrip(".,")
                    break
            if not path_match:
                path_match = "."
            result = await self.pc_agent.execute_tool("filesystem.list_directory", {"path": path_match})
            return self._format_pc_result("Directory Listing", result)
        if "screenshot" in msg_lower:
            result = await self.pc_agent.execute_tool("screenshot.capture", {})
            return self._format_pc_result("Screenshot", result)
        if "network" in msg_lower or "my ip" in msg_lower:
            result = await self.pc_agent.execute_tool("network.info", {})
            return self._format_pc_result("Network Information", result)
        if any(kw in msg_lower for kw in ["list running", "open "]):
            if "open" in msg_lower:
                app_name = message.split("open")[-1].strip().rstrip(".,")
                result = await self.pc_agent.execute_tool("application.open", {"application": app_name})
            else:
                result = await self.pc_agent.execute_tool("application.list_running", {})
            return self._format_pc_result("Applications", result)
        return await self.conversation.send_message(message)

    def _format_pc_result(self, title: str, result: dict) -> str:
        if not result.get("success"):
            return f"**PC Agent — {title}**\n\nError: {result.get('error', 'Unknown error')}"
        import json
        data = result.get("result", result)
        if isinstance(data, (dict, list)):
            data_str = json.dumps(data, indent=2, default=str)
        else:
            data_str = str(data)
        if len(data_str) > 2000:
            data_str = data_str[:2000] + "\n... (truncated)"
        return f"**PC Agent — {title}**\n\n```\n{data_str}\n```"

    async def get_status(self) -> dict:
        stats = await memory_db.get_stats()
        ai_available = False
        try:
            ai_available = await self.ai_provider.is_available()
        except Exception:
            pass

        return {
            "name": config.get("aira.name", "AIRA"),
            "version": config.get("aira.version", "1.0.0"),
            "ready": self._ready,
            "ai_provider": config.get("ai.provider", "unknown"),
            "ai_model": config.get("ai.model", "unknown"),
            "ai_available": ai_available,
            "memory_stats": stats,
            "skills": self.skill_manager.get_skill_names() if self.skill_manager else [],
            "tools": [t["name"] for t in self.tools.list_tools()] if self.tools else [],
            "agents": [a["name"] for a in self.agents.list_agents()] if self.agents else [],
            "pc_agent": {
                "available": self.pc_agent is not None,
                "tools": [t["name"] for t in self.pc_agent.list_tools()] if self.pc_agent else [],
            },
        }

    async def shutdown(self):
        logger.info("AIRA shutting down...")
        await memory_db.close()
        self._ready = False


brain = AIRABrain()
