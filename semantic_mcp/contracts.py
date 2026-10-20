from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MetricSelection(StrictModel):
    name: str
    basis: Literal["explicit", "default"] = "explicit"
    # Required for a variant selected explicitly: verbatim phrase from the question.
    evidence: str | None = None


class GroupBy(StrictModel):
    name: str
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None


class QueryFilter(StrictModel):
    dimension: str
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"] = "eq"
    values: list[str | int | float | bool] = Field(min_length=1, max_length=100)


class TimeRange(StrictModel):
    kind: Literal[
        "all_time",
        "absolute",
        "previous_month",
        "previous_fiscal_month",
        "current_month",
        "current_fiscal_month",
        "last_n_days",
    ] = "all_time"
    start: date | None = None
    end: date | None = None
    days: int | None = Field(default=None, ge=1, le=3660)

    @model_validator(mode="after")
    def valid_range(self):
        if self.kind == "absolute":
            if not self.start or not self.end or self.start > self.end:
                raise ValueError("Absolute dates require start <= end; both endpoints inclusive")
        elif self.start or self.end:
            raise ValueError("Relative time ranges must not contain fixed dates")
        if (self.kind == "last_n_days") != (self.days is not None):
            raise ValueError("days is required only for last_n_days")
        return self


class Ordering(StrictModel):
    name: str
    descending: bool = False


class QuerySpec(StrictModel):
    metrics: list[MetricSelection] = Field(min_length=1, max_length=10)
    group_by: list[GroupBy] = Field(default_factory=list, max_length=10)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=20)
    time_range: TimeRange = Field(default_factory=TimeRange)
    order_by: list[Ordering] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=5000, ge=1, le=50000)


class Resolution(StrictModel):
    outcome: Literal["resolved", "ambiguous", "no_match", "out_of_scope"]
    spec: QuerySpec | None = None
    rationale: str = Field(min_length=1, max_length=2000)
    candidates: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def spec_required(self):
        if (self.outcome == "resolved") != (self.spec is not None):
            raise ValueError("Only resolved outcomes carry a query spec")
        return self


class Column(StrictModel):
    name: str
    role: Literal["dimension", "metric"]
    type: str
    unit: str | None = None
    additivity: str | None = None


class QueryResponse(StrictModel):
    status: Literal["answered", "cannot_answer", "error"]
    request_id: str
    resolution_id: str | None = None
    interpretation: str | None = None
    columns: list[Column] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    truncated: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)
    reason_code: str | None = None
    message: str | None = None
    candidates: list[str] = Field(default_factory=list)


class QueryExecutionError(RuntimeError):
    """An execution failure with an explicitly safe public explanation."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class CannotAnswer(Exception):
    def __init__(self, reason_code: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.candidates = candidates or []
