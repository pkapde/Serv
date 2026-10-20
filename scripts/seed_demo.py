"""Create deterministic synthetic warehouse data without touching real warehouses."""

from datetime import date, timedelta
from pathlib import Path

import duckdb


def seed(path=Path("var/warehouse.duckdb")):
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                interaction_id INTEGER PRIMARY KEY, interaction_date DATE, interaction_medium TEXT,
                queue_type TEXT, fiscal_month TEXT, hold_seconds REAL, handle_seconds REAL,
                talk_seconds REAL, resolved_1d INTEGER, resolved_7d INTEGER);
            CREATE TABLE IF NOT EXISTS date_spine (date_day DATE PRIMARY KEY);
            DELETE FROM interactions;
            DELETE FROM date_spine;
        """)
        rows = [
            (1, "2026-09-01", "Call", "LIVE", "2026-P09", 10, 100, 80, 1, 1),
            (2, "2026-09-02", "Call", "LIVE", "2026-P09", 90, 300, 180, 0, 1),
            (3, "2026-09-03", "Chat", "LIVE", "2026-P09", 20, 200, 160, 1, 1),
            (4, "2026-09-04", "Chat", "LIVE", "2026-P09", 0, 0, 0, 0, 0),
            (5, "2026-09-05", "Email", "LIVE", "2026-P09", 0, 0, 0, 1, 1),
            (6, "2026-09-06", "Call", "TEST", "2026-P09", 9999, 10000, 100, 0, 0),
            (7, "2026-10-01", "Call", "LIVE", "2026-P10", 20, 100, 60, 1, 1),
            (8, "2026-10-02", "Chat", "LIVE", "2026-P10", 30, 150, 100, 0, 1),
            (9, "2026-08-10", "Call", "LIVE", "2026-P08", 5, 50, 35, 1, 1),
        ]
        conn.executemany("INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        start, end = date(2026, 1, 1), date(2027, 12, 31)
        conn.executemany(
            "INSERT INTO date_spine VALUES (?)",
            [((start + timedelta(days=i)).isoformat(),) for i in range((end - start).days + 1)],
        )
    return path


if __name__ == "__main__":
    print(f"Created synthetic demo warehouse: {seed().resolve()}")
