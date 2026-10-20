# CX Semantic MCP MVP

A local query service with exactly two MCP tools: `ask_question` and `save_query`.
For run commands, architecture, implementation details and troubleshooting, read [MCP_RUNBOOK.md](MCP_RUNBOOK.md).
For integration into an existing FastAPI backend, follow [INTEGRATION.md](INTEGRATION.md).
It loads a static Ossie model, resolves a question into a typed spec, validates it, compiles with
MetricFlow, executes through a warehouse adapter, and returns data plus provenance.

The included demo uses synthetic DuckDB data and fixture interpretations. It does not contact an LLM,
Teradata, or Snowflake. Live adapters and the approved-endpoint resolver are separate modes.

## Run locally on Windows

For standalone testing with a real resolver LLM and the browser UI (no tuning-fork),
copy `.env.llm.example` to `.env`, fill the three `MCP_LLM_*` fields, prepare data/tokenizer,
then run `python -m uvicorn main_mcp_llm:create_app --factory --host 127.0.0.1 --port 8000`.
Open http://127.0.0.1:8000/ and enter `local-demo-token`. This uses real interpretation
and synthetic DuckDB execution. The provider must implement the current resolver's
Chat Completions + strict JSON-schema + bearer-token contract; see MCP_RUNBOOK.md.

Run commands from this project directory using Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe scripts/prepare_tokenizer.py
.\.venv\Scripts\python.exe scripts/start.py
```

Startup creates synthetic data if missing. In another terminal:

```powershell
.\.venv\Scripts\python.exe scripts/demo_client.py
```

MCP endpoint: `http://127.0.0.1:8000/mcp` (Streamable HTTP, stateless).
Bearer token for the **local demo only**: `local-demo-token`.
Health: `http://127.0.0.1:8000/health`. API documentation: `/docs`, with bearer authentication.

The demo client discovers the two tools, asks a question, saves its resolution, and reruns the saved query
through REST. Use any exact question in `examples/questions.json` in fixture mode.

Example: `What is hold percentage by channel last fiscal month?`
For the sample September fiscal period, Call returns `0.25` (25%), Chat `0.10` (10%), Email `null`.
Ratios use sums at output grain; zero denominator returns null. Percent values are fractions.
Without a time phrase, queries use all time. Demo fiscal periods cover July 27 through December 27, 2026;
relative fiscal questions outside that coverage return `cannot_answer`.

## Interfaces

| Interface | Input | Purpose |
| --- | --- | --- |
| MCP `ask_question` | `question` | Resolve, validate, compile, execute, return provenance and `resolution_id` |
| MCP `save_query` | `resolution_id`, `name` | Persist this caller's server-issued, successful interpretation |
| REST `GET /api/v1/saved-queries/{id}` | Saved query ID | Retrieve this caller's stored spec and interpretation |
| REST `POST /api/v1/saved-queries/{id}/run` | Saved query ID | Recompute relative dates and execute; zero LLM calls |

Responses use `answered`, `cannot_answer`, or `error`. Query failures return no rows. Request IDs tie
responses to persisted audit records. There is no clarification loop, raw-SQL tool, catalogue tool,
model authoring, or cross-warehouse federation.

## Configuration

Copy `.env.example` to `.env` and edit it; `.env` is ignored. Never commit real credentials.
`Settings` also reads environment variables, suitable for AWS Secrets Manager injection.

- `EXECUTION_MODE=demo` executes generated SQL on read-only synthetic DuckDB data.
- `EXECUTION_MODE=live` selects the Snowflake connector or Teradata SQL driver from the model's binding.
- `RESOLVER_MODE=fixture` supports only the checked-in demo question mappings.
- `RESOLVER_MODE=llm` calls an approved endpoint implementing `/chat/completions` with strict JSON-schema
  output. Configure `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`. Other provider APIs need a resolver adapter.
- `API_TOKENS` is a JSON mapping from bearer token to caller identity. Resolutions and saved queries are
  scoped to that identity. Production tokens are client application credentials; this is not per-user RLS.
- SQLite holds local saved specs, cache and audit; `DATABASE_URL=postgresql+psycopg://...` enables PostgreSQL.
- Configure `ALLOWED_HOSTS` and `ALLOWED_ORIGINS` for your HTTPS host before remote MCP use.

`APP_ENV=production` refuses fixture/demo modes, SQLite, the default demo token, or a non-HTTPS LLM URL.
Terminate TLS at the AWS load balancer. OAuth discovery is not implemented; this build uses bearer tokens.
Use SELECT-only warehouse accounts and an independently managed application database account.
Warehouse drivers enforce query timeouts; the service caps results and active executions per process.
The row cap probes one extra row so exact-limit results are not falsely marked truncated.

## Model and compiler contract

This build **pins Ossie 0.1.1**, the schema supported by the installed native MetricFlow converter.
It does not claim to compile every possible Ossie expression. `examples/cx.ossie.yaml` is the reference.

Governance metadata is encoded under `custom_extensions`, using a `COMMON` extension whose JSON `data`
contains the `cx_mcp` namespace. The loader keeps the extensions independently of the compiler:

- Model: explicit, contiguous fiscal periods, timezone, and a day-level time-spine table per warehouse.
- Dataset: time dimension, warehouse bindings, and optional preferred execution warehouse.
- Metric: label, owner, additivity, unit, synonyms, variant group/default, explicit variant phrases,
  intrinsic structured filters, and caveats with expiry.
- Field: whether it is exposed as a query dimension, synonyms, and optional allowed values.

Initial metric support: simple SUM, COUNT, COUNT DISTINCT, AVG, MIN, MAX over dataset-qualified fields,
and ratios of these aggregates. Aggregate fields must bind directly to same-named physical columns.
Computed/aliased aggregate inputs, complex metric expressions, converter fallback approximations, composite
relationship keys, and differing foreign/primary key names are rejected at startup. Supported joins
use a shared single entity key and declared PK/unique keys. Expose only intended aggregation dimensions;
record-level identifiers are not in the resolver catalogue.

Snapshot metrics require `additivity=semi_additive`, `non_additive_dimension`, `window_choice` (min/max),
and optional `window_groupings`; these are passed to MetricFlow's native snapshot semantics.
Ratios and averages must be non-additive and are recomputed at output grain.

The service applies native MetricFlow manifest transformations before planning, including COUNT
normalization. It derives valid metric/dimension combinations from compiler metadata. Every intrinsic
filter is installed into the compiler's metric definition, rather than entrusted to the LLM.
Compiler-generated alias IDs are normalized so cold/warm planning and saved reruns yield identical SQL.

Snowflake uses MetricFlow's Snowflake renderer. Teradata uses SQLGlot translation with unsupported
features set to fail, followed by a single-SELECT and model-binding check. This is a tested local
compilation path, **not yet warehouse reconciliation**. Reconcile real fiscal/date expressions, ratios,
joins, distinct counts and snapshots against trusted queries on both warehouses before production use.

Each question executes in one warehouse. Dataset preferences determine the target; the client cannot
submit a warehouse or SQL. Missing bindings and cross-warehouse metric combinations fail closed.
Saved specs keep logical names. A model-file hash mismatch requires resolving again, rather than silently
rebinding a saved dashboard. Model changes require a service restart.

## Provenance and audit

Answers include metric owners, explicit/default choices, applied metric and question filters, absolute
dates plus relative time spec, live caveats, model hash, SQL hash, warehouse, source bindings, query ID,
execution timestamp, truncation, and demo/live markers. Decimal driver values are serialized as strings
to retain precision. Teradata returns the service request ID as its correlation ID; the same ID is attached
through QUERY_BAND. Snowflake returns its warehouse query ID.

Audit records include questions, attempted/canonical resolver outputs, validated specs, SQL, caller,
row count and latency. They do not retain result rows. The spec cache includes model hash, resolver
version/mode, endpoint and model. Case is preserved in normalized questions to protect filter semantics.
The LLM receives a catalogue without table names or SQL and never receives query results.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check semantic_mcp scripts tests
```

Tests cover the actual MCP HTTP protocol, authentication, ratio-of-sums, zero denominator, intrinsic
filters, ambiguity/default enforcement, unknown names, invalid values, saved-query ownership and
model changes, relative-period rollover, cache/SQL repeatability, caps, and both dialect compilation paths.

`scripts/compiler_spike.py` is an offline inspection helper. No live credentials are used by tests.

## Container demo

```text
docker compose up --build
```

This starts a local demo backed by PostgreSQL for persistence. Docker is not required for local testing.
The image prewarms tokenization at build time so runtime does not need to fetch vocabulary files.

## Remaining integration work

Supply the real model, warehouse accounts/network access, and approved LLM endpoint. Run a client-owned
golden set (~50 questions including ambiguity) and warehouse reconciliation. External Claude/Replit
connections, live LLM accuracy, actual Teradata/Snowflake execution, Docker execution and AWS deployment
require environment-specific verification. The local fixture set is not a business accuracy benchmark.
