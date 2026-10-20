"""Execution adapters. Production accounts must have SELECT-only database grants."""

import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from semantic_mcp.contracts import QueryExecutionError


@dataclass
class ExecutionResult:
    names: list[str]
    rows: list[list]
    truncated: bool
    query_id: str


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Preserve decimal precision on the wire; column metadata declares decimal.
        return str(value)
    if isinstance(value, bytes):
        raise ValueError("Binary query results are outside the aggregate response contract")
    return value


def collect(cursor, query, query_id):
    names = [col[0].casefold() for col in cursor.description]
    rows = cursor.fetchmany(query.effective_limit + 1)
    return ExecutionResult(
        names,
        [[json_value(v) for v in row] for row in rows[: query.effective_limit]],
        len(rows) > query.effective_limit,
        query_id,
    )


class DemoExecutor:
    def __init__(self, settings):
        self.path = settings.demo_database_path
        self.timeout = settings.query_timeout_seconds

    def execute(self, query, request_id):
        import duckdb

        if not self.path.exists():
            raise QueryExecutionError(
                "demo_data_missing",
                "Local sample data is missing. Stop the server, run python scripts/seed_demo.py from the repository root, then restart.",
            )
        tree = sqlglot.parse_one(query.sql, read=query.warehouse)
        # Explicit demo bindings only. DuckDB never pretends to be a live warehouse.
        for table in tree.find_all(exp.Table):
            table.set("catalog", None)
            table.set("db", None)
        sql = tree.sql(dialect="duckdb", unsupported_level=ErrorLevel.RAISE)
        with duckdb.connect(str(self.path.resolve()), read_only=True) as conn:
            timer = threading.Timer(self.timeout, conn.interrupt)
            timer.daemon = True
            timer.start()
            try:
                return collect(conn.execute(sql), query, "demo:" + request_id)
            finally:
                timer.cancel()


class SnowflakeExecutor:
    def __init__(self, settings):
        self.settings = settings

    def execute(self, query, request_id):
        import snowflake.connector

        s = self.settings
        if not all((s.snowflake_account, s.snowflake_user, s.snowflake_password, s.snowflake_warehouse)):
            raise RuntimeError("Snowflake connection configuration is incomplete")
        params = {
            "account": s.snowflake_account,
            "user": s.snowflake_user,
            "password": s.snowflake_password,
            "warehouse": s.snowflake_warehouse,
            "login_timeout": min(s.query_timeout_seconds, 30),
            "network_timeout": s.query_timeout_seconds,
            "session_parameters": {
                "QUERY_TAG": request_id,
                "STATEMENT_TIMEOUT_IN_SECONDS": s.query_timeout_seconds,
                "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": s.query_timeout_seconds,
                "WEEK_START": 1,
            },
        }
        for key in ("database", "schema", "role"):
            if getattr(s, "snowflake_" + key):
                params[key] = getattr(s, "snowflake_" + key)
        with snowflake.connector.connect(**params) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query.sql, timeout=s.query_timeout_seconds)
                return collect(cursor, query, cursor.sfqid)


class TeradataExecutor:
    def __init__(self, settings):
        self.settings = settings

    def execute(self, query, request_id):
        import teradatasql

        s = self.settings
        if not all((s.teradata_host, s.teradata_user, s.teradata_password)):
            raise RuntimeError("Teradata connection configuration is incomplete")
        params = {"host": s.teradata_host, "user": s.teradata_user, "password": s.teradata_password}
        if s.teradata_database:
            params["database"] = s.teradata_database
        with teradatasql.connect(**params) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET QUERY_BAND = 'request_id=" + request_id + ";' FOR SESSION")
                # Driver escape controls server-side request timeout, not just HTTP waiting.
                sql = "{fn teradata_request_timeout(" + str(s.query_timeout_seconds) + ")} " + query.sql
                cursor.execute(sql)
                return collect(cursor, query, request_id)


def executor_for(settings, warehouse):
    if settings.execution_mode == "demo":
        return DemoExecutor(settings)
    return SnowflakeExecutor(settings) if warehouse == "snowflake" else TeradataExecutor(settings)
