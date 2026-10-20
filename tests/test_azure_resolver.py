import json

import httpx
import pytest
from pydantic import ValidationError

from semantic_mcp.config import Settings


@pytest.mark.parametrize("v1", [False, True])
async def test_azure_request_and_query_execution(service, v1):
    service.settings.resolver_mode = "llm"
    service.settings.llm_provider = "azure_openai"
    service.settings.llm_base_url = "https://resource.openai.azure.com" + ("/openai/v1/" if v1 else "/")
    service.settings.llm_model = "test-deployment"
    service.settings.llm_api_key = "test-azure-key"
    service.settings.llm_api_version = "test-version"
    service.settings.llm_send_temperature = not v1
    calls = []

    def provider(request):
        calls.append(request)
        path = "/openai/v1/chat/completions" if v1 else "/openai/deployments/test-deployment/chat/completions"
        assert request.url.path == path
        assert request.headers["api-key"] == "test-azure-key"
        assert "authorization" not in request.headers
        assert request.url.params.get("api-version") == (None if v1 else "test-version")
        body = json.loads(request.content)
        assert body["model"] == "test-deployment"
        assert body["response_format"]["json_schema"]["strict"] is True
        if v1:
            assert "temperature" not in body
        else:
            assert body["temperature"] == 0
        resolution = {
            "outcome": "resolved",
            "rationale": "Count interactions",
            "spec": {"metrics": [{"name": "calls_count"}], "group_by": [{"name": "interaction_medium"}]},
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(resolution)}}]})

    await service.resolver.client.aclose()
    service.resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    answer = await service.ask_question("Count interactions per channel", "tester")
    assert answer["status"] == "answered"
    assert answer["rows"] == [["Call", 4], ["Chat", 3], ["Email", 1]]
    assert answer["provenance"]["llm_calls"] == 1
    assert len(calls) == 1
    original = service.resolver.cache_key("Question", service.model.hash)
    service.settings.llm_api_version = "changed-version"
    assert service.resolver.cache_key("Question", service.model.hash) != original


def test_azure_root_requires_version_and_v1_does_not():
    values = dict(
        _env_file=None,
        resolver_mode="llm",
        llm_provider="azure_openai",
        llm_base_url="https://resource.openai.azure.com",
        llm_model="deployment",
        llm_api_key="test-key",
    )
    with pytest.raises(ValidationError, match="require LLM_API_VERSION"):
        Settings(**values)
    assert Settings(**values, llm_api_version="test-version").llm_provider == "azure_openai"
    values["llm_base_url"] += "/openai/v1"
    assert Settings(**values).llm_api_version == ""
