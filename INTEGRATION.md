# Integrate MCP into the existing FastAPI backend

This package adds the working two-tool service without replacing your main.py,
host routes, request logging, database initialization, or exception handlers.
The default mount is /analytics: MCP is /analytics/mcp and saved-query REST is
/analytics/api/v1/saved-queries/{id}. This namespace isolates child authentication
and errors from host endpoints. The standalone service still uses /mcp.

## Create your organisation branch

Run in the organisation backend checkout:

```powershell
git switch -c feature/mcp-integration
```

Transfer these files from this project using your approved internal process:

- semantic_mcp/ (exclude __pycache__)
- examples/
- scripts/seed_demo.py, scripts/prepare_tokenizer.py, scripts/demo_client.py
- requirements-mcp.txt

Do not replace your existing main.py, Dockerfile, requirements.txt, src/, or .env.
Do not transfer .venv/, var/, local databases, or Git metadata.

## Dependencies

The host Dockerfile uses Python 3.11; the local terminal shown uses Python 3.14.
Run installation and verification in the actual application environment.
Package metadata supports Python 3.11, but execution tests on 3.11 are still required.
Our local tests run on Python 3.12. Resolve the combined requirements before deployment:

```text
python -m pip install --dry-run -r requirements.txt -r requirements-mcp.txt
python -m pip install -r requirements.txt -r requirements-mcp.txt
python -m pip check
```

For Docker, change its existing pip install line to include both requirement files.
Ensure both are copied before that line. Add COPY semantic_mcp ./semantic_mcp,
COPY examples ./examples, and COPY scripts ./scripts before USER appuser.
Keep the existing application startup command and port.
Run python scripts/prepare_tokenizer.py during image build and make var/ readable
and writable by appuser along with the other application runtime directories.
In demo images, run python scripts/seed_demo.py before changing to appuser.
Do not seed demo tables on a real warehouse.

## main.py change

Add this import:

```python
from semantic_mcp.integration import install_mcp
```

At the end of create_app(), after all existing router/handler registration and
immediately before return app, add:

```python
install_mcp(app)
return app
```

Keep the existing lifespan unchanged. install_mcp composes it with MCP startup
and cleanup. Call once before serving; do not add a second lifespan manager.
If the organisation has a catch-all route or mount, register MCP before it.
The host OpenAPI schema does not include child routes; use /analytics/docs.

## Isolated environment configuration

Integrated mode uses MCP_ prefixes to avoid inheriting host DATABASE_URL and
other unrelated configuration. Add the following to the organisation .env:

```dotenv
MCP_APP_ENV=demo
MCP_MODEL_PATH=examples/cx.ossie.yaml
MCP_DATABASE_URL=sqlite:///var/mcp-service.db
MCP_DEMO_DATABASE_PATH=var/warehouse.duckdb
MCP_RESOLVER_MODE=fixture
MCP_EXECUTION_MODE=demo
MCP_API_TOKENS={"replace-with-a-local-test-token":"mcp-test-client"}
MCP_ALLOWED_HOSTS=["127.0.0.1:*","localhost:*"]
MCP_ALLOWED_ORIGINS=["http://127.0.0.1:*","http://localhost:*"]
```

Use a separate MCP database initially: its tables and create_all are independent
of the host Alembic schema. Sharing the host database requires schema and migration
review; this package does not integrate the host ORM/session or migrations.
Host middleware still runs on child requests; host route dependencies do not.
MCP explicitly authenticates its own requests; the child /health remains public.
Use approved secret injection and actual allowed hosts/origins for deployment.
Live mode requires the corresponding MCP_SNOWFLAKE_*/MCP_TERADATA_* credentials.

For real question interpretation with local data, set MCP_RESOLVER_MODE=llm,
MCP_LLM_BASE_URL, MCP_LLM_API_KEY, and MCP_LLM_MODEL. The provider must support
Chat Completions with strict JSON-schema output; direct Anthropic APIs need an adapter.

## Local smoke test

For a visual test without importing host Azure settings, copy main_mcp_demo.py
to the backend root and run:

```powershell
python -m uvicorn main_mcp_demo:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/, enter local-demo-token, and connect. Pick a sample
question and Ask; then Save and Run saved query. This page uses MCP JSON-RPC
for discovery/ask/save and REST for reruns. The page is public on this local demo;
all data endpoints still require bearer authentication. Tokens are not persisted.
The demo entry point uses fixture interpretations and synthetic data only.
Prepare sample data/tokenizer first as below. Copy the updated semantic_mcp/demo_ui.py
along with main_mcp_demo.py; the page is not installed on the full host by default.

From the backend project root, prepare synthetic data and tokenizer, then use the
existing backend startup command:

```powershell
python scripts/prepare_tokenizer.py
python scripts/seed_demo.py
python main.py
```

In a second terminal using the same environment:

```powershell
$env:SERVICE_URL = 'http://127.0.0.1:8000/analytics'
$env:SERVICE_TOKEN = 'replace-with-a-local-test-token'
python scripts/demo_client.py
```

The client discovers both tools, asks a question, saves it and reruns it through
REST with zero LLM calls. Check /analytics/health for demo/fixture mode, confirm
unauthenticated /analytics/mcp fails with 401, and retest existing host endpoints.
Fixture mode accepts examples/questions.json questions only. Sample fiscal dates
cover July 27 to December 27, 2026; the relative demo requires a date within coverage.

## Scope of verification

Integration tests cover host lifespan preservation, existing routes, namespaced
configuration and child MCP authentication/transport. They do not validate your
organisation dependency resolution, Python 3.11 runtime, reverse proxy, live LLM,
Teradata/Snowflake results, or organisation authentication middleware.
