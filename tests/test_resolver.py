import json

import httpx
import pytest
from sqlalchemy import select

from semantic_mcp.resolver import strict_output_schema


@pytest.mark.asyncio
async def test_llm_gets_catalogue_only_and_cache_skips_repeat_call(service):
    service.settings.resolver_mode = "llm"
    service.settings.llm_base_url = "https://approved.example/v1"
    service.settings.llm_model = "test-model"
    service.settings.llm_api_key = "test-key"
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert "CX_GOLD" not in json.dumps(body)
        assert "SUM(" not in json.dumps(body)
        assert "SELECT " not in json.dumps(body)
        assert "rows" not in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "outcome": "resolved",
                                    "rationale": "Interaction count by channel",
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

    await service.resolver.client.aclose()
    service.resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = await service.ask_question("Count interactions grouped by channel", "alice")
    second = await service.ask_question("Count interactions grouped by channel", "alice")
    assert first["status"] == second["status"] == "answered"
    assert len(requests) == 1
    assert first["provenance"]["llm_calls"] == 1
    assert second["provenance"]["llm_calls"] == 0
    assert first["rows"] == second["rows"]


@pytest.mark.asyncio
async def test_malformed_output_retries_once_and_audits_failure(service):
    service.settings.resolver_mode = "llm"
    service.settings.llm_base_url = "https://approved.example/v1"
    service.settings.llm_model = "test-model"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "not valid JSON"}}]})

    await service.resolver.client.aclose()
    service.resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await service.ask_question("Count interactions", "alice")
    assert response["status"] == "error"
    assert len(calls) == 2
    assert response["rows"] == []
    with service.store.engine.connect() as conn:
        audit = json.loads(
            conn.execute(
                select(service.store.audit.c.payload).where(
                    service.store.audit.c.request_id == response["request_id"]
                )
            ).scalar_one()
        )
    assert audit["llm_calls"] == 2
    assert "generated_sql" not in audit


def test_provider_strict_schema_has_required_keys_and_no_defaults():
    def check(node):
        if isinstance(node, dict):
            assert "default" not in node
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    check(strict_output_schema())
