import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from semantic_mcp.config import Settings
from semantic_mcp.contracts import CannotAnswer, QueryResponse
from semantic_mcp.service import QueryService
from semantic_mcp.store import new_id

caller_context = ContextVar("caller", default=None)


def caller():
    value = caller_context.get()
    if value is None:
        raise RuntimeError("Authentication required")
    return value


class BearerAuth:
    """ASGI auth covers MCP and REST without BaseHTTPMiddleware stream interference."""

    def __init__(self, app, tokens):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]
        if scope["type"] != "http" or path == "/health":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        header = headers.get(b"authorization", b"").decode("latin1")
        scheme, _, token = header.partition(" ")
        identity = None
        if scheme.casefold() == "bearer" and token:
            for expected, owner in self.tokens.items():
                if secrets.compare_digest(token.encode(), expected.encode()):
                    identity = owner
        if identity is None:
            response = QueryResponse(
                status="error",
                request_id=new_id("req"),
                reason_code="unauthorized",
                message="A valid bearer token is required.",
            )
            await JSONResponse(
                response.model_dump(mode="json"), status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
            return
        marker = caller_context.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            caller_context.reset(marker)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    service = QueryService(settings)
    mcp = FastMCP(
        "CX Semantic Analytics",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
        )
    )
    async def ask_question(question: str) -> dict[str, Any]:
        """Answer an aggregate CX question using governed metrics.

        Pass the user's question close to verbatim. Show interpretation, defaults, filters,
        caveats and warehouse provenance alongside any chart. Percent values are fractions.
        Ambiguity with no governed default returns cannot_answer with candidate metrics.
        """
        return await service.ask_question(question, caller())

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        )
    )
    async def save_query(resolution_id: str, name: str) -> dict[str, Any]:
        """Save an answered server-issued resolution for dashboard reuse.

        Only this caller's successful resolution IDs are accepted. Relative time stays relative.
        The returned saved_query_id is used with the dashboard REST API; never send SQL or a spec.
        """
        return await service.save_query(resolution_id, name, caller())

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await service.close()

    app = FastAPI(title="CX Semantic Query Service", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.mcp = mcp
    app.add_middleware(BearerAuth, tokens=settings.api_tokens)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "execution_mode": settings.execution_mode,
            "resolver_mode": settings.resolver_mode,
            "model": service.model.name,
        }

    @app.get("/api/v1/saved-queries/{saved_query_id}")
    async def get_saved(saved_query_id: str):
        try:
            return service.store.get_saved(saved_query_id, caller())
        except CannotAnswer as exc:
            response = QueryResponse(
                status="cannot_answer",
                request_id=new_id("req"),
                reason_code=exc.reason_code,
                message=str(exc),
            )
            return JSONResponse(response.model_dump(mode="json"), status_code=404)

    @app.post("/api/v1/saved-queries/{saved_query_id}/run")
    async def run_saved(saved_query_id: str):
        return await service.run_saved(saved_query_id, caller())

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        response = QueryResponse(
            status="error",
            request_id=new_id("req"),
            reason_code="invalid_request",
            message="Request parameters do not match the API schema.",
        )
        return JSONResponse(response.model_dump(mode="json"), status_code=422)

    app.mount("/", mcp_app)
    return app
