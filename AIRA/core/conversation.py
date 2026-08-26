from typing import Optional
from AIRA.core.memory_db import memory_db
from AIRA.core.logging import get_logger

logger = get_logger("memory")

AIRA_SYSTEM_PROMPT = """You are AIRA — Autonomous Intelligent Reasoning Assistant.

You are an intelligent, helpful, and friendly AI assistant. Your characteristics:
- Intelligent and technically capable
- Helpful and friendly in conversation
- Technical when necessary, conversational when appropriate
- Concise for simple questions, detailed for technical problems
- Honest about uncertainty — never claim something was completed if it was not
- Capable of explaining what you are doing
- Always refer to yourself as AIRA

You have access to memory, knowledge, and tools. You can:
- Remember previous conversations
- Access long-term knowledge
- Learn from successful interactions
- Analyze and improve from errors
- Use tools to interact with the filesystem, git, and system

When you solve a problem successfully, that knowledge may be stored for future reference.
When you encounter an error, analyze it and suggest improvements.

Be concise but thorough. Always be truthful about your capabilities and limitations."""


class ConversationHandler:
    def __init__(self, ai_provider):
        self.ai = ai_provider
        self.current_conversation_id: Optional[str] = None
        self.short_term_messages: list[dict] = []
        self.system_prompt = AIRA_SYSTEM_PROMPT

    async def start_conversation(self, title: str = None) -> str:
        conv = await memory_db.create_conversation(title)
        self.current_conversation_id = conv["id"]
        self.short_term_messages = []
        logger.info(f"Started conversation: {self.current_conversation_id}")
        return self.current_conversation_id

    async def send_message(self, content: str) -> str:
        if not self.current_conversation_id:
            await self.start_conversation()

        await memory_db.add_message(self.current_conversation_id, "user", content)
        self.short_term_messages.append({"role": "user", "content": content})

        context = await self._build_context()
        messages = [{"role": "system", "content": context}] + self.short_term_messages[-30:]

        try:
            response = await self.ai.chat(messages)
        except Exception as e:
            logger.error(f"AI response error: {e}")
            response = f"I encountered an error while processing your request: {e}. Please try again."

        await memory_db.add_message(self.current_conversation_id, "assistant", response)
        self.short_term_messages.append({"role": "assistant", "content": response})

        await self._evaluate_memory(content, response)

        return response

    async def send_message_stream(self, content: str):
        if not self.current_conversation_id:
            await self.start_conversation()

        await memory_db.add_message(self.current_conversation_id, "user", content)
        self.short_term_messages.append({"role": "user", "content": content})

        context = await self._build_context()
        messages = [{"role": "system", "content": context}] + self.short_term_messages[-30:]

        full_response = ""
        try:
            async for chunk in self.ai.chat_stream(messages):
                full_response += chunk
                yield chunk
        except Exception as e:
            logger.error(f"AI stream error: {e}")
            error_msg = f"I encountered an error: {e}"
            full_response = error_msg
            yield error_msg

        await memory_db.add_message(self.current_conversation_id, "assistant", full_response)
        self.short_term_messages.append({"role": "assistant", "content": full_response})

        await self._evaluate_memory(content, full_response)

    async def _build_context(self) -> str:
        parts = [self.system_prompt]

        if self.current_conversation_id:
            history = await memory_db.get_conversation_messages(self.current_conversation_id, limit=5)
            if history:
                parts.append("\n--- Recent conversation history ---")
                for msg in history[-5:]:
                    parts.append(f"{msg['role']}: {msg['content'][:200]}")

        relevant = await memory_db.search_long_term(query=None, limit=5)
        if relevant:
            parts.append("\n--- Long-term knowledge context ---")
            for mem in relevant[:3]:
                parts.append(f"[{mem['category']}] {mem.get('summary', mem['content'][:200])}")

        knowledge = await memory_db.search_knowledge(state="verified", limit=3)
        if knowledge:
            parts.append("\n--- Verified knowledge ---")
            for k in knowledge:
                parts.append(f"[{k['category']}] {k['title']}: {k['solution'][:200]}")

        return "\n".join(parts)

    async def _evaluate_memory(self, user_msg: str, ai_response: str):
        importance_indicators = [
            "remember", "important", "always", "preference", "config",
            "setting", "password", "key", "rule", "pattern", "solution",
            "learn", "know", "fact", "definition", "explain"
        ]
        msg_lower = user_msg.lower()
        importance = 0.3
        for indicator in importance_indicators:
            if indicator in msg_lower:
                importance += 0.1
        importance = min(importance, 1.0)

        if importance >= 0.6:
            summary = f"User asked: {user_msg[:100]}. AIRA responded with key information."
            await memory_db.store_long_term(
                key=user_msg[:100],
                category="conversation",
                content=f"Q: {user_msg}\nA: {ai_response}",
                summary=summary,
                importance=importance,
                source="conversation",
            )
            logger.info(f"Stored long-term memory (importance={importance:.1f})")

    async def get_history(self) -> list[dict]:
        if not self.current_conversation_id:
            return []
        return await memory_db.get_conversation_messages(self.current_conversation_id)

    async def list_conversations(self) -> list[dict]:
        return await memory_db.get_conversations()

    async def load_conversation(self, conversation_id: str):
        self.current_conversation_id = conversation_id
        messages = await memory_db.get_conversation_messages(conversation_id)
        self.short_term_messages = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
