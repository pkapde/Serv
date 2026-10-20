"""Standalone real-LLM test with local DuckDB; no tuning-fork dependencies.

Configure MCP_LLM_BASE_URL, MCP_LLM_API_KEY and MCP_LLM_MODEL, then run:
    python -m uvicorn main_mcp_llm:create_app --factory --host 127.0.0.1 --port 8000
"""

import json
from pathlib import Path

from fastapi import FastAPI

from semantic_mcp.config import Settings
from semantic_mcp.demo_ui import install_demo_ui
from semantic_mcp.integration import IntegrationSettings, install_mcp


def create_app(settings: Settings | None = None):
    settings = settings or IntegrationSettings(app_env="demo", resolver_mode="llm", execution_mode="demo")
    if settings.resolver_mode != "llm" or settings.execution_mode != "demo":
        raise ValueError("This entry point requires LLM resolution and local demo execution")
    app = FastAPI(title="Standalone MCP LLM test")
    questions = list(json.loads(Path("examples/questions.json").read_text(encoding="utf-8")))
    install_demo_ui(app, questions, resolver_mode="llm")
    install_mcp(app, settings)
    return app
