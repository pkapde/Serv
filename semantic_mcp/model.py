"""Load the pinned Ossie 0.1.1 format with explicit cx_mcp governance extensions.

MetricFlow's native converter is used. Its approximate-expression fallback is rejected.
"""

import copy
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlglot
import yaml
from metricflow.converters.models import OSIDocument
from metricflow.converters.osi_to_msi import OSIToMSIConverter
from pydantic import Field, ValidationError
from sqlglot import exp

from semantic_mcp.contracts import CannotAnswer, QueryFilter, StrictModel

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
SOURCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,2}$")


class Caveat(StrictModel):
    text: str = Field(min_length=1)
    expires: date


class MetricMetadata(StrictModel):
    label: str
    synonyms: list[str] = Field(default_factory=list)
    owner: str = Field(min_length=1)
    unit: str = "number"
    additivity: Literal["additive", "non_additive", "semi_additive"]
    variant_group: str | None = None
    is_default: bool = False
    explicit_terms: list[str] = Field(default_factory=list)
    caveats: list[Caveat] = Field(default_factory=list)
    intrinsic_filters: list[QueryFilter] = Field(default_factory=list)
    # For snapshot metrics: compiler selects min/max time before aggregating.
    non_additive_dimension: str | None = None
    window_choice: Literal["min", "max"] = "max"
    window_groupings: list[str] = Field(default_factory=list)


class DimensionMetadata(StrictModel):
    synonyms: list[str] = Field(default_factory=list)
    allowed_values: list[str | int | float | bool] | None = None
    exposed: bool = False


class DatasetMetadata(StrictModel):
    time_dimension: str
    preferred_warehouse: Literal["snowflake", "teradata"] | None = None
    bindings: dict[Literal["snowflake", "teradata"], str]


def extension(obj):
    found = []
    for ext in obj.get("custom_extensions", []):
        data = json.loads(ext["data"])
        if "cx_mcp" in data:
            found.append(data["cx_mcp"])
    if len(found) > 1:
        raise ValueError("Duplicate cx_mcp extensions")
    return found[0] if found else {}


def safe_metric_expression(expression, dataset_fields):
    """Admit only native-converter patterns whose meaning survives conversion exactly."""
    node = sqlglot.parse_one(expression)

    def aggregate(value):
        while isinstance(value, exp.Paren):
            value = value.this
        if not isinstance(value, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)):
            raise ValueError("Unsupported aggregate expression; approximation is forbidden")
        inner = value.this
        if isinstance(inner, exp.Distinct) and isinstance(value, exp.Count) and len(inner.expressions) == 1:
            inner = inner.expressions[0]
        if not isinstance(inner, exp.Column) or not inner.table:
            raise ValueError("Aggregate inputs must be dataset-qualified field references")
        if inner.table not in dataset_fields or inner.name not in dataset_fields[inner.table]:
            raise ValueError(f"Unknown metric source field: {inner.sql()}")
        if inner.db or inner.catalog:
            raise ValueError("Metric expressions reference logical datasets, not physical tables")

    if isinstance(node, exp.Div):
        aggregate(node.this)
        aggregate(node.expression)
    else:
        aggregate(node)
    return node


class SemanticModel:
    def __init__(self, path: Path):
        raw_bytes = path.read_bytes()
        self.hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        raw = yaml.safe_load(raw_bytes)
        errors = []
        if not isinstance(raw, dict) or raw.get("version") != "0.1.1":
            raise ValueError("This build pins Ossie 0.1.1; other schema versions must be migrated explicitly")
        models = raw.get("semantic_model", [])
        if not isinstance(models, list) or len(models) != 1:
            raise ValueError("The pilot requires exactly one semantic model")
        body = models[0]
        self.name = body.get("name", "")
        self.metrics = {}
        self.dimensions = {}
        self.datasets = {}
        self.dataset_fields = {}
        self.groups = {}
        self.raw_body = body
        model_ext = extension(body)
        if set(model_ext) - {"calendar", "time_spine"}:
            errors.append("Unknown model cx_mcp extension fields")
        self.calendar = model_ext.get("calendar", {})
        try:
            ZoneInfo(self.calendar.get("timezone", "UTC"))
        except (ZoneInfoNotFoundError, TypeError):
            errors.append("Calendar timezone must be a valid IANA timezone")
        self.time_spine = model_ext.get("time_spine", {})
        if not self.time_spine.get("bindings") or not IDENTIFIER.fullmatch(self.time_spine.get("column", "")):
            errors.append("A time_spine column and physical bindings are required by MetricFlow")
        for source in self.time_spine.get("bindings", {}).values():
            if not SOURCE.fullmatch(source):
                errors.append(f"Invalid time spine table reference: {source}")
        previous_end = None
        for period in self.calendar.get("periods", []):
            try:
                start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
                if start > end or (previous_end and start != previous_end + timedelta(days=1)):
                    errors.append("Fiscal periods must be ordered, contiguous and non-overlapping")
                previous_end = end
            except (KeyError, ValueError):
                errors.append("Fiscal periods require valid start and end dates")
        for dataset in body.get("datasets", []):
            name = dataset.get("name", "")
            try:
                if name in self.datasets or not IDENTIFIER.fullmatch(name):
                    raise ValueError("Dataset names must be unique lowercase identifiers")
                metadata = DatasetMetadata.model_validate(extension(dataset))
                if not SOURCE.fullmatch(dataset["source"]):
                    raise ValueError("Dataset source must be a physical table/view reference")
                if metadata.preferred_warehouse and metadata.preferred_warehouse not in metadata.bindings:
                    raise ValueError("Preferred warehouse requires a matching physical binding")
                for source in metadata.bindings.values():
                    if not SOURCE.fullmatch(source):
                        raise ValueError("Only physical table/view bindings are accepted")
                self.datasets[name] = metadata
                fields = dataset.get("fields", [])
                self.dataset_fields[name] = {f["name"] for f in fields}
                if len(self.dataset_fields[name]) != len(fields):
                    raise ValueError("Duplicate field names")
                if metadata.time_dimension not in self.dataset_fields[name]:
                    raise ValueError("Dataset time dimension is missing")
                for field in fields:
                    if not IDENTIFIER.fullmatch(field["name"]):
                        raise ValueError("Field names must be lowercase identifiers")
                    fm = DimensionMetadata.model_validate(extension(field))
                    expressions = field["expression"]["dialects"]
                    if len(expressions) != 1 or expressions[0]["dialect"] != "ANSI_SQL":
                        raise ValueError("Fields require one ANSI_SQL expression in this pilot")
                    field_expression = sqlglot.parse_one(expressions[0]["expression"])
                    if field_expression.find(exp.Query) or field_expression.find(exp.Command):
                        raise ValueError("Field expressions must be scalar, not queries or commands")
                    if fm.exposed:
                        key = field["name"]
                        if key in self.dimensions:
                            raise ValueError("Exposed dimension names must be unique across datasets")
                        self.dimensions[key] = {
                            "name": key,
                            "label": field.get("label", key),
                            "description": field.get("description", ""),
                            "dataset": name,
                            "type": field.get("datatype", "String").lower(),
                            "is_time": field.get("dimension", {}).get("is_time", False),
                            **fm.model_dump(),
                        }
            except (ValueError, KeyError, ValidationError) as exc:
                errors.append(f"Dataset {name}: {exc}")
        for metric in body.get("metrics", []):
            name = metric.get("name", "")
            try:
                if name in self.metrics or not IDENTIFIER.fullmatch(name):
                    raise ValueError("Metric names must be unique lowercase identifiers")
                metadata = MetricMetadata.model_validate(extension(metric))
                expressions = metric["expression"]["dialects"]
                if len(expressions) != 1 or expressions[0]["dialect"] != "ANSI_SQL":
                    raise ValueError("Pilot metrics require exactly one ANSI_SQL definition")
                node = safe_metric_expression(expressions[0]["expression"], self.dataset_fields)
                # The pinned converter binds aggregate inputs by bare name. It does not dereference
                # aliased/computed logical fields, so reject them instead of querying the wrong column.
                for reference in node.find_all(exp.Column):
                    dataset = next(d for d in body["datasets"] if d["name"] == reference.table)
                    field = next(f for f in dataset["fields"] if f["name"] == reference.name)
                    field_expression = sqlglot.parse_one(field["expression"]["dialects"][0]["expression"])
                    if (
                        not isinstance(field_expression, exp.Column)
                        or field_expression.sql() != reference.name
                    ):
                        raise ValueError("Aggregate inputs must map directly to same-named physical columns")
                if isinstance(node, (exp.Div, exp.Avg)) and metadata.additivity != "non_additive":
                    raise ValueError("Ratios and averages must be declared non_additive")
                if metadata.additivity == "semi_additive" and not metadata.non_additive_dimension:
                    raise ValueError("Snapshot metrics require a declared non_additive_dimension")
                if metadata.non_additive_dimension and metadata.additivity != "semi_additive":
                    raise ValueError("non_additive_dimension applies only to semi_additive metrics")
                if metadata.additivity == "non_additive" and isinstance(node, (exp.Sum, exp.Count)):
                    raise ValueError(
                        "Non-additive SUM/COUNT needs explicit snapshot semantics; cannot sum blindly"
                    )
                self.metrics[name] = {
                    "name": name,
                    "description": metric.get("description", ""),
                    "type": metric.get("datatype", "Decimal").lower(),
                    "datasets": sorted({c.table for c in node.find_all(exp.Column)}),
                    **metadata.model_dump(mode="json"),
                }
                if metadata.variant_group:
                    self.groups.setdefault(metadata.variant_group, []).append(name)
                elif metadata.is_default:
                    raise ValueError("Default metric must belong to a variant group")
            except (ValueError, KeyError, ValidationError, sqlglot.errors.ParseError) as exc:
                errors.append(f"Metric {name}: {exc}")
        for group, members in self.groups.items():
            if sum(self.metrics[n]["is_default"] for n in members) > 1:
                errors.append(f"Variant group {group} has multiple defaults")
        if not self.metrics:
            errors.append("At least one governed metric is required")
        for rel in body.get("relationships", []):
            if len(rel["from_columns"]) != 1 or rel["from_columns"] != rel["to_columns"]:
                errors.append(
                    f"Relationship {rel['name']}: native converter supports shared single-key entities only"
                )
        self.dimension_names = self._name_index(self.dimensions, errors, "dimension")
        self.metric_names = self._name_index(self.metrics, errors, "metric")
        if errors:
            raise ValueError("Model validation failed:\n" + "\n".join(errors))
        self.document = OSIDocument.parse_obj(raw)
        self.manifest = OSIToMSIConverter().convert(self.document).output
        self._enrich_manifest()

    @staticmethod
    def _name_index(objects, errors, kind):
        index = {}
        for name, obj in objects.items():
            for alias in {name.casefold(), obj["label"].casefold()}:
                if alias in index and index[alias] != name:
                    errors.append(f"Ambiguous {kind} name/label: {alias}")
                index[alias] = name
        return index

    def canonical(self, name, kind):
        index = self.metric_names if kind == "metric" else self.dimension_names
        if name.casefold() not in index:
            raise CannotAnswer("unknown_name", f"Unknown {kind}: {name}")
        return index[name.casefold()]

    def _enrich_manifest(self):
        from metricflow_semantic_interfaces.implementations.elements.measure import (
            PydanticNonAdditiveDimensionParameters,
        )

        for metric in self.manifest.metrics:
            params = metric.type_params.metric_aggregation_params
            if params:
                if params.agg.value == "count":
                    metric.type_params.fill_nulls_with = 0
                params.agg_time_dimension = self.datasets[params.semantic_model].time_dimension
                if metric.name in self.metrics:
                    meta = self.metrics[metric.name]
                    if meta["non_additive_dimension"]:
                        params.non_additive_dimension = PydanticNonAdditiveDimensionParameters(
                            name=meta["non_additive_dimension"],
                            window_choice=meta["window_choice"],
                            window_groupings=meta["window_groupings"],
                        )

    def manifest_for(self, warehouse):
        from metricflow_semantic_interfaces.implementations.time_spine_table_configuration import (
            PydanticTimeSpineTableConfiguration,
        )
        from metricflow_semantic_interfaces.transformations.semantic_manifest_transformer import (
            PydanticSemanticManifestTransformer,
        )

        manifest = copy.deepcopy(self.manifest)
        for sm in manifest.semantic_models:
            source = self.datasets[sm.name].bindings.get(warehouse)
            if source:
                sm.node_relation = OSIToMSIConverter._parse_source(source)
        source = self.time_spine["bindings"].get(warehouse)
        if not source:
            raise ValueError(f"No time spine binding for {warehouse}")
        manifest.project_configuration.time_spine_table_configurations = [
            PydanticTimeSpineTableConfiguration(
                location=source, column_name=self.time_spine["column"], grain="day"
            )
        ]
        return PydanticSemanticManifestTransformer.transform(manifest)

    def catalogue(self):
        return {
            "model": self.name,
            "calendar": {
                "timezone": self.calendar.get("timezone", "UTC"),
                "fiscal_calendar_available": bool(self.calendar.get("periods")),
            },
            "metrics": [
                {
                    k: v
                    for k, v in m.items()
                    if k
                    in {
                        "name",
                        "label",
                        "description",
                        "synonyms",
                        "dimensions",
                        "variant_group",
                        "is_default",
                        "explicit_terms",
                        "caveats",
                        "unit",
                        "additivity",
                    }
                }
                for m in self.metrics.values()
            ],
            "dimensions": [
                {
                    k: v
                    for k, v in d.items()
                    if k in {"name", "label", "description", "synonyms", "allowed_values", "is_time", "type"}
                }
                for d in self.dimensions.values()
            ],
        }
