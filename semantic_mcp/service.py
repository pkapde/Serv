import asyncio
import hashlib
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from semantic_mcp.compiler import Compiler
from semantic_mcp.contracts import CannotAnswer, Column, QueryResponse, QuerySpec
from semantic_mcp.model import SemanticModel
from semantic_mcp.resolver import Resolver
from semantic_mcp.store import Store, new_id, timestamp
from semantic_mcp.time_ranges import resolve_time_range
from semantic_mcp.validator import validate_spec
from semantic_mcp.warehouses import executor_for

logger = logging.getLogger(__name__)


class QueryService:
    def __init__(self, settings):
        self.settings = settings
        self.model = SemanticModel(settings.model_path)
        self.compiler = Compiler(self.model, settings.row_cap, settings.default_warehouse)
        self.store = Store(settings.database_url)
        self.resolver = Resolver(self.model, settings)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        self.resolver_lock = asyncio.Lock()

    async def close(self):
        await self.resolver.close()
        self.store.close()

    async def ask_question(self, question, caller):
        request_id = new_id("req")
        started = time.monotonic()
        audit = {"operation": "ask_question", "question": question, "llm_calls": 0}
        try:
            if not isinstance(question, str) or not question.strip() or len(question) > 4000:
                raise CannotAnswer("out_of_scope", "Question must contain 1 to 4000 characters.")
            key = self.resolver.cache_key(question, self.model.hash)
            # Prevent duplicate LLM calls locally; the durable unique cache key also converges across instances.
            async with self.resolver_lock:
                resolution = self.store.cached(key)
                audit["cache_hit"] = resolution is not None
                if resolution is None:
                    resolution = await self.resolver.resolve(question, audit)
                    audit["attempted_resolver_output"] = resolution.model_dump(mode="json")
                    resolution = self.store.cache_put(key, resolution)
            audit["resolver_output"] = resolution.model_dump(mode="json")
            if resolution.outcome != "resolved":
                candidates = [name for name in resolution.candidates if name in self.model.metrics]
                reason = {
                    "ambiguous": "ambiguous_no_default",
                    "no_match": "no_matching_metric",
                    "out_of_scope": "out_of_scope",
                }[resolution.outcome]
                raise CannotAnswer(reason, resolution.rationale, candidates)
            spec, defaults = validate_spec(resolution.spec, self.model, question)
            audit["validator_result"] = "accepted"
            audit["validated_spec"] = spec.model_dump(mode="json")
            response = await self._execute(spec, defaults, request_id, audit)
            response.resolution_id = self.store.record_resolution(
                caller,
                self.model.hash,
                {
                    "spec": spec.model_dump(mode="json"),
                    "interpretation": response.interpretation,
                    "original_question": question,
                },
            )
        except CannotAnswer as exc:
            audit.setdefault("validator_result", "rejected")
            response = QueryResponse(
                status="cannot_answer",
                request_id=request_id,
                reason_code=exc.reason_code,
                message=str(exc),
                candidates=exc.candidates,
            )
        except Exception as exc:
            logger.warning("Request %s failed (%s)", request_id, type(exc).__name__)
            audit["error_type"] = type(exc).__name__
            response = QueryResponse(
                status="error",
                request_id=request_id,
                reason_code="execution_error",
                message="The request could not be completed. Use the request ID to investigate.",
            )
        return self._finish(response, audit, caller, started)

    async def run_saved(self, saved_id, caller):
        request_id = new_id("req")
        started = time.monotonic()
        audit = {"operation": "run_saved_query", "saved_query_id": saved_id, "llm_calls": 0}
        try:
            saved = self.store.get_saved(saved_id, caller)
            if saved["model_file_hash"] != self.model.hash:
                raise CannotAnswer(
                    "model_changed", "The saved query belongs to another model version; resolve it again."
                )
            spec, defaults = validate_spec(
                QuerySpec.model_validate(saved["spec"]), self.model, saved["original_question"]
            )
            audit["validated_spec"] = spec.model_dump(mode="json")
            audit["validator_result"] = "accepted"
            response = await self._execute(spec, defaults, request_id, audit)
        except CannotAnswer as exc:
            response = QueryResponse(
                status="cannot_answer",
                request_id=request_id,
                reason_code=exc.reason_code,
                message=str(exc),
                candidates=exc.candidates,
            )
        except Exception as exc:
            logger.warning("Saved query %s failed (%s)", request_id, type(exc).__name__)
            audit["error_type"] = type(exc).__name__
            response = QueryResponse(
                status="error",
                request_id=request_id,
                reason_code="execution_error",
                message="The saved query could not be completed. Use the request ID to investigate.",
            )
        return self._finish(response, audit, caller, started)

    async def save_query(self, resolution_id, name, caller):
        request_id = new_id("req")
        audit = {"operation": "save_query", "resolution_id": resolution_id, "llm_calls": 0}
        try:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
                raise CannotAnswer("out_of_scope", "Saved query name must contain 1 to 200 characters.")
            result = self.store.save(resolution_id, name.strip(), caller, self.model.hash)
            result.update({"status": "answered", "request_id": request_id})
        except CannotAnswer as exc:
            result = QueryResponse(
                status="cannot_answer", request_id=request_id, reason_code=exc.reason_code, message=str(exc)
            ).model_dump(mode="json")
        except Exception as exc:
            audit["error_type"] = type(exc).__name__
            result = QueryResponse(
                status="error",
                request_id=request_id,
                reason_code="storage_error",
                message="The query could not be saved.",
            ).model_dump(mode="json")
        audit["status"] = result["status"]
        try:
            self.store.log(request_id, caller, audit)
        except Exception:
            logger.error("Audit storage failed for request %s", request_id)
            return QueryResponse(
                status="error",
                request_id=request_id,
                reason_code="audit_unavailable",
                message="The operation could not be audited.",
            ).model_dump(mode="json")
        return result

    async def _execute(self, spec, defaults, request_id, audit):
        start, end, calendar = resolve_time_range(spec.time_range, self.model.calendar)
        # Include queue wait in the cap; warehouse timeout is separately enforced by each driver.
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=self.settings.query_timeout_seconds)
        except TimeoutError:
            raise CannotAnswer("capacity_exceeded", "The query service is busy; retry later.") from None
        try:
            compiled = await asyncio.to_thread(self.compiler.compile, spec, start, end)
            audit.update({"generated_sql": compiled.sql, "warehouse": compiled.warehouse})
            executor = executor_for(self.settings, compiled.warehouse)
            result = await asyncio.to_thread(executor.execute, compiled, request_id)
        finally:
            self.semaphore.release()
        # Use column identities rather than trusting warehouse ordering.
        if set(result.names) != set(compiled.compiler_names):
            raise RuntimeError("Warehouse output does not match the compiled response schema")
        indices = [result.names.index(name) for name in compiled.compiler_names]
        rows = [[row[i] for i in indices] for row in result.rows]
        columns = [
            Column(name=g.name, role="dimension", type=self.model.dimensions[g.name]["type"])
            for g in spec.group_by
        ]
        columns += [
            Column(
                name=m.name,
                role="metric",
                type=self.model.metrics[m.name]["type"],
                unit=self.model.metrics[m.name]["unit"],
                additivity=self.model.metrics[m.name]["additivity"],
            )
            for m in spec.metrics
        ]
        today = datetime.now(ZoneInfo(self.model.calendar.get("timezone", "UTC"))).date()
        filters = [{"source": "question", **f.model_dump(mode="json")} for f in spec.filters]
        caveats = []
        metrics = []
        choices = []
        for selected in spec.metrics:
            metric = self.model.metrics[selected.name]
            metrics.append({"name": selected.name, "label": metric["label"], "owner": metric["owner"]})
            choices.append({"metric": selected.name, "basis": selected.basis, "evidence": selected.evidence})
            filters.extend(
                {"source": "metric definition", "metric": selected.name, **f}
                for f in metric["intrinsic_filters"]
            )
            caveats.extend(
                {"metric": selected.name, **c}
                for c in metric["caveats"]
                if date.fromisoformat(c["expires"]) >= today
            )
        interpretation = ", ".join(self.model.metrics[m.name]["label"] for m in spec.metrics)
        if spec.group_by:
            interpretation += " by " + ", ".join(
                self.model.dimensions[g.name]["label"]
                + (f" ({g.grain or 'day'})" if self.model.dimensions[g.name]["is_time"] else "")
                for g in spec.group_by
            )
        interpretation += f"; {start} to {end} ({calendar})" if start else "; all time"
        if spec.filters:
            interpretation += "; " + "; ".join(
                f"{self.model.dimensions[f.dimension]['label']} {f.operator} {f.values}" for f in spec.filters
            )
        provenance = {
            "model_file_hash": self.model.hash,
            "metrics": metrics,
            "metric_choices": choices,
            "defaults_applied": defaults,
            "filters_applied": filters,
            "time_range": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "calendar": calendar,
                "relative_spec": spec.time_range.model_dump(mode="json"),
            },
            "caveats": caveats,
            "warehouse": compiled.warehouse,
            "source_bindings": compiled.bindings,
            "warehouse_query_id": result.query_id,
            "sql_hash": "sha256:" + hashlib.sha256(compiled.sql.encode()).hexdigest(),
            "executed_at": timestamp(),
            "execution_mode": self.settings.execution_mode,
            "resolver_mode": self.settings.resolver_mode,
            "llm_calls": audit["llm_calls"],
            "effective_limit": compiled.effective_limit,
            "percent_representation": "fraction",
        }
        audit.update(
            {
                "row_count": len(rows),
                "warehouse_query_id": result.query_id,
                "truncated": result.truncated,
                "provenance": provenance,
            }
        )
        return QueryResponse(
            status="answered",
            request_id=request_id,
            interpretation=interpretation,
            columns=columns,
            rows=rows,
            truncated=result.truncated,
            provenance=provenance,
        )

    def _finish(self, response, audit, caller, started):
        audit.update(
            {
                "status": response.status,
                "reason_code": response.reason_code,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "model_hash": self.model.hash,
            }
        )
        # Audit failures fail the operation rather than returning an unaudited analytical answer.
        try:
            self.store.log(response.request_id, caller, audit)
        except Exception:
            logger.error("Audit storage failed for request %s", response.request_id)
            return QueryResponse(
                status="error",
                request_id=response.request_id,
                reason_code="audit_unavailable",
                message="The operation could not be audited.",
            ).model_dump(mode="json")
        return response.model_dump(mode="json")
