import re
import threading
from dataclasses import dataclass
from datetime import datetime, time

import sqlglot
from metricflow.engine.metricflow_engine import (
    MetricFlowEngine,
    MetricFlowQueryRequest,
    OutputColumnOrderMode,
)
from metricflow.protocols.sql_client import SqlEngine
from metricflow.sql.render.snowflake import SnowflakeSqlPlanRenderer
from metricflow_semantic_interfaces.implementations.filters.where_filter import (
    PydanticWhereFilter,
    PydanticWhereFilterIntersection,
)
from metricflow_semantic_interfaces.validations.semantic_manifest_validator import SemanticManifestValidator
from metricflow_semantics.model.semantic_manifest_lookup import SemanticManifestLookup
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from semantic_mcp.contracts import CannotAnswer

COMPILE_LOCK = threading.Lock()


def canonical_sql(sql):
    """Normalize compiler-generated alias IDs, which otherwise vary with planner cache warmth."""
    tree = sqlglot.parse_one(sql, read="snowflake")
    aliases = {}
    for alias in tree.find_all(exp.TableAlias):
        name = alias.name
        if re.fullmatch(r"(?:subq|cte)_\d+|[a-z0-9_]+_src_\d+", name):
            aliases.setdefault(name, f"mcp_alias_{len(aliases)}")
    for alias in tree.find_all(exp.TableAlias):
        if alias.name in aliases:
            alias.set("this", exp.to_identifier(aliases[alias.name]))
    for column in tree.find_all(exp.Column):
        if column.table in aliases:
            column.set("table", exp.to_identifier(aliases[column.table]))
    for table in tree.find_all(exp.Table):
        if not table.db and not table.catalog and table.name in aliases:
            table.set("this", exp.to_identifier(aliases[table.name]))
    return tree.sql(dialect="snowflake", comments=False)


class CompileOnlyClient:
    """MetricFlow gets a renderer, never a live connection or an execution capability."""

    sql_engine_type = SqlEngine.SNOWFLAKE
    sql_plan_renderer = SnowflakeSqlPlanRenderer()

    def query(self, *args, **kwargs):
        raise RuntimeError("MetricFlow is compile-only")

    execute = dry_run = query


def literal(value):
    if isinstance(value, bool):
        return exp.Boolean(this=value).sql()
    if isinstance(value, (int, float)):
        return exp.Literal.number(value).sql()
    return exp.Literal.string(value).sql()


def render_filter(item, compiler_name):
    # Names come only from the compiler catalogue; values become escaped SQL literals.
    if item.operator in {"eq", "ne", "gt", "gte", "lt", "lte"}:
        if len(item.values) != 1:
            raise CannotAnswer("invalid_filter_value", "This comparison needs exactly one filter value.")
        operator = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[item.operator]
        return f"{{{{ Dimension('{compiler_name}') }}}} {operator} {literal(item.values[0])}"
    operator = "IN" if item.operator == "in" else "NOT IN"
    return f"{{{{ Dimension('{compiler_name}') }}}} {operator} ({', '.join(literal(v) for v in item.values)})"


@dataclass
class CompiledQuery:
    sql: str
    warehouse: str
    output_names: list[str]
    compiler_names: list[str]
    effective_limit: int
    bindings: list[str]


class Compiler:
    def __init__(self, model, row_cap, default_warehouse):
        self.model = model
        self.row_cap = row_cap
        self.default_warehouse = default_warehouse
        # MetricFlow resets global ID counters. Serialize compilation to guarantee stable aliases.
        self.lock = COMPILE_LOCK
        self.engines = {}
        self.dimension_mapping = {}
        manifests = {w: model.manifest_for(w) for w in model.time_spine["bindings"]}
        for warehouse, manifest in manifests.items():
            initial = MetricFlowEngine(SemanticManifestLookup(manifest), CompileOnlyClient())
            mapping = {}
            for name in model.metrics:
                dimensions = initial.list_dimensions(metric_names=[name])
                available = {}
                for exposed, obj in model.dimensions.items():
                    candidates = [
                        d.dunder_name
                        for d in dimensions
                        if d.name == exposed
                        and d.semantic_model_reference
                        and d.semantic_model_reference.semantic_model_name == obj["dataset"]
                    ]
                    if len(candidates) == 1:
                        available[exposed] = candidates[0]
                mapping[name] = available
                model.metrics[name]["dimensions"] = sorted(available)
            # Compile intrinsic filters on each metric; native ratio filter applies to both leaves.
            for metric in manifest.metrics:
                if metric.name in model.metrics:
                    from semantic_mcp.contracts import QueryFilter

                    filters = []
                    for raw_filter in model.metrics[metric.name]["intrinsic_filters"]:
                        item = QueryFilter.model_validate(raw_filter)
                        from semantic_mcp.validator import validate_filter

                        if item.dimension not in model.dimensions:
                            raise ValueError(f"Metric {metric.name}: unknown intrinsic filter dimension")
                        validate_filter(item, model.dimensions[item.dimension])
                        if item.dimension not in mapping[metric.name]:
                            raise ValueError(
                                f"Metric {metric.name}: intrinsic filter dimension is unavailable"
                            )
                        filters.append(
                            PydanticWhereFilter(
                                where_sql_template=render_filter(item, mapping[metric.name][item.dimension])
                            )
                        )
                    if filters:
                        metric.filter = PydanticWhereFilterIntersection(where_filters=filters)
            validator = SemanticManifestValidator()
            results = validator.validate_semantic_manifest(manifest)
            if results.has_blocking_issues:
                raise ValueError(
                    "Compiler model validation failed: " + "\n".join(str(e) for e in results.errors)
                )
            self.engines[warehouse] = MetricFlowEngine(SemanticManifestLookup(manifest), CompileOnlyClient())
            self.dimension_mapping[warehouse] = mapping

    def route(self, spec):
        dataset_names = {ds for metric in spec.metrics for ds in self.model.metrics[metric.name]["datasets"]}
        targets = {
            self.model.datasets[ds].preferred_warehouse or self.default_warehouse for ds in dataset_names
        }
        if len(targets) != 1:
            raise CannotAnswer(
                "unsupported_combination", "This question requires data from multiple warehouses."
            )
        warehouse = targets.pop()
        required = dataset_names | {self.model.dimensions[g.name]["dataset"] for g in spec.group_by}
        required |= {self.model.dimensions[f.dimension]["dataset"] for f in spec.filters}
        if warehouse not in self.engines or any(
            warehouse not in self.model.datasets[ds].bindings for ds in required
        ):
            raise CannotAnswer(
                "unsupported_combination", "The selected warehouse lacks a required model binding."
            )
        return warehouse

    def compile(self, spec, start=None, end=None):
        warehouse = self.route(spec)
        maps = [self.dimension_mapping[warehouse][m.name] for m in spec.metrics]

        def dimension_name(name):
            names = {mapping.get(name) for mapping in maps}
            if None in names or len(names) != 1:
                raise CannotAnswer(
                    "unsupported_combination", f"Dimension {name} is unavailable for these metrics."
                )
            return names.pop()

        groups = [
            dimension_name(g.name)
            + (f"__{g.grain or 'day'}" if self.model.dimensions[g.name]["is_time"] else "")
            for g in spec.group_by
        ]
        output_names = [g.name for g in spec.group_by] + [m.name for m in spec.metrics]
        compiler_names = groups + [m.name for m in spec.metrics]
        orders = []
        for item in spec.order_by:
            target = compiler_names[output_names.index(item.name)]
            orders.append(("-" if item.descending else "") + target)
        if not orders:
            # Stable ordering is needed for capped and saved queries.
            orders = groups or [m.name for m in spec.metrics]
        limit = min(spec.limit, self.row_cap)
        request = MetricFlowQueryRequest.create(
            metric_names=[m.name for m in spec.metrics],
            group_by_names=groups,
            limit=limit + 1,
            order_by_names=orders,
            time_constraint_start=datetime.combine(start, time.min) if start else None,
            time_constraint_end=datetime.combine(end, time.max) if end else None,
            where_constraints=[render_filter(f, dimension_name(f.dimension)) for f in spec.filters],
            output_column_order_mode=OutputColumnOrderMode.INPUT_ORDER,
        )
        try:
            with self.lock:
                sql = self.engines[warehouse].explain(request).sql_statement.sql
            sql = canonical_sql(sql)
            if warehouse == "teradata":
                sql = sqlglot.transpile(
                    sql, read="snowflake", write="teradata", unsupported_level=ErrorLevel.RAISE
                )[0]
            # Fail closed: a single SELECT with only declared tables, never client-authored SQL.
            statements = sqlglot.parse(sql, read=warehouse)
            if len(statements) != 1 or not isinstance(statements[0], exp.Select):
                raise ValueError("Compiler must emit one SELECT")
            tree = statements[0]
            if any(
                tree.find_all(exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Into)
            ):
                raise ValueError("Non-read-only SQL rejected")
            bindings = sorted({t.sql(dialect=warehouse).split(" AS ")[0] for t in tree.find_all(exp.Table)})
            ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
            allowed = {
                d.bindings[warehouse].casefold()
                for d in self.model.datasets.values()
                if warehouse in d.bindings
            }
            allowed.add(self.model.time_spine["bindings"][warehouse].casefold())
            for table in tree.find_all(exp.Table):
                reference = ".".join(filter(None, [table.catalog, table.db, table.name]))
                if reference.casefold() not in allowed and reference not in ctes:
                    raise ValueError("SQL refers to an undeclared warehouse binding")
            return CompiledQuery(sql, warehouse, output_names, compiler_names, limit, bindings)
        except CannotAnswer:
            raise
        except Exception as exc:
            # Compiler details remain in server audit; don't expose infrastructure internals to clients.
            raise CannotAnswer(
                "unsupported_combination", "The compiler cannot safely execute this combination."
            ) from exc
