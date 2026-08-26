from abc import ABC, abstractmethod
from typing import Optional
from AIRA.tools.tool_registry import ToolRegistry, ToolResult
from AIRA.core.logging import get_logger

logger = get_logger("tools")


class BaseAgent(ABC):
    name: str = "base"
    description: str = ""

    def __init__(self, ai_provider, tool_registry: ToolRegistry):
        self.ai = ai_provider
        self.tools = tool_registry

    @abstractmethod
    async def execute(self, task: str, context: dict = None) -> dict:
        pass

    def _format_result(self, success: bool, output: str = None, error: str = None) -> dict:
        return {"success": success, "output": output, "error": error}


class ConversationAgent(BaseAgent):
    name = "conversation"
    description = "Handles natural language conversation"

    async def execute(self, task: str, context: dict = None) -> dict:
        try:
            response = await self.ai.chat([{"role": "user", "content": task}])
            return self._format_result(True, response)
        except Exception as e:
            return self._format_result(False, error=str(e))


class KnowledgeAgent(BaseAgent):
    name = "knowledge"
    description = "Manages knowledge storage and retrieval"

    async def execute(self, task: str, context: dict = None) -> dict:
        from AIRA.core.memory_db import memory_db
        try:
            results = await memory_db.search_knowledge(query=task, limit=5)
            if results:
                knowledge_text = "\n".join(
                    f"- {r['title']}: {r['solution'][:200]}" for r in results
                )
                return self._format_result(True, knowledge_text)
            return self._format_result(True, "No relevant knowledge found.")
        except Exception as e:
            return self._format_result(False, error=str(e))


class DebuggingAgent(BaseAgent):
    name = "debugging"
    description = "Analyzes and helps fix errors"

    async def execute(self, task: str, context: dict = None) -> dict:
        from AIRA.core.memory_db import memory_db
        try:
            error_record = await memory_db.record_error(
                category=context.get("category", "general") if context else "general",
                error_type=context.get("error_type", "unknown") if context else "unknown",
                message=task,
                context=str(context) if context else None,
            )

            similar = await memory_db.search_knowledge(query=task, limit=3)
            prompt = f"Debug this error: {task}"
            if similar:
                prompt += f"\n\nSimilar known solutions:\n"
                for s in similar:
                    prompt += f"- {s['title']}: {s['solution'][:200]}\n"

            response = await self.ai.chat([{"role": "user", "content": prompt}])
            return self._format_result(True, response)
        except Exception as e:
            return self._format_result(False, error=str(e))


class ImprovementAgent(BaseAgent):
    name = "improvement"
    description = "Identifies and proposes improvements"

    async def execute(self, task: str, context: dict = None) -> dict:
        from AIRA.core.memory_db import memory_db
        try:
            stats = await memory_db.get_stats()
            errors = await memory_db.fetch_all(
                "SELECT * FROM errors WHERE resolved = 0 ORDER BY occurrence_count DESC LIMIT 5"
            ) if hasattr(memory_db, 'fetch_all') else []

            prompt = f"Analyze this improvement request: {task}\n\nSystem stats: {stats}"
            if errors:
                prompt += f"\n\nUnresolved errors: {len(errors)}"

            analysis = await self.ai.chat([{"role": "user", "content": prompt}])
            improvement = await memory_db.store_improvement(
                problem=task,
                analysis=analysis,
                solution=analysis,
                result="proposed",
                source="improvement_agent",
            )
            return self._format_result(True, f"Improvement proposed: {improvement['id']}")
        except Exception as e:
            return self._format_result(False, error=str(e))


class SafetyAgent(BaseAgent):
    name = "safety"
    description = "Validates operations for safety"

    DANGEROUS_PATTERNS = [
        "rm -rf", "format c:", "del /s", "rmdir /s",
        "drop table", "delete from", "truncate",
        "sudo", "chmod 777", "eval(", "exec(",
    ]

    async def execute(self, task: str, context: dict = None) -> dict:
        task_lower = task.lower()
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in task_lower:
                return self._format_result(
                    False,
                    error=f"Dangerous operation detected: contains '{pattern}'. Blocked for safety.",
                )
        return self._format_result(True, "Operation appears safe.")


class AgentOrchestrator:
    def __init__(self, ai_provider, tool_registry: ToolRegistry):
        self.agents: dict[str, BaseAgent] = {}
        self.ai = ai_provider
        self.tools = tool_registry
        self._register_defaults()

    def _register_defaults(self):
        agent_classes = [
            ConversationAgent,
            KnowledgeAgent,
            DebuggingAgent,
            ImprovementAgent,
            SafetyAgent,
        ]
        for cls in agent_classes:
            agent = cls(self.ai, self.tools)
            self.agents[agent.name] = agent
            logger.info(f"Registered agent: {agent.name}")

    def register(self, agent: BaseAgent):
        self.agents[agent.name] = agent

    def get(self, name: str) -> Optional[BaseAgent]:
        return self.agents.get(name)

    async def route(self, task: str, agent_name: str = None, context: dict = None) -> dict:
        if agent_name and agent_name in self.agents:
            agent = self.agents[agent_name]
        else:
            agent = self.agents.get("conversation")

        if not agent:
            return {"success": False, "error": "No agent available"}

        return await agent.execute(task, context)

    def list_agents(self) -> list[dict]:
        return [
            {"name": a.name, "description": a.description}
            for a in self.agents.values()
        ]
