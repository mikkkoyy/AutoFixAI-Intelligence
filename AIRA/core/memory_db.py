import aiosqlite
import json
from pathlib import Path
from typing import Optional
from AIRA.core.models import generate_id, timestamp_now
from AIRA.core.logging import get_logger

logger = get_logger("memory")

DB_PATH = Path(__file__).parent.parent / "memory" / "aira_memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary TEXT,
    message_count INTEGER DEFAULT 0,
    importance REAL DEFAULT 0.5,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    tokens INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS long_term_memory (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    importance REAL DEFAULT 0.5,
    confidence REAL DEFAULT 0.5,
    source TEXT,
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    access_count INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    problem TEXT,
    solution TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.5,
    state TEXT DEFAULT 'unverified',
    source TEXT,
    version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS errors (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    context TEXT,
    stack_trace TEXT,
    solution_id TEXT,
    resolved INTEGER DEFAULT 0,
    occurrence_count INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (solution_id) REFERENCES knowledge(id)
);

CREATE TABLE IF NOT EXISTS improvements (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    problem TEXT NOT NULL,
    analysis TEXT,
    solution TEXT NOT NULL,
    result TEXT DEFAULT 'pending',
    confidence REAL DEFAULT 0.0,
    source TEXT,
    version TEXT,
    auto_applied INTEGER DEFAULT 0,
    approved INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS user_preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    version TEXT DEFAULT '1.0.0',
    instructions TEXT,
    tools TEXT DEFAULT '[]',
    requirements TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sync_log (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    commit_hash TEXT,
    message TEXT,
    timestamp TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_long_term_category ON long_term_memory(category);
CREATE INDEX IF NOT EXISTS idx_long_term_key ON long_term_memory(key);
CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_state ON knowledge(state);
CREATE INDEX IF NOT EXISTS idx_errors_category ON errors(category);
CREATE INDEX IF NOT EXISTS idx_errors_resolved ON errors(resolved);
"""


class MemoryDB:
    _instance = None
    _db: Optional[aiosqlite.Connection] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def initialize(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(DB_PATH))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Memory database initialized")

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("MemoryDB not initialized")
        return self._db

    async def execute(self, query: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self.db.execute(query, params)
        await self.db.commit()
        return cursor

    async def fetch_one(self, query: str, params: tuple = ()) -> Optional[dict]:
        cursor = await self.db.execute(query, params)
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None

    async def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def create_conversation(self, title: str = None) -> dict:
        conv_id = generate_id()
        now = timestamp_now()
        await self.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, title or "New Conversation", now, now),
        )
        return {"id": conv_id, "title": title or "New Conversation", "created_at": now}

    async def add_message(self, conversation_id: str, role: str, content: str, tokens: int = 0) -> dict:
        msg_id = generate_id()
        now = timestamp_now()
        await self.execute(
            "INSERT INTO messages (id, conversation_id, role, content, timestamp, tokens) VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, conversation_id, role, content, now, tokens),
        )
        await self.execute(
            "UPDATE conversations SET updated_at = ?, message_count = message_count + 1 WHERE id = ?",
            (now, conversation_id),
        )
        return {"id": msg_id, "role": role, "content": content, "timestamp": now}

    async def get_conversation_messages(self, conversation_id: str, limit: int = 50) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC LIMIT ?",
            (conversation_id, limit),
        )

    async def get_conversations(self, limit: int = 20) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
        )

    async def update_conversation_summary(self, conversation_id: str, summary: str):
        now = timestamp_now()
        await self.execute(
            "UPDATE conversations SET summary = ?, updated_at = ? WHERE id = ?",
            (summary, now, conversation_id),
        )

    async def store_long_term(self, key: str, category: str, content: str,
                              summary: str = None, importance: float = 0.5,
                              confidence: float = 0.5, source: str = None,
                              tags: list[str] = None) -> dict:
        mem_id = generate_id()
        now = timestamp_now()
        await self.execute(
            """INSERT INTO long_term_memory (id, key, category, content, summary, importance, confidence, source, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, key, category, content, summary, importance, confidence,
             source, json.dumps(tags or []), now, now),
        )
        return {"id": mem_id, "key": key, "category": category}

    async def search_long_term(self, query: str = None, category: str = None,
                               min_importance: float = 0.0, limit: int = 20) -> list[dict]:
        conditions = ["importance >= ?"]
        params: list = [min_importance]
        if category:
            conditions.append("category = ?")
            params.append(category)
        if query:
            conditions.append("(key LIKE ? OR content LIKE ? OR summary LIKE ?)")
            q = f"%{query}%"
            params.extend([q, q, q])
        where = " AND ".join(conditions)
        params.append(limit)
        return await self.fetch_all(
            f"SELECT * FROM long_term_memory WHERE {where} ORDER BY importance DESC, updated_at DESC LIMIT ?",
            tuple(params),
        )

    async def store_knowledge(self, title: str, category: str, solution: str,
                              problem: str = None, tags: list[str] = None,
                              confidence: float = 0.5, source: str = None) -> dict:
        kid = generate_id()
        now = timestamp_now()
        await self.execute(
            """INSERT INTO knowledge (id, title, category, problem, solution, tags, confidence, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kid, title, category, problem, solution, json.dumps(tags or []),
             confidence, source, now, now),
        )
        return {"id": kid, "title": title, "category": category}

    async def search_knowledge(self, query: str = None, category: str = None,
                               state: str = None, tags: list[str] = None,
                               limit: int = 20) -> list[dict]:
        conditions = []
        params: list = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if state:
            conditions.append("state = ?")
            params.append(state)
        if query:
            conditions.append("(title LIKE ? OR problem LIKE ? OR solution LIKE ?)")
            q = f"%{query}%"
            params.extend([q, q, q])
        if tags:
            for tag in tags:
                conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        return await self.fetch_all(
            f"SELECT * FROM knowledge WHERE {where} ORDER BY confidence DESC LIMIT ?",
            tuple(params),
        )

    async def record_error(self, category: str, error_type: str, message: str,
                           context: str = None, stack_trace: str = None) -> dict:
        now = timestamp_now()
        existing = await self.fetch_one(
            "SELECT * FROM errors WHERE error_type = ? AND message = ?",
            (error_type, message),
        )
        if existing:
            await self.execute(
                "UPDATE errors SET occurrence_count = occurrence_count + 1, last_seen = ? WHERE id = ?",
                (now, existing["id"]),
            )
            return {"id": existing["id"], "occurrence_count": existing["occurrence_count"] + 1}
        eid = generate_id()
        await self.execute(
            """INSERT INTO errors (id, category, error_type, message, context, stack_trace, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, category, error_type, message, context, stack_trace, now, now),
        )
        return {"id": eid, "occurrence_count": 1}

    async def store_improvement(self, problem: str, analysis: str, solution: str,
                                result: str = "pending", confidence: float = 0.0,
                                source: str = None) -> dict:
        imp_id = generate_id()
        now = timestamp_now()
        from AIRA import VERSION
        await self.execute(
            """INSERT INTO improvements (id, timestamp, problem, analysis, solution, result, confidence, source, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (imp_id, now, problem, analysis, solution, result, confidence, source, VERSION),
        )
        return {"id": imp_id, "problem": problem, "result": result}

    async def get_stats(self) -> dict:
        stats = {}
        for table in ["conversations", "messages", "long_term_memory", "knowledge", "errors", "improvements", "skills"]:
            row = await self.fetch_one(f"SELECT COUNT(*) as count FROM {table}")
            stats[table] = row["count"] if row else 0
        verified = await self.fetch_one(
            "SELECT COUNT(*) as count FROM knowledge WHERE state = 'verified'"
        )
        stats["verified_knowledge"] = verified["count"] if verified else 0
        unresolved = await self.fetch_one(
            "SELECT COUNT(*) as count FROM errors WHERE resolved = 0"
        )
        stats["unresolved_errors"] = unresolved["count"] if unresolved else 0
        return stats

    async def record_sync(self, direction: str, status: str, commit_hash: str = None,
                          message: str = None) -> dict:
        sid = generate_id()
        now = timestamp_now()
        await self.execute(
            """INSERT INTO sync_log (id, direction, status, commit_hash, message, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, direction, status, commit_hash, message, now),
        )
        return {"id": sid, "direction": direction, "status": status}


memory_db = MemoryDB()
