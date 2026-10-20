"""Mount the complete MCP service in an existing FastAPI host."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic_settings import SettingsConfigDict

from semantic_mcp.app import create_app
from semantic_mcp.config import Settings


class IntegrationSettings(Settings):
    """Avoid consuming the host's DATABASE_URL, API_TOKENS or model configuration."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MCP_", extra="ignore")


def install_mcp(host: FastAPI, settings: Settings | None = None, prefix: str = "/analytics"):
    """Call once during app construction, after registering the host routes.

    The namespaced child owns auth, exception handlers and persistence. Its lifespan
    is explicitly composed because mounted ASGI applications do not start themselves.
    """
    if hasattr(host.state, "semantic_mcp_app"):
        raise ValueError("MCP is already installed on this application")
    if not prefix.startswith("/") or prefix == "/" or prefix.endswith("/"):
        raise ValueError("Use a non-root prefix with no trailing slash, e.g. /analytics")
    if any(getattr(route, "path", None) == prefix for route in host.routes):
        raise ValueError("The MCP mount prefix is already registered")
    child = create_app(settings if settings is not None else IntegrationSettings())
    previous_lifespan = host.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous_lifespan(app) as state:
            async with child.router.lifespan_context(child):
                yield state

    host.router.lifespan_context = lifespan
    host.mount(prefix, child, name="semantic-analytics")
    host.state.semantic_mcp_app = child
    return child
