import uvicorn
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    print("=" * 50)
    print("  AIRA PC Agent — Local PC Control")
    print("  Binding: http://127.0.0.1:8765")
    print("  Mode: SAFE (default)")
    print("=" * 50)
    uvicorn.run(
        "AIRA.pc_agent.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
