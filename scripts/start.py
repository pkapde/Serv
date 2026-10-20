"""Start a loopback local service or the container service."""

import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic_mcp.config import Settings

if __name__ == "__main__":
    settings = Settings()
    if settings.execution_mode == "demo" and not settings.demo_database_path.exists():
        from scripts.seed_demo import seed

        seed(settings.demo_database_path)
    uvicorn.run(
        "semantic_mcp.app:create_app",
        factory=True,
        host=os.getenv("BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        log_level="warning",
    )
