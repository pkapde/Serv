"""Run MCP locally without importing the organisation backend's Azure settings.

Prepare sample data first, then run:
    python -m uvicorn main_mcp_demo:app --host 127.0.0.1 --port 8000
"""

import json
from pathlib import Path

from fastapi import FastAPI

from semantic_mcp.demo_ui import install_demo_ui
from semantic_mcp.integration import IntegrationSettings, install_mcp

app = FastAPI(title="Local MCP integration test")

install_demo_ui(app, list(json.loads(Path("examples/questions.json").read_text(encoding="utf-8"))))

install_mcp(
    app,
    IntegrationSettings(
        _env_file=None,
        resolver_mode="fixture",
        execution_mode="demo",
        database_url="sqlite:///var/mcp-service.db",
    ),
)
