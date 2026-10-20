from fastapi import FastAPI
from fastapi.testclient import TestClient

from semantic_mcp.demo_ui import install_demo_ui
from semantic_mcp.integration import install_mcp


def test_demo_page_public_but_data_requires_token(service, monkeypatch):
    monkeypatch.setattr("semantic_mcp.app.QueryService", lambda settings: service)
    host = FastAPI()
    install_demo_ui(host, ["How many total interactions by channel?", "</script><script>alert(1)</script>"])
    install_mcp(host, service.settings)
    with TestClient(host) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Connect and discover tools" in page.text
        assert "notifications/initialized" in page.text
        assert "__DEMO_CONFIG__" not in page.text
        assert "</script><script>alert(1)</script>" not in page.text
        assert "localStorage" not in page.text
        assert client.post("/analytics/mcp", json={}).status_code == 401
