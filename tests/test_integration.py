import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AIRA.core.memory_db import memory_db


async def test_concurrent_access():
    await memory_db.initialize()

    tasks = []
    for i in range(10):
        tasks.append(memory_db.create_conversation(f"Concurrent {i}"))
    results = await asyncio.gather(*tasks)
    assert len(results) == 10

    ids = [r["id"] for r in results]
    assert len(set(ids)) == 10

    await memory_db.close()
    print("PASS: concurrent_access")


async def test_large_data():
    await memory_db.initialize()

    for i in range(50):
        await memory_db.store_long_term(
            key=f"key_{i}", category="test_bulk",
            content="x" * 1000, importance=float(i) / 50,
        )

    results = await memory_db.search_long_term(category="test_bulk", limit=10)
    assert len(results) == 10
    assert results[0]["importance"] >= results[-1]["importance"]

    await memory_db.close()
    print("PASS: large_data")


async def main():
    await test_concurrent_access()
    await test_large_data()
    print("\n=== ALL INTEGRATION TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
