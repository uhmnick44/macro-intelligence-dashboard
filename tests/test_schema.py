"""
Schema tests for src/storage/schema.sql
Run from the project root:  pytest tests/test_schema.py -v

Each test builds a fresh in-memory database, so nothing touches db/macro.db.
'TEST_CONSENSUS' is a test-only placeholder, not a real consensus provider.
"""
import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "storage" / "schema.sql"
TS = "2026-10-01T12:00:00Z"


def _seed(conn):
    conn.executescript(f"""
        INSERT INTO sources (source_id, name, source_tier, collection_method) VALUES
            ('FRED', 'Federal Reserve Economic Data', 'official', 'api'),
            ('BLS', 'Bureau of Labor Statistics', 'official', 'api'),
            ('TEST_CONSENSUS', 'Test-only consensus provider', 'third_party', 'manual_entry');

        INSERT INTO methodology_versions (version, config_hash, description, created_at)
            VALUES ('test-v0', 'testhash', 'Test methodology', '{TS}');

        INSERT INTO ingestion_runs (run_id, started_at, status, run_trigger)
            VALUES (1, '{TS}', 'success', 'test');

        INSERT INTO fetch_log (fetch_id, run_id, source_id, target, retrieved_at, status)
            VALUES (1, 1, 'FRED', 'test', '{TS}', 'ok');

        INSERT INTO indicators (indicator_id, name, pillar, source_id, source_series_id,
                                frequency, units, seasonal_adjustment, polarity, regime_role) VALUES
            ('cpi_headline', 'CPI', 'inflation', 'FRED', 'CPIAUCSL',
             'monthly', 'index', 'SA', 1, 'input'),
            ('ahe', 'Average Hourly Earnings', 'labor', 'FRED', 'CES0500000003',
             'monthly', 'dollars per hour', 'SA', 1, 'input');
    """)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    _seed(conn)
    yield conn
    conn.close()


OBS_SQL = """
    INSERT INTO observations (indicator_id, observation_date, vintage_date,
                              vintage_basis, value, value_status, fetch_id)
    VALUES (?, ?, ?, ?, ?, ?, 1)
"""


def _insert_cpi_release(conn, forecast, decimals=1, source="TEST_CONSENSUS"):
    conn.execute("""
        INSERT INTO release_events (event_id, release_name, source_id, reference_period,
                                    release_time_utc, status)
        VALUES (1, 'CPI', 'BLS', '2026-08', '2026-09-11T12:30:00Z', 'released')
    """)
    conn.execute("""
        INSERT INTO release_values (value_id, event_id, indicator_id, measure, unit,
                                    published_decimals)
        VALUES (1, 1, 'cpi_headline', 'mom_pct', 'percent', ?)
    """, (decimals,))
    conn.execute("""
        INSERT INTO forecasts (value_id, forecast_source, forecast_value,
                               collected_at, collection_method)
        VALUES (1, ?, ?, ?, 'manual_entry')
    """, (source, forecast, TS))


def _add_candidates_and_validation(conn, manual, computed, status):
    """Schema-level setup of one validated figure (the pipeline does this in Python)."""
    conn.executescript(f"""
        INSERT INTO release_method_versions VALUES ('rm-test', 'h', '{TS}');
        INSERT INTO active_versions VALUES ('release_methods', 'rm-test', '{TS}');
        INSERT INTO active_versions VALUES ('validation_rule', 'rule-test', '{TS}');
    """)
    conn.execute("""INSERT INTO release_value_candidates (candidate_id, value_id, origin, value,
                    value_unrounded, bound_low, bound_high, source_id, method, method_version,
                    as_of_date, inputs, run_id, recorded_at)
                    VALUES (1, 1, 'computed', ?, ?, ?, ?, 'FRED', 'm', 'rm-test', '2026-09-11',
                            '{}', 1, ?)""", (computed, computed, computed, computed, TS))
    conn.execute("""INSERT INTO release_value_candidates (candidate_id, value_id, origin, value,
                    value_text, source_id, fetch_id, run_id, recorded_at)
                    VALUES (2, 1, 'manual', ?, ?, 'BLS', 1, 1, ?)""", (manual, str(manual), TS))
    conn.execute("""INSERT INTO release_validations (value_id, computed_candidate_id,
                    manual_candidate_id, rule_version, status, published_decimals, explanation,
                    run_id, validated_at)
                    VALUES (1, 1, 2, 'rule-test', ?, 1, 'x', 1, ?)""", (status, TS))
    # integrity checks passed for both candidates and the forecast, in a completed sweep
    conn.execute("INSERT INTO integrity_sweeps VALUES (1, ?, 3, 0)", (TS,))
    for subject, ref in (("computed", "1"), ("manual", "2"), ("forecast", "1")):
        conn.execute("""INSERT INTO integrity_checks (run_id, value_id, subject, subject_ref,
                        outcome, detail, checked_at) VALUES (1, 1, ?, ?, 'pass', 'ok', ?)""",
                     (subject, ref, TS))


# --- environment -------------------------------------------------------

def test_sqlite_supports_strict_tables():
    assert sqlite3.sqlite_version_info >= (3, 37, 0), (
        f"SQLite {sqlite3.sqlite_version} is too old for STRICT tables; need >= 3.37"
    )


# --- provenance and integrity -------------------------------------------

def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(OBS_SQL, ("not_an_indicator", "2026-08-01", "2026-09-30",
                             "retrieval_date", 1.0, "ok"))


def test_bad_date_format_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(OBS_SQL, ("cpi_headline", "08/01/2026", "2026-09-30",
                             "retrieval_date", 1.0, "ok"))


def test_null_value_requires_missing_status(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(OBS_SQL, ("cpi_headline", "2026-07-01", "2026-09-30",
                             "retrieval_date", None, "ok"))
    # NULL with an explicit 'missing' status is allowed
    db.execute(OBS_SQL, ("cpi_headline", "2026-07-01", "2026-09-30",
                         "retrieval_date", None, "missing"))


def test_vintage_cannot_precede_observation(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(OBS_SQL, ("cpi_headline", "2026-08-01", "2026-07-15",
                             "source_realtime", 1.0, "ok"))


# --- vintages -------------------------------------------------------------

def test_latest_view_returns_newest_vintage(db):
    db.execute(OBS_SQL, ("cpi_headline", "2026-08-01", "2026-09-11",
                         "source_realtime", 320.0, "ok"))
    db.execute(OBS_SQL, ("cpi_headline", "2026-08-01", "2026-09-30",
                         "retrieval_date", 320.4, "ok"))
    rows = db.execute("""
        SELECT value, vintage_date FROM v_observations_latest
        WHERE indicator_id = 'cpi_headline' AND observation_date = '2026-08-01'
    """).fetchall()
    assert rows == [(320.4, "2026-09-30")]


# --- forecasts and surprises ---------------------------------------------

@pytest.mark.parametrize("bad_source", [None, "NOT_A_REGISTERED_SOURCE"])
def test_forecast_requires_registered_source(db, bad_source):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_cpi_release(db, forecast=0.2, source=bad_source)


def test_surprise_rounded_to_published_precision(db):
    _insert_cpi_release(db, forecast=0.2, decimals=1)
    _add_candidates_and_validation(db, manual=0.3, computed=0.3, status="match")
    (surprise,) = db.execute("SELECT raw_surprise FROM v_surprises").fetchone()
    assert surprise == 0.1   # not 0.09999999999999998


def test_discrepancy_excluded_from_surprises(db):
    _insert_cpi_release(db, forecast=0.2, decimals=1)
    _add_candidates_and_validation(db, manual=0.5, computed=0.3, status="discrepancy")
    assert db.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 0
    (status, verified) = db.execute(
        "SELECT final_status, verified_value FROM v_release_status").fetchone()
    assert (status, verified) == ("discrepancy", None)


# --- Phase 5 integrity triggers ------------------------------------------

def test_candidates_are_append_only(db):
    _insert_cpi_release(db, forecast=0.2)
    _add_candidates_and_validation(db, manual=0.3, computed=0.3, status="match")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE release_value_candidates SET value = 0.9 WHERE candidate_id = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM release_validations")


def test_observations_are_append_only(db):
    db.execute(OBS_SQL, ("cpi_headline", "2026-08-01", "2026-09-11", "source_realtime",
                         320.0, "ok"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE observations SET value = 321.0")


def test_validation_cannot_mix_candidates_from_other_figures(db):
    _insert_cpi_release(db, forecast=0.2)
    _add_candidates_and_validation(db, manual=0.3, computed=0.3, status="match")
    db.execute("""INSERT INTO release_values (value_id, event_id, indicator_id, measure, unit,
                  published_decimals) VALUES (2, 1, 'cpi_headline', 'yoy_pct', 'percent', 1)""")
    with pytest.raises(sqlite3.IntegrityError, match="same release value"):
        db.execute("""INSERT INTO release_validations (value_id, computed_candidate_id,
                      manual_candidate_id, rule_version, status, published_decimals, explanation,
                      run_id, validated_at)
                      VALUES (2, 1, 2, 'rule-test', 'match', 1, 'x', 1, ?)""", (TS,))


# --- analytics ------------------------------------------------------------

PILLAR_SQL = """
    INSERT INTO pillar_states (as_of_date, pillar, methodology_version, data_mode,
                               state, inputs_used, inputs_missing, computed_at)
    VALUES ('2026-09-30', ?, 'test-v0', 'latest', ?, 1, 0, ?)
"""


def test_pillar_state_must_match_pillar(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(PILLAR_SQL, ("growth", "heating", TS))


def test_indicator_counts_in_only_one_pillar(db):
    db.execute("""
        INSERT INTO indicator_signals (indicator_id, as_of_date, methodology_version,
                                       data_mode, direction, computed_at)
        VALUES ('ahe', '2026-09-30', 'test-v0', 'latest', 'rising', ?)
    """, (TS,))
    db.execute(PILLAR_SQL, ("labor", "strengthening", TS))
    db.execute(PILLAR_SQL, ("inflation", "heating", TS))

    input_sql = """
        INSERT INTO pillar_state_inputs (as_of_date, pillar, methodology_version, data_mode,
                                         indicator_id, polarity, weight, contribution)
        VALUES ('2026-09-30', ?, 'test-v0', 'latest', 'ahe', 1, 1.0, 1.0)
    """
    db.execute(input_sql, ("labor",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(input_sql, ("inflation",))


def test_surprise_needs_validated_forecast(db):
    _insert_cpi_release(db, forecast=0.2, decimals=1)
    _add_candidates_and_validation(db, manual=0.3, computed=0.3, status="match")
    db.execute("""INSERT INTO integrity_checks (run_id, value_id, subject, subject_ref, outcome,
                  severity, detail, checked_at)
                  VALUES (1, 1, 'forecast', '1', 'fail', 'data_quality', 'no as-of', ?)""", (TS,))
    assert db.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 0


def test_override_requires_evidence_and_approval_in_schema(db):
    _insert_cpi_release(db, forecast=0.2)
    _add_candidates_and_validation(db, manual=0.5, computed=0.3, status="discrepancy")
    sql = """INSERT INTO release_overrides (override_id, action, validation_id, resolution,
             reason_code, justification, evidence_source, evidence_reference, decided_by,
             decided_at, run_id, raw_path, raw_sha256, recorded_at)
             VALUES (?, 'resolve', 1, 'use_computed', 'other', 'checked the release', ?, ?,
                     'a', '2026-10-13', 1, 'p', 'h', ?)"""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ("O1", None, "BLS table A", TS))          # no evidence source
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ("O1", "BLS", "-", TS))                    # reference too short
    db.execute(sql, ("O1", "BLS", "BLS table A", TS))
    apr = """INSERT INTO override_approvals (approval_id, override_id, approved_by, approved_at,
             run_id, raw_path, raw_sha256, recorded_at) VALUES (?, 'O1', ?, ?, 1, 'p', 'h', ?)"""
    with pytest.raises(sqlite3.IntegrityError, match="predate"):
        db.execute(apr, ("A0", "a", "2026-10-12T09:00:00Z", TS))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(apr, ("A0", "a", "2026-10-13", TS))             # not a timestamp
    db.execute(apr, ("O1", "a", "2026-10-13T15:00:00Z", TS))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(apr, ("A2", " A ", "2026-10-14T09:00:00Z", TS))  # same person twice


def test_nothing_verified_without_a_sweep_in_the_latest_run(db):
    _insert_cpi_release(db, forecast=0.2)
    _add_candidates_and_validation(db, manual=0.3, computed=0.3, status="match")
    db.execute("INSERT INTO ingestion_runs (run_id, started_at, status, run_trigger) "
               "VALUES (2, ?, 'failed', 'test')", (TS,))
    row = db.execute("SELECT verification, unverified_reason FROM v_release_status").fetchone()
    assert row[0] == "unverified" and "did not complete" in row[1]
