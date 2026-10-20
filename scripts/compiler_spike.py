"""Inspect both production dialects using the actual service compiler; no execution."""

from pathlib import Path

from semantic_mcp.compiler import Compiler
from semantic_mcp.contracts import QuerySpec
from semantic_mcp.model import SemanticModel

if __name__ == "__main__":
    model = SemanticModel(Path("examples/cx.ossie.yaml"))
    compiler = Compiler(model, row_cap=5000, default_warehouse="snowflake")
    spec = QuerySpec.model_validate(
        {
            "metrics": [{"name": "hold_pct_handle_time", "basis": "default"}],
            "group_by": [{"name": "interaction_medium"}],
        }
    )
    for warehouse in ("snowflake", "teradata"):
        model.datasets["interactions"].preferred_warehouse = warehouse
        compiled = compiler.compile(spec)
        print(warehouse, "\n", compiled.sql, "\n")
