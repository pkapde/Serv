import json

import httpx
import pytest
from fastapi.testclient import TestClient

from main_mcp_llm import create_app


def test_standalone_llm_question_and_saved_run(service, monkeypatch):
    service.settings.resolver_mode = "llm"
    service.settings.llm_base_url = "https://approved.example/v1"
    service.settings.llm_model = "test-model"
    service.settings.llm_api_key = "private-test-key"
    calls = []

    def provider(request):
        calls.append(request)
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "outcome": "resolved",
                                    "rationale": "Count by channel",
                                    "spec": {
                                        "metrics": [{"name": "calls_count"}],
                                        "group_by": [{"name": "interaction_medium"}],
                                    },
                                }
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(
        service.resolver, "client", httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    with TestClient(create_app(service.settings)) as client:
        page = client.get("/").text
        assert "Live LLM interpretation" in page
        assert "No LLM credentials are needed" not in page
        assert "private-test-key" not in page
        assert client.get("/analytics/health").json()["resolver_mode"] == "llm"
        headers = {
            "Authorization": "Bearer local-demo-token",
            "Accept": "application/json, text/event-stream",
        }

        def call(name, arguments):
            result = client.post(
                "/analytics/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert result.status_code == 200
            return result.json()["result"]["structuredContent"]

        answer = call("ask_question", {"question": "Count interactions per medium using the model"})
        assert answer["rows"] == [["Call", 4], ["Chat", 3], ["Email", 1]]
        assert answer["provenance"]["llm_calls"] == 1
        saved = call("save_query", {"resolution_id": answer["resolution_id"], "name": "Live interpretation"})
        rerun = client.post(
            f"/analytics/api/v1/saved-queries/{saved['saved_query_id']}/run", headers=headers
        ).json()
        assert rerun["rows"] == answer["rows"]
        assert rerun["provenance"]["llm_calls"] == 0
        assert len(calls) == 1


def test_standalone_llm_rejects_fixture_mode(service):
    with pytest.raises(ValueError, match="requires LLM"):
        create_app(service.settings)
