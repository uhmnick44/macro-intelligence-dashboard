"""
Schema migration tests (v2 -> v3).
Run: pytest tests/test_migration.py -v
"""
import re
import sqlite3
from pathlib import Path

import pytest

from src.storage import db, migrations

HISTORY = Path(__file__).resolve().parents[1] / "src" / "storage" / "schema_history"
V2 = HISTORY / "schema_v2.sql"


def _v2_db(with_actual=False):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(V2.read_text())
    c.executescript("""
        INSERT INTO sources (source_id, name, source_tier, collection_method)
            VALUES ('FRED', 'f', 'official', 'api'), ('BLS', 'b', 'official', 'reference_only');
        INSERT INTO indicators (indicator_id, name, pillar, source_id, source_series_id,
                                frequency, units, seasonal_adjustment, polarity, regime_role)
            VALUES ('cpi_headline', 'CPI', 'inflation', 'FRED', 'CPIAUCSL', 'monthly', 'i',
                    'SA', 1, 'input');
        INSERT INTO release_events (event_id, release_name, source_id, reference_period,
                                    release_time_utc, status)
            VALUES (1, 'CPI', 'BLS', '2026-08', '2026-09-11T12:30:00Z', 'scheduled');
    """)
    c.execute("""INSERT INTO release_values (value_id, event_id, indicator_id, measure, unit,
                 published_decimals, actual, actual_origin)
                 VALUES (7, 1, 'cpi_headline', 'mom_pct', 'percent', 1, ?, ?)""",
              (0.3, "manual_entry") if with_actual else (None, "pending"))
    c.execute("""INSERT INTO forecasts (value_id, forecast_source, forecast_value, collected_at,
                 collection_method) VALUES (7, 'BLS', 0.2, 'x', 'manual_entry')""")
    c.commit()
    return c


def _normalized_schema(conn):
    out = {}
    for typ, name, sql in conn.execute("SELECT type, name, sql FROM sqlite_master "
                                       "WHERE sql IS NOT NULL"):
        sql = re.sub(r"--[^\n]*", "", sql)
        sql = sql.replace("IF NOT EXISTS ", "").replace('"', "")
        out[(typ, name)] = re.sub(r"\s+", " ", sql).strip()
    return out


def test_init_schema_refuses_old_database():
    c = _v2_db()
    with pytest.raises(RuntimeError, match="migrate"):
        db.init_schema(c)
    assert db.schema_version(c) == "2"     # untouched


def test_migration_preserves_data_and_matches_fresh_schema():
    c = _v2_db()
    log = migrations.migrate(c)
    assert log == ["migrated schema 2 -> 3", "migrated schema 3 -> 4",
                   "migrated schema 4 -> 5"]
    assert db.schema_version(c) == "5"
    row = c.execute("SELECT value_id, forecast_value FROM release_values "
                    "JOIN forecasts USING (value_id)").fetchone()
    assert tuple(row) == (7, 0.2)
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []

    fresh = sqlite3.connect(":memory:")
    db.init_schema(fresh)
    assert _normalized_schema(c) == _normalized_schema(fresh)


def test_migration_refuses_unplaced_actuals_and_changes_nothing():
    c = _v2_db(with_actual=True)
    with pytest.raises(migrations.MigrationError, match="actuals.csv"):
        migrations.migrate(c)
    assert db.schema_version(c) == "2"
    assert c.execute("SELECT actual FROM release_values").fetchone()[0] == 0.3


def test_migration_writes_backup_first(tmp_path):
    path = tmp_path / "macro.db"
    src = sqlite3.connect(path)
    src.executescript(V2.read_text())
    src.commit()
    log = migrations.migrate(src, db_path=path)
    backups = list(tmp_path.glob("macro.db.bak-v2-*"))
    assert len(backups) == 1 and "backup" in log[0]
    assert db.schema_version(sqlite3.connect(backups[0])) == "2"


def test_live_schema_equals_newest_frozen_schema():
    newest = max(HISTORY.glob("schema_v*.sql"), key=lambda p: int(p.stem.split("_v")[1]))
    live = Path(db.SCHEMA_PATH).read_text()
    assert newest.read_text() == live, "copy schema.sql to schema_history when releasing"
    assert newest.stem == f"schema_v{db.EXPECTED_SCHEMA_VERSION}"


def _v3_db(with_override=True):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript((HISTORY / "schema_v3.sql").read_text())
    c.executescript("""
        INSERT INTO sources (source_id, name, source_tier, collection_method)
            VALUES ('FRED', 'f', 'official', 'api'), ('BLS', 'b', 'official', 'reference_only');
        INSERT INTO indicators (indicator_id, name, pillar, source_id, source_series_id,
                                frequency, units, seasonal_adjustment, polarity, regime_role)
            VALUES ('cpi_headline', 'CPI', 'inflation', 'FRED', 'CPIAUCSL', 'monthly', 'i',
                    'SA', 1, 'input');
        INSERT INTO ingestion_runs (run_id, started_at, status, run_trigger)
            VALUES (1, 'x', 'success', 'test');
        INSERT INTO fetch_log (fetch_id, run_id, source_id, target, retrieved_at, status)
            VALUES (1, 1, 'BLS', 't', 'x', 'ok');
        INSERT INTO release_events (event_id, release_name, source_id, reference_period,
                                    release_time_utc, status)
            VALUES (1, 'CPI', 'BLS', '2026-08', '2026-09-11T12:30:00Z', 'released');
        INSERT INTO release_values VALUES (1, 1, 'cpi_headline', 'mom_pct', 'percent', 1);
        INSERT INTO release_value_candidates (candidate_id, value_id, origin, value, value_text,
            source_id, fetch_id, run_id, recorded_at)
            VALUES (1, 1, 'manual', 0.5, '0.5', 'BLS', 1, 1, 'x');
        INSERT INTO release_validations (validation_id, value_id, manual_candidate_id,
            rule_version, status, published_decimals, explanation, run_id, validated_at)
            VALUES (1, 1, 1, 'r', 'manual_only', 1, 'x', 1, 'x');
    """)
    if with_override:
        c.executescript("""
        INSERT INTO release_overrides (override_id, action, validation_id, resolution,
            reason_code, justification, decided_by, decided_at, run_id, raw_path, raw_sha256,
            recorded_at)
            VALUES ('OVR-1', 'resolve', 1, 'use_manual', 'other', 'j', 'me', '2026-10-13', 1,
                    'p', 'h', 'x');
    """)
    c.commit()
    return c


def test_v3_to_v4_refuses_to_invent_approvals():
    c = _v3_db()
    with pytest.raises(migrations.MigrationError, match="approval"):
        migrations.migrate(c)
    assert db.schema_version(c) == "3"
    assert c.execute("SELECT COUNT(*) FROM release_overrides").fetchone()[0] == 1


def test_v3_to_v4_without_overrides_keeps_candidates():
    c = _v3_db(with_override=False)
    assert migrations.migrate(c) == ["migrated schema 3 -> 4", "migrated schema 4 -> 5"]
    assert c.execute("SELECT value_text FROM release_value_candidates").fetchone()[0] == "0.5"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v4_to_v5_refuses_to_invent_evidence_or_approval_times():
    c = sqlite3.connect(":memory:")
    c.executescript((HISTORY / "schema_v4.sql").read_text())
    c.executescript("""
        INSERT INTO sources (source_id, name, source_tier, collection_method)
            VALUES ('BLS', 'b', 'official', 'reference_only');
        INSERT INTO indicators (indicator_id, name, pillar, source_id, frequency, units,
                                seasonal_adjustment, polarity, regime_role)
            VALUES ('cpi_headline', 'CPI', 'inflation', 'BLS', 'monthly', 'i', 'SA', 1, 'input');
        INSERT INTO ingestion_runs VALUES (1, 'x', NULL, 'success', 'test', NULL, NULL);
        INSERT INTO fetch_log (fetch_id, run_id, source_id, target, retrieved_at, status)
            VALUES (1, 1, 'BLS', 't', 'x', 'ok');
        INSERT INTO release_events (event_id, release_name, source_id, reference_period,
                                    release_time_utc, status)
            VALUES (1, 'CPI', 'BLS', '2026-08', '2026-09-11T12:30:00Z', 'released');
        INSERT INTO release_values VALUES (1, 1, 'cpi_headline', 'mom_pct', 'percent', 1);
        INSERT INTO release_value_candidates (candidate_id, value_id, origin, value, value_text,
            source_id, fetch_id, run_id, recorded_at) VALUES (1, 1, 'manual', 0.5, '0.5', 'BLS', 1, 1, 'x');
        INSERT INTO release_validations (validation_id, value_id, manual_candidate_id,
            rule_version, status, published_decimals, explanation, run_id, validated_at)
            VALUES (1, 1, 1, 'r', 'manual_only', 1, 'x', 1, 'x');
        INSERT INTO release_overrides (override_id, action, validation_id, resolution,
            reason_code, justification, evidence, decided_by, decided_at, approved_by,
            approved_at, run_id, raw_path, raw_sha256, recorded_at)
            VALUES ('OVR-1', 'resolve', 1, 'use_manual', 'other', 'j', 'doc', 'me',
                    '2026-10-13', 'me', '2026-10-13', 1, 'p', 'h', 'x');
    """)
    with pytest.raises(migrations.MigrationError, match="evidence_source"):
        migrations.migrate(c)
    assert db.schema_version(c) == "4"
