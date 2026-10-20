import json

import pytest
import sqlglot
import yaml

from semantic_mcp.contracts import CannotAnswer, QuerySpec
from semantic_mcp.model import SemanticModel
from semantic_mcp.warehouses import DemoExecutor


@pytest.mark.parametrize("warehouse", ["snowflake", "teradata"])
def test_native_compilation_and_dialect_execution(service, warehouse):
    service.model.datasets["interactions"].preferred_warehouse = warehouse
    spec = QuerySpec.model_validate(
        {
            "metrics": [{"name": "hold_pct_handle_time", "basis": "default"}],
            "group_by": [{"name": "interaction_medium"}],
        }
    )
    compiled = service.compiler.compile(spec)
    assert compiled.warehouse == warehouse
    sqlglot.parse_one(compiled.sql, read=warehouse)
    data = DemoExecutor(service.settings).execute(compiled, "req_test")
    assert dict(data.rows)["Call"] == pytest.approx(125 / 550)
    assert dict(data.rows)["Chat"] == pytest.approx(50 / 350)
    assert dict(data.rows)["Email"] is None
    expected_binding = "CX_GOLD.PUBLIC.INTERACTIONS" if warehouse == "snowflake" else "CX_GOLD.INTERACTIONS"
    assert expected_binding in compiled.sql


@pytest.mark.parametrize("warehouse", ["snowflake", "teradata"])
@pytest.mark.parametrize("grain", ["day", "month"])
def test_time_grain_sql_is_executable_in_demo(service, warehouse, grain):
    service.model.datasets["interactions"].preferred_warehouse = warehouse
    spec = QuerySpec.model_validate(
        {"metrics": [{"name": "calls_count"}], "group_by": [{"name": "interaction_date", "grain": grain}]}
    )
    data = DemoExecutor(service.settings).execute(service.compiler.compile(spec), "req_test")
    assert sum(row[1] for row in data.rows) == 8


def test_metric_filters_cannot_be_removed_by_query_filter(service):
    spec = QuerySpec.model_validate(
        {
            "metrics": [{"name": "calls_count"}],
            "filters": [{"dimension": "queue_type", "operator": "eq", "values": ["TEST"]}],
        }
    )
    data = DemoExecutor(service.settings).execute(service.compiler.compile(spec), "req_test")
    assert data.rows == [[0]]


def test_unavailable_dimension_combination_and_binding_fail_closed(service):
    spec = QuerySpec.model_validate(
        {"metrics": [{"name": "calls_count"}], "group_by": [{"name": "interaction_medium"}]}
    )
    del service.model.datasets["interactions"].bindings["snowflake"]
    with pytest.raises(CannotAnswer):
        service.compiler.compile(spec)


def test_complex_converter_fallback_is_rejected(tmp_path):
    document = yaml.safe_load(open("examples/cx.ossie.yaml", encoding="utf-8"))
    document["semantic_model"][0]["metrics"][0]["expression"]["dialects"][0]["expression"] = (
        "SUM(interactions.hold_seconds) + 5"
    )
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported aggregate"):
        SemanticModel(path)


def test_multiple_defaults_are_rejected(tmp_path):
    document = yaml.safe_load(open("examples/cx.ossie.yaml", encoding="utf-8"))
    target = document["semantic_model"][0]["metrics"][2]["custom_extensions"][0]
    extension = json.loads(target["data"])
    extension["cx_mcp"]["is_default"] = True
    target["data"] = json.dumps(extension)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="multiple defaults"):
        SemanticModel(path)


def test_computed_aggregate_input_cannot_be_silently_misbound(tmp_path):
    from pathlib import Path

    document = yaml.safe_load(Path("examples/cx.ossie.yaml").read_text(encoding="utf-8"))
    fields = document["semantic_model"][0]["datasets"][0]["fields"]
    field = next(f for f in fields if f["name"] == "handle_seconds")
    field["expression"]["dialects"][0]["expression"] = "hold_seconds + talk_seconds"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="same-named physical columns"):
        SemanticModel(path)
