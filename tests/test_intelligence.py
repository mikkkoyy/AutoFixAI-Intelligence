from AIRA.intelligence import IntelligenceStore


def test_intelligence_memory_roundtrip(tmp_path):
    store = IntelligenceStore(tmp_path / "intelligence")

    path = store.save_memory(
        "test_memory",
        {"message": "AIRA learned something"},
        category="test",
        importance=0.9,
    )

    assert path.exists()

    record = store.load_memory("test_memory")

    assert record is not None
    assert record["key"] == "test_memory"
    assert record["category"] == "test"
    assert record["content"]["message"] == "AIRA learned something"
    assert record["importance"] == 0.9


def test_knowledge_storage(tmp_path):
    store = IntelligenceStore(tmp_path / "intelligence")

    path = store.save_knowledge(
        "pytest",
        "AIRA uses pytest for verification.",
        source="test",
    )

    assert path.exists()


def test_upgrade_proposal(tmp_path):
    store = IntelligenceStore(tmp_path / "intelligence")

    path = store.save_upgrade_proposal(
        "Improve test coverage",
        "Add tests for the intelligence layer.",
        ["tests/test_intelligence.py"],
    )

    assert path.exists()

    record = path.read_text(encoding="utf-8")
    assert '"status": "proposed"' in record
