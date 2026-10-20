import json
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_mcp.compiler import Compiler
from semantic_mcp.contracts import CannotAnswer, QuerySpec
from semantic_mcp.model import SemanticModel
from semantic_mcp.warehouses import DemoExecutor


def expr(value):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": value}]}


def ext(value):
    return [{"vendor_name": "COMMON", "data": json.dumps({"cx_mcp": value})}]


def model_with_snapshots(tmp_path):
    document = yaml.safe_load(Path("examples/cx.ossie.yaml").read_text(encoding="utf-8"))
    body = document["semantic_model"][0]
    body["datasets"].append(
        {
            "name": "snapshots",
            "source": "CX_GOLD.PUBLIC.SNAPSHOTS",
            "primary_key": ["snapshot_id"],
            "custom_extensions": ext(
                {
                    "time_dimension": "snapshot_date",
                    "preferred_warehouse": "snowflake",
                    "bindings": {"snowflake": "CX_GOLD.PUBLIC.SNAPSHOTS", "teradata": "CX_GOLD.SNAPSHOTS"},
                }
            ),
            "fields": [
                {"name": "snapshot_id", "expression": expr("snapshot_id"), "datatype": "Integer"},
                {
                    "name": "snapshot_date",
                    "expression": expr("snapshot_date"),
                    "datatype": "Date",
                    "dimension": {"is_time": True},
                    "custom_extensions": ext({"exposed": True}),
                },
                {"name": "open_items", "expression": expr("open_items"), "datatype": "Integer"},
            ],
        }
    )
    body["metrics"].append(
        {
            "name": "backlog",
            "expression": expr("SUM(snapshots.open_items)"),
            "datatype": "Integer",
            "custom_extensions": ext(
                {
                    "label": "Backlog",
                    "owner": "cx-ops",
                    "additivity": "semi_additive",
                    "non_additive_dimension": "snapshot_date",
                    "window_choice": "max",
                }
            ),
        }
    )
    path = tmp_path / "snapshots.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return SemanticModel(path)


@pytest.mark.parametrize("warehouse", ["snowflake", "teradata"])
def test_snapshot_uses_last_value_rather_than_sum_across_time(service, tmp_path, warehouse):
    model = model_with_snapshots(tmp_path)
    model.datasets["snapshots"].preferred_warehouse = warehouse
    compiler = Compiler(model, 5000, "snowflake")
    with duckdb.connect(str(service.settings.demo_database_path)) as conn:
        conn.execute("CREATE TABLE snapshots(snapshot_id INTEGER, snapshot_date DATE, open_items INTEGER)")
        conn.execute(
            "INSERT INTO snapshots VALUES (1,'2026-09-01',10),(2,'2026-09-02',20),(3,'2026-09-03',7)"
        )
    compiled = compiler.compile(QuerySpec.model_validate({"metrics": [{"name": "backlog"}]}))
    result = DemoExecutor(service.settings).execute(compiled, "req_snapshot")
    assert result.rows == [[7]]


def test_cross_warehouse_metrics_are_rejected(service, tmp_path):
    model = model_with_snapshots(tmp_path)
    model.datasets["snapshots"].preferred_warehouse = "teradata"
    compiler = Compiler(model, 5000, "snowflake")
    spec = QuerySpec.model_validate({"metrics": [{"name": "calls_count"}, {"name": "backlog"}]})
    with pytest.raises(CannotAnswer, match="multiple warehouses"):
        compiler.compile(spec)


def test_fanout_join_is_rejected_but_many_to_one_is_available(service, tmp_path):
    document = yaml.safe_load(Path("examples/cx.ossie.yaml").read_text(encoding="utf-8"))
    body = document["semantic_model"][0]
    body["datasets"][0]["fields"].append(
        {"name": "customer_id", "datatype": "Integer", "expression": expr("customer_id")}
    )
    body["datasets"].append(
        {
            "name": "customers",
            "source": "CX_GOLD.PUBLIC.CUSTOMERS",
            "primary_key": ["customer_id"],
            "custom_extensions": ext(
                {
                    "time_dimension": "customer_date",
                    "preferred_warehouse": "snowflake",
                    "bindings": {"snowflake": "CX_GOLD.PUBLIC.CUSTOMERS", "teradata": "CX_GOLD.CUSTOMERS"},
                }
            ),
            "fields": [
                {"name": "customer_id", "expression": expr("customer_id"), "datatype": "Integer"},
                {
                    "name": "customer_date",
                    "expression": expr("customer_date"),
                    "datatype": "Date",
                    "dimension": {"is_time": True},
                },
                {
                    "name": "customer_region",
                    "expression": expr("customer_region"),
                    "datatype": "String",
                    "custom_extensions": ext({"exposed": True}),
                },
            ],
        }
    )
    body["relationships"] = [
        {
            "name": "interactions_customers",
            "from": "interactions",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["customer_id"],
        }
    ]
    body["metrics"].append(
        {
            "name": "customer_count",
            "expression": expr("COUNT(customers.customer_id)"),
            "datatype": "Integer",
            "custom_extensions": ext({"label": "Customers", "owner": "cx-ops", "additivity": "additive"}),
        }
    )
    path = tmp_path / "joins.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    model = SemanticModel(path)
    compiler = Compiler(model, 5000, "snowflake")
    with duckdb.connect(str(service.settings.demo_database_path)) as conn:
        conn.execute("ALTER TABLE interactions ADD customer_id INTEGER DEFAULT 1")
        conn.execute("CREATE TABLE customers(customer_id INTEGER, customer_date DATE, customer_region TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, '2026-01-01', 'North')")
    good = QuerySpec.model_validate(
        {"metrics": [{"name": "calls_count"}], "group_by": [{"name": "customer_region"}]}
    )
    result = DemoExecutor(service.settings).execute(compiler.compile(good), "req_join")
    assert result.rows == [["North", 8]]
    bad = QuerySpec.model_validate(
        {"metrics": [{"name": "customer_count"}], "group_by": [{"name": "interaction_medium"}]}
    )
    with pytest.raises(CannotAnswer):
        compiler.compile(bad)
