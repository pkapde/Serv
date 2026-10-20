from fastapi.testclient import TestClient

from semantic_mcp.app import create_app


def rpc(client, method, params=None, token="local-demo-token", request_id=1):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def test_mcp_transport_discovery_question_save_and_rest(service, monkeypatch):
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    app = create_app(service.settings)
    with TestClient(app) as client:
        assert client.get("/health").json()["execution_mode"] == "demo"
        initialize = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        )
        assert initialize.status_code == 200
        assert initialize.json()["result"]["serverInfo"]["name"] == "CX Semantic Analytics"
        discovered = rpc(client, "tools/list").json()["result"]["tools"]
        assert {tool["name"] for tool in discovered} == {"ask_question", "save_query"}
        answer = rpc(
            client,
            "tools/call",
            {"name": "ask_question", "arguments": {"question": "How many total interactions by channel?"}},
        ).json()["result"]["structuredContent"]
        assert answer["status"] == "answered"
        assert answer["rows"] == [["Call", 4], ["Chat", 3], ["Email", 1]]
        saved = rpc(
            client,
            "tools/call",
            {
                "name": "save_query",
                "arguments": {"resolution_id": answer["resolution_id"], "name": "Volumes"},
            },
        ).json()["result"]["structuredContent"]
        assert saved["status"] == "answered"
        url = f"/api/v1/saved-queries/{saved['saved_query_id']}"
        headers = {"Authorization": "Bearer local-demo-token"}
        assert client.get(url, headers=headers).json()["spec"]["time_range"]["kind"] == "all_time"
        rerun = client.post(url + "/run", headers=headers).json()
        assert rerun["rows"] == answer["rows"]
        assert rerun["provenance"]["llm_calls"] == 0


def test_auth_covers_mcp_and_rest(service, monkeypatch):
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    with TestClient(create_app(service.settings)) as client:
        assert rpc(client, "tools/list", token="invalid").status_code == 401
        assert client.get("/api/v1/saved-queries/fake").status_code == 401
        assert client.post("/api/v1/saved-queries/fake/run").status_code == 401
