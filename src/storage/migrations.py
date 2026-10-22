"""
Explicit, versioned schema migrations.

    python -m src.pipeline migrate

Rules
  - Migrations never run automatically. init_schema() refuses an old
    database and tells you to run `migrate`.
  - The database is backed up (SQLite online backup) before any change.
  - Each step runs in one transaction. Foreign keys are checked before
    commit; any problem rolls the whole step back.
  - Each step builds its target from a FROZEN schema file
    (src/storage/schema_history/schema_vN.sql), never from the live
    schema.sql, so a later schema change cannot alter what an old
    migration does. A test checks that a database migrated from v2
    matches a freshly created one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.storage.db import EXPECTED_SCHEMA_VERSION, schema_version

HISTORY = Path(__file__).with_name("schema_history")


def frozen_schema(version: str) -> str:
    return (HISTORY / f"schema_v{version}.sql").read_text()


class MigrationError(RuntimeError):
    pass


# Frozen copy of the v3 release_values definition.
_RELEASE_VALUES_V3 = """
CREATE TABLE release_values_v3 (
    value_id           INTEGER PRIMARY KEY,
    event_id           INTEGER NOT NULL REFERENCES release_events(event_id),
    indicator_id       TEXT NOT NULL REFERENCES indicators(indicator_id),
    measure            TEXT NOT NULL
                       CHECK (measure IN ('level', 'mom_pct', 'yoy_pct', 'change', 'rate_pct')),
    unit               TEXT NOT NULL,
    published_decimals INTEGER NOT NULL CHECK (published_decimals BETWEEN 0 AND 4),
    UNIQUE (event_id, indicator_id, measure)
) STRICT;
INSERT INTO release_values_v3 (value_id, event_id, indicator_id, measure, unit, published_decimals)
    SELECT value_id, event_id, indicator_id, measure, unit, published_decimals FROM release_values;
DROP TABLE release_values;
ALTER TABLE release_values_v3 RENAME TO release_values;
"""


def _rebuild(conn: sqlite3.Connection, statements: list[str], new_version: str) -> None:
    """Drop all views, run `statements` (which end with the frozen target
    schema), set the version, check foreign keys, commit - all or nothing."""
    views = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")]
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # required for table rebuilds; no-op inside a txn
    try:
        script = ["BEGIN;"] + [f"DROP VIEW {name};" for name in views] + statements
        script.append(f"UPDATE schema_meta SET value = '{new_version}' "
                      "WHERE key = 'schema_version';")
        conn.executescript("\n".join(script))  # leaves the transaction open
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise MigrationError(f"foreign key check failed: {[tuple(p) for p in problems][:10]}")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _v2_to_v3(conn: sqlite3.Connection) -> None:
    # v2 release_values could hold an 'actual'. No v2 code path ever wrote
    # one, but if a row has one we stop rather than guess where it belongs.
    populated = conn.execute(
        "SELECT COUNT(*) FROM release_values WHERE actual IS NOT NULL").fetchone()[0]
    if populated:
        raise MigrationError(
            f"{populated} release_values rows have an 'actual' value. v3 stores actuals as "
            "candidates with provenance; re-enter them through data/manual/actuals.csv. "
            "Nothing was changed.")

    _rebuild(conn, [_RELEASE_VALUES_V3, frozen_schema("3")], "3")


def _v3_to_v4(conn: sqlite3.Connection) -> None:
    # v4 overrides need approved_by / approved_at and evidence. Approvals
    # cannot be invented for existing rows, so stop if there are any.
    n = conn.execute("SELECT COUNT(*) FROM release_overrides").fetchone()[0]
    if n:
        raise MigrationError(
            f"{n} overrides exist. v4 requires evidence and an approval (approved_by, "
            "approved_at) on every override, which cannot be added automatically. Keep the "
            "backup, add those columns to data/manual/overrides.csv, then rebuild the "
            "database from FRED and data/manual. Nothing was changed.")
    _rebuild(conn, ["DROP TABLE release_overrides;", frozen_schema("4")], "4")


def _v4_to_v5(conn: sqlite3.Connection) -> None:
    # v5 moves approvals into override_approvals and splits evidence into a
    # registered source + reference. Existing overrides cannot be converted
    # without inventing an evidence source and an approval time.
    n = conn.execute("SELECT COUNT(*) FROM release_overrides").fetchone()[0]
    if n:
        raise MigrationError(
            f"{n} overrides exist. v5 requires evidence_source, evidence_reference and a UTC "
            "approval timestamp, which cannot be added automatically. Keep the backup, update "
            "data/manual/overrides.csv to the v5 columns, then rebuild the database from FRED "
            "and data/manual. Nothing was changed.")
    _rebuild(conn, ["DROP TABLE release_overrides;", frozen_schema("5")], "5")


MIGRATIONS = {"2": _v2_to_v3, "3": _v3_to_v4, "4": _v4_to_v5}


def backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = db_path.with_name(f"{db_path.name}.bak-v{schema_version(conn)}-{stamp}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(conn: sqlite3.Connection, *, db_path: Path | None = None) -> list[str]:
    """Upgrade step by step to EXPECTED_SCHEMA_VERSION. Returns a log."""
    log = []
    current = schema_version(conn)
    if current is None:
        raise MigrationError("empty database: run `python -m src.pipeline init-db` instead")
    if current == EXPECTED_SCHEMA_VERSION:
        return [f"already at schema version {current}; nothing to do"]
    if db_path is not None:
        log.append(f"backup written to {backup(conn, db_path)}")

    while current != EXPECTED_SCHEMA_VERSION:
        step = MIGRATIONS.get(current)
        if step is None:
            raise MigrationError(f"no migration from schema version {current}")
        step(conn)
        new = schema_version(conn)
        log.append(f"migrated schema {current} -> {new}")
        current = new
    return log
