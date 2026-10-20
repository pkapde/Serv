from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semantic_mcp.integration import IntegrationSettings, install_mcp


def test_host_lifecycle_routes_auth_and_mcp(service, monkeypatch):
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield {"host_state": "preserved"}
        events.append("shutdown")

    host = FastAPI(lifespan=lifespan)

    @host.get("/api/v1/existing")
    def existing():
        return {"existing": True}

    install_mcp(host, service.settings)
    with TestClient(host) as client:
        assert events == ["startup"]
        assert client.get("/api/v1/existing").json() == {"existing": True}
        assert client.get("/analytics/health").status_code == 200
        assert client.get("/analytics/api/v1/saved-queries/fake").status_code == 401
        result = client.post(
            "/analytics/mcp",
            headers={
                "Authorization": "Bearer local-demo-token",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert result.status_code == 200
        assert {t["name"] for t in result.json()["result"]["tools"]} == {"ask_question", "save_query"}
        headers = {
            "Authorization": "Bearer local-demo-token",
            "Accept": "application/json, text/event-stream",
        }

        def call(name, arguments):
            response = client.post(
                "/analytics/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200
            return response.json()["result"]["structuredContent"]

        answer = call("ask_question", {"question": "How many total interactions by channel?"})
        assert answer["rows"] == [["Call", 4], ["Chat", 3], ["Email", 1]]
        saved = call("save_query", {"resolution_id": answer["resolution_id"], "name": "Volumes"})
        rerun = client.post(
            f"/analytics/api/v1/saved-queries/{saved['saved_query_id']}/run", headers=headers
        ).json()
        assert rerun["rows"] == answer["rows"]
        assert rerun["provenance"]["llm_calls"] == 0
    assert events == ["startup", "shutdown"]


def test_integration_settings_are_namespaced(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "host-database-must-not-be-used")
    monkeypatch.setenv("MCP_DATABASE_URL", "sqlite:///var/mcp-integration.db")
    assert IntegrationSettings(_env_file=None).database_url == "sqlite:///var/mcp-integration.db"


def test_duplicate_install_rejected(service, monkeypatch):
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    host = FastAPI()
    install_mcp(host, service.settings)
    with pytest.raises(ValueError, match="already installed"):
        install_mcp(host, service.settings)
