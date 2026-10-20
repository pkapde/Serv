import duckdb

from main_mcp_llm import create_app
from semantic_mcp.warehouses import ExecutionResult


async def test_missing_demo_data_is_actionable_and_has_no_rows(service, tmp_path):
    service.settings.demo_database_path = tmp_path / "missing.duckdb"
    answer = await service.ask_question("How many total interactions by channel?", "tester")
    assert answer["status"] == "error"
    assert answer["reason_code"] == "demo_data_missing"
    assert "seed_demo.py" in answer["message"]
    assert answer["rows"] == []


async def test_schema_mismatch_is_identified(service, monkeypatch):
    class WrongColumns:
        def execute(self, query, request_id):
            return ExecutionResult(["incorrect_name"], [[1]], False, request_id)

    monkeypatch.setattr("semantic_mcp.service.executor_for", lambda *args: WrongColumns())
    answer = await service.ask_question("How many total interactions by channel?", "tester")
    assert answer["reason_code"] == "result_schema_mismatch"
    assert answer["rows"] == []


def test_llm_demo_creates_missing_sample_database(service, tmp_path, monkeypatch):
    service.settings.resolver_mode = "llm"
    service.settings.demo_database_path = tmp_path / "new-demo.duckdb"
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    create_app(service.settings)
    with duckdb.connect(str(service.settings.demo_database_path), read_only=True) as connection:
        assert connection.execute("select count(*) from interactions").fetchone()[0] == 9
