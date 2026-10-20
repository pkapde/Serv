import asyncio
from datetime import date

import pytest

from scripts.seed_demo import seed
from semantic_mcp.config import Settings
from semantic_mcp.service import QueryService
from semantic_mcp.time_ranges import resolve_time_range


@pytest.fixture
def service(tmp_path, monkeypatch):
    seed(tmp_path / "warehouse.duckdb")
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'service.db'}",
        demo_database_path=tmp_path / "warehouse.duckdb",
    )
    monkeypatch.setattr(
        "semantic_mcp.service.resolve_time_range",
        lambda value, calendar: resolve_time_range(value, calendar, today=date(2026, 10, 10)),
    )
    instance = QueryService(settings)
    yield instance
    asyncio.run(instance.close())
