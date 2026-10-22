"""
Point-in-time (as-of) reads from the vintage-aware observations table.

"As of date D" = the value that was publicly known on D: the latest
vintage with vintage_date <= D.

Rows with vintage_basis = 'retrieval_date' are EXCLUDED: they only prove a
value was current when we downloaded it, not on any earlier date. Using
them in an as-of query would leak later revisions into the past
(look-ahead bias).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

RELEASE_TZ = ZoneInfo("America/New_York")
POINT_IN_TIME_BASES = ("source_realtime", "source_realtime_clipped", "release_date")


@dataclass(frozen=True)
class Vintage:
    indicator_id: str
    observation_date: str
    vintage_date: str
    vintage_basis: str
    value: float | None
    value_status: str
    fetch_id: int          # -> fetch_log -> raw snapshot (path + SHA-256)

    def as_dict(self) -> dict:
        return {"indicator_id": self.indicator_id, "observation_date": self.observation_date,
                "vintage_date": self.vintage_date, "vintage_basis": self.vintage_basis,
                "value": self.value, "fetch_id": self.fetch_id}


_SELECT = """
    SELECT indicator_id, observation_date, vintage_date, vintage_basis, value, value_status,
           fetch_id
    FROM observations
    WHERE indicator_id = ? AND observation_date = ?
      AND vintage_basis IN ('source_realtime', 'source_realtime_clipped', 'release_date')
"""


def value_as_of(conn: sqlite3.Connection, indicator_id: str, observation_date: str,
                as_of_date: str) -> Vintage | None:
    row = conn.execute(_SELECT + " AND vintage_date <= ? ORDER BY vintage_date DESC LIMIT 1",
                       (indicator_id, observation_date, as_of_date)).fetchone()
    return Vintage(*row) if row else None


def first_vintage(conn: sqlite3.Connection, indicator_id: str,
                  observation_date: str) -> Vintage | None:
    row = conn.execute(_SELECT + " ORDER BY vintage_date ASC LIMIT 1",
                       (indicator_id, observation_date)).fetchone()
    return Vintage(*row) if row else None


def later_vintage_dates(conn: sqlite3.Connection, indicator_id: str,
                        observation_dates: list[str], after: str) -> list[str]:
    marks = ",".join("?" * len(observation_dates))
    rows = conn.execute(f"""
        SELECT DISTINCT vintage_date FROM observations
        WHERE indicator_id = ? AND observation_date IN ({marks}) AND vintage_date > ?
          AND vintage_basis IN ('source_realtime', 'source_realtime_clipped', 'release_date')
        ORDER BY vintage_date
    """, (indicator_id, *observation_dates, after)).fetchall()
    return [r[0] for r in rows]


def release_date_local(release_time_utc: str) -> str:
    """'2026-09-11T12:30:00Z' -> '2026-09-11' (date in America/New_York)."""
    dt = datetime.strptime(release_time_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(RELEASE_TZ).strftime("%Y-%m-%d")
