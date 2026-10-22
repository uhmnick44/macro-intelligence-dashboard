"""
Database access layer: connection, schema setup, registry sync, run/fetch
logging, and vintage-aware observation inserts.

All writes go through this module so the integrity rules live in one place.
"""
from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
EXPECTED_SCHEMA_VERSION = "5"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def schema_version(conn: sqlite3.Connection) -> str | None:
    """Version recorded in the database, or None for an empty database."""
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'schema_meta'").fetchone()
    if not has_meta:
        return None
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else None


def init_schema(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} < 3.37; STRICT tables unsupported")
    existing = schema_version(conn)
    if existing is not None and existing != EXPECTED_SCHEMA_VERSION:
        # Checked BEFORE running the schema script, so an old database is
        # never partially upgraded.
        raise RuntimeError(
            f"Database schema_version={existing}, code expects {EXPECTED_SCHEMA_VERSION}. "
            "Run `python -m src.pipeline migrate` (it backs up the database first). "
            "The database is never altered silently.")
    conn.executescript(schema_path.read_text())
    (version,) = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if version != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema_version={version}, code expects {EXPECTED_SCHEMA_VERSION}. "
            "Migrate or rebuild the database; it will not be altered silently.")


# ---------------------------------------------------------------------
# Registry sync (YAML -> tables)
# ---------------------------------------------------------------------

def _bool(v):
    return None if v is None else int(bool(v))


def sync_sources(conn: sqlite3.Connection, sources: list[dict]) -> None:
    with conn:
        for s in sources:
            conn.execute("""
                INSERT INTO sources (source_id, name, source_tier, collection_method, url,
                                     usage_terms, redistribution_allowed, terms_checked_at, notes)
                VALUES (:source_id, :name, :source_tier, :collection_method, :url,
                        :usage_terms, :redistribution_allowed, :terms_checked_at, :notes)
                ON CONFLICT (source_id) DO UPDATE SET
                    name = excluded.name, source_tier = excluded.source_tier,
                    collection_method = excluded.collection_method, url = excluded.url,
                    usage_terms = excluded.usage_terms,
                    redistribution_allowed = excluded.redistribution_allowed,
                    terms_checked_at = excluded.terms_checked_at, notes = excluded.notes
            """, {
                "source_id": s["source_id"], "name": s["name"],
                "source_tier": s["source_tier"], "collection_method": s["collection_method"],
                "url": s.get("url"), "usage_terms": s.get("usage_terms"),
                "redistribution_allowed": _bool(s.get("redistribution_allowed")),
                "terms_checked_at": (str(s["terms_checked_at"])
                                     if s.get("terms_checked_at") else None),
                "notes": s.get("notes"),
            })


def sync_indicators(conn: sqlite3.Connection, indicators: list[dict]) -> None:
    with conn:
        for i in indicators:
            conn.execute("""
                INSERT INTO indicators (indicator_id, name, pillar, source_id, source_series_id,
                                        publisher, frequency, units, seasonal_adjustment,
                                        polarity, regime_role, is_active, limitations)
                VALUES (:indicator_id, :name, :pillar, :source_id, :source_series_id,
                        :publisher, :frequency, :units, :seasonal_adjustment,
                        :polarity, :regime_role, :is_active, :limitations)
                ON CONFLICT (indicator_id) DO UPDATE SET
                    name = excluded.name, pillar = excluded.pillar,
                    source_id = excluded.source_id, source_series_id = excluded.source_series_id,
                    publisher = excluded.publisher, frequency = excluded.frequency,
                    units = excluded.units, seasonal_adjustment = excluded.seasonal_adjustment,
                    polarity = excluded.polarity, regime_role = excluded.regime_role,
                    is_active = excluded.is_active, limitations = excluded.limitations
            """, {
                "indicator_id": i["indicator_id"], "name": i["name"], "pillar": i["pillar"],
                "source_id": i["source_id"], "source_series_id": i.get("source_series_id"),
                "publisher": i.get("publisher"), "frequency": i["frequency"],
                "units": i["units"], "seasonal_adjustment": i["seasonal_adjustment"],
                "polarity": i["polarity"], "regime_role": i["regime_role"],
                "is_active": int(i.get("is_active", True)), "limitations": i.get("limitations"),
            })


def sync_assets(conn: sqlite3.Connection, assets: list[dict]) -> None:
    with conn:
        for a in assets:
            conn.execute("""
                INSERT INTO assets (asset_id, name, asset_class, quote_type, indicator_id, notes)
                VALUES (:asset_id, :name, :asset_class, :quote_type, :indicator_id, :notes)
                ON CONFLICT (asset_id) DO UPDATE SET
                    name = excluded.name, asset_class = excluded.asset_class,
                    quote_type = excluded.quote_type, indicator_id = excluded.indicator_id,
                    notes = excluded.notes
            """, {"asset_id": a["asset_id"], "name": a["name"], "asset_class": a["asset_class"],
                  "quote_type": a["quote_type"], "indicator_id": a.get("indicator_id"),
                  "notes": a.get("notes")})


# ---------------------------------------------------------------------
# Runs and fetch log
# ---------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, trigger: str = "manual",
              code_version: str | None = None) -> int:
    with conn:
        cur = conn.execute("""
            INSERT INTO ingestion_runs (started_at, status, run_trigger, code_version)
            VALUES (?, 'running', ?, ?)
        """, (utc_now(), trigger, code_version))
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, notes: str | None) -> None:
    with conn:
        conn.execute("""
            UPDATE ingestion_runs SET finished_at = ?, status = ?, notes = ? WHERE run_id = ?
        """, (utc_now(), status, notes, run_id))


def log_fetch(conn: sqlite3.Connection, *, run_id: int, source_id: str, target: str,
              retrieved_at: str, status: str, source_last_updated: str | None = None,
              raw_path: str | None = None, raw_sha256: str | None = None,
              rows_received: int | None = None, error_message: str | None = None) -> int:
    with conn:
        cur = conn.execute("""
            INSERT INTO fetch_log (run_id, source_id, target, retrieved_at, source_last_updated,
                                   raw_path, raw_sha256, rows_received, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, source_id, target, retrieved_at, source_last_updated,
              raw_path, raw_sha256, rows_received, status, error_message))
    return cur.lastrowid


# ---------------------------------------------------------------------
# Observations (vintage-aware)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ObsRow:
    observation_date: str
    vintage_date: str
    vintage_basis: str
    value: float | None
    value_status: str  # 'ok' | 'missing'


@dataclass
class InsertResult:
    inserted: int = 0
    unchanged: int = 0
    conflicts: list[str] | None = None

    def __post_init__(self):
        if self.conflicts is None:
            self.conflicts = []


def insert_observations(conn: sqlite3.Connection, indicator_id: str,
                        rows: list[ObsRow], fetch_id: int, *,
                        commit: bool = True) -> InsertResult:
    """
    Insert observations without ever overwriting an existing vintage.

    retrieval_date rows (FRED default mode): a new vintage row is written only
        when the value differs from the latest stored vintage of that
        observation. Re-running the pipeline does not duplicate the table.

    source_realtime / source_realtime_clipped / release_date rows: the
        vintage is defined by the source, so the row is written once. If the
        same (observation, vintage) already exists with a different value,
        that is a data conflict: it is reported and the stored row is kept.
    """
    result = InsertResult()
    # commit=False lets a caller group several indicators in one transaction
    with (conn if commit else contextlib.nullcontext()):
        for r in rows:
            existing = conn.execute("""
                SELECT value, value_status FROM observations
                WHERE indicator_id = ? AND observation_date = ? AND vintage_date = ?
            """, (indicator_id, r.observation_date, r.vintage_date)).fetchone()

            if r.vintage_basis == "retrieval_date":
                latest = conn.execute("""
                    SELECT value, value_status FROM observations
                    WHERE indicator_id = ? AND observation_date = ?
                    ORDER BY vintage_date DESC LIMIT 1
                """, (indicator_id, r.observation_date)).fetchone()
                if latest and (latest["value"], latest["value_status"]) == (r.value, r.value_status):
                    result.unchanged += 1
                    continue

            if existing:
                if (existing["value"], existing["value_status"]) == (r.value, r.value_status):
                    result.unchanged += 1
                else:
                    result.conflicts.append(
                        f"{indicator_id} {r.observation_date} vintage {r.vintage_date}: "
                        f"stored {existing['value']!r}, received {r.value!r} (kept stored)")
                continue

            conn.execute("""
                INSERT INTO observations (indicator_id, observation_date, vintage_date,
                                          vintage_basis, value, value_status, fetch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (indicator_id, r.observation_date, r.vintage_date, r.vintage_basis,
                  r.value, r.value_status, fetch_id))
            result.inserted += 1
    return result
