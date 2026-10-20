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


def test_azure_root_chooses_v1_without_version_and_legacy_with_version():
    values = dict(
        _env_file=None,
        resolver_mode="llm",
        llm_provider="azure_openai",
        llm_base_url="https://resource.openai.azure.com",
        llm_model="deployment",
        llm_api_key="test-key",
    )
    assert Settings(**values).llm_base_url.endswith("/openai/v1")
    assert Settings(**values, llm_api_version="test-version").llm_provider == "azure_openai"
    values["llm_base_url"] += "/openai/v1"
    assert Settings(**values).llm_api_version == ""


@pytest.mark.parametrize("provider", ["azure_openai", "openai_compatible"])
async def test_pasted_foundry_project_url_is_normalized_and_executed(service, provider):
    configured = Settings(
        _env_file=None,
        resolver_mode="llm",
        llm_provider=provider,
        llm_base_url="https://example-resource.services.ai.azure.com/api/projects/example-project/",
        llm_model="gpt-5",
        llm_api_key="test-key",
        llm_send_temperature=True,
    )
    assert configured.llm_provider == "azure_openai"
    assert configured.llm_base_url == "https://example-resource.services.ai.azure.com/openai/v1"
    assert configured.llm_send_temperature is False
    for name in (
        "resolver_mode",
        "llm_provider",
        "llm_base_url",
        "llm_model",
        "llm_api_key",
        "llm_send_temperature",
    ):
        setattr(service.settings, name, getattr(configured, name))

    def azure(request):
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["api-key"] == "test-key"
        assert "temperature" not in json.loads(request.content)
        result = {
            "outcome": "ambiguous",
            "rationale": "No declared default",
            "candidates": ["resolution_rate_1d", "resolution_rate_7d"],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    await service.resolver.client.aclose()
    service.resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(azure))
    answer = await service.ask_question("What is the resolution rate?", "tester")
    assert answer["status"] == "cannot_answer"
    assert answer["candidates"] == ["resolution_rate_1d", "resolution_rate_7d"]


def test_other_azure_paths_are_not_silently_rewritten():
    with pytest.raises(ValidationError, match="resource root"):
        Settings(
            _env_file=None,
            resolver_mode="llm",
            llm_provider="azure_openai",
            llm_base_url="https://resource.services.ai.azure.com/unsupported/path",
            llm_model="deployment",
            llm_api_key="test-key",
        )
