from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from semantic_mcp.contracts import CannotAnswer, TimeRange


def resolve_time_range(value: TimeRange, calendar: dict, today: date | None = None):
    today = today or datetime.now(ZoneInfo(calendar.get("timezone", "UTC"))).date()
    if value.kind == "all_time":
        return None, None, "all_time"
    if value.kind == "absolute":
        return value.start, value.end, "absolute"
    if value.kind == "last_n_days":
        return today - timedelta(days=value.days - 1), today, "calendar"
    if "fiscal" in value.kind:
        periods = calendar.get("periods", [])
        current = next(
            (
                i
                for i, p in enumerate(periods)
                if date.fromisoformat(p["start"]) <= today <= date.fromisoformat(p["end"])
            ),
            None,
        )
        if current is None or (value.kind == "previous_fiscal_month" and current == 0):
            raise CannotAnswer("out_of_scope", "The fiscal calendar does not cover this requested period.")
        period = periods[current - 1 if value.kind == "previous_fiscal_month" else current]
        return date.fromisoformat(period["start"]), date.fromisoformat(period["end"]), "fiscal"
    first = today.replace(day=1)
    if value.kind == "previous_month":
        end = first - timedelta(days=1)
        return end.replace(day=1), end, "calendar"
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, next_month - timedelta(days=1), "calendar"
