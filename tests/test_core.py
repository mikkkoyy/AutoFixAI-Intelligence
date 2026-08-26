import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AIRA.config import config
from AIRA.core.memory_db import memory_db
from AIRA.core.models import generate_id, timestamp_now, PermissionLevel, KnowledgeState
from AIRA.tools.tool_registry import create_default_registry, ToolResult


def test_config():
    config.load()
    assert config.get("aira.name") == "AIRA"
    assert config.get("aira.version") == "1.0.0"
    assert config.get("ai.provider") is not None
    assert config.get("memory.enabled") is True
    print("PASS: config")


def test_models():
    id1 = generate_id()
    id2 = generate_id()
    assert len(id1) == 32
    assert id1 != id2

    ts = timestamp_now()
    assert "T" in ts or "Z" in ts

    assert PermissionLevel.READ.value == "read"
    assert KnowledgeState.VERIFIED.value == "verified"
    print("PASS: models")


async def test_memory_db():
    await memory_db.initialize()

    conv = await memory_db.create_conversation("Test Chat")
    assert conv["id"]
    assert conv["title"] == "Test Chat"

    msg = await memory_db.add_message(conv["id"], "user", "Hello AIRA")
    assert msg["id"]
    assert msg["role"] == "user"

    msgs = await memory_db.get_conversation_messages(conv["id"])
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello AIRA"

    convs = await memory_db.get_conversations()
    assert len(convs) >= 1

    lt = await memory_db.store_long_term(
        key="test fact", category="test", content="This is a test fact",
        importance=0.8,
    )
    assert lt["id"]

    results = await memory_db.search_long_term(query="test fact")
    assert len(results) >= 1

    k = await memory_db.store_knowledge(
        title="Test Solution", category="testing", solution="Do X to fix Y",
        tags=["test"], confidence=0.9,
    )
    assert k["id"]

    kresults = await memory_db.search_knowledge(query="Test Solution")
    assert len(kresults) >= 1

    err = await memory_db.record_error("test", "TestError", "something failed")
    assert err["id"]

    stats = await memory_db.get_stats()
    assert stats["conversations"] >= 1
    assert stats["messages"] >= 1
    assert stats["knowledge"] >= 1

    await memory_db.close()
    print("PASS: memory_db")


def test_tools():
    registry = create_default_registry()
    tools = registry.list_tools()
    assert len(tools) >= 6

    names = [t["name"] for t in tools]
    assert "file_read" in names
    assert "file_write" in names
    assert "terminal" in names
    assert "git" in names
    assert "memory" in names

    read_tool = registry.get("file_read")
    assert read_tool is not None
    assert read_tool.permission == PermissionLevel.READ

    write_tool = registry.get("file_write")
    assert write_tool.validate(path="test.txt") is True
    assert write_tool.validate(path=".env") is False
    assert write_tool.validate(path="config/secrets.yaml") is False

    term_tool = registry.get("terminal")
    assert term_tool.validate(command="ls") is True
    assert term_tool.validate(command="rm -rf /") is False
    assert term_tool.validate(command="format c:") is False

    print("PASS: tools")


def test_safety():
    from AIRA.agents.agent_orchestrator import SafetyAgent
    agent = SafetyAgent(None, None)
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(agent.execute("read file test.txt"))
    assert result["success"] is True

    result = loop.run_until_complete(agent.execute("rm -rf /"))
    assert result["success"] is False

    result = loop.run_until_complete(agent.execute("format c:"))
    assert result["success"] is False
    loop.close()
    print("PASS: safety")


async def run_async_tests():
    await test_memory_db()


def main():
    test_config()
    test_models()
    test_tools()
    test_safety()
    asyncio.run(run_async_tests())
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()
