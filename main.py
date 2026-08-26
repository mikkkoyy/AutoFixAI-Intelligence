import uvicorn
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    uvicorn.run(
        "AIRA.api.app:app",
        host="127.0.0.1",
        port=8420,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
