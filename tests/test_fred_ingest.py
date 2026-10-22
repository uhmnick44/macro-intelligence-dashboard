"""
FRED client + ingestion tests (network fully mocked; synthetic values only).
Run: pytest tests/test_fred_ingest.py -v
"""
import json

import pytest

from src.data_collection.fred_client import REDACTED, FredError
from src.data_collection.fred_ingest import (check_metadata, ingest_indicator,
                                             parse_last_updated, parse_observations)
from tests.conftest import TEST_KEY, FakeResponse, make_client, meta_payload, obs_payload


def _obs(date, value, rt="2026-10-10"):
    return {"realtime_start": rt, "realtime_end": "9999-12-31" if rt != "2026-10-10" else rt,
            "date": date, "value": value}


# --- client ---------------------------------------------------------------

def test_api_key_never_returned_or_snapshotted(cfg, conn, store, run_id):
    client, session = make_client([
        FakeResponse(200, meta_payload("INDPRO")),
        FakeResponse(200, obs_payload([_obs("2026-08-01", "100.0")])),
    ])
    ingest_indicator(conn, client, store, cfg.indicator("industrial_production"), run_id=run_id)
    # key was sent ...
    assert all(p["api_key"] == TEST_KEY for _, p in session.calls)
    # ... but appears in no snapshot or sidecar on disk
    for f in store.root.rglob("*"):
        if f.is_file():
            assert TEST_KEY not in f.read_text()


def test_retries_on_429_then_succeeds():
    client, session = make_client([
        FakeResponse(429, {"error_code": 429, "error_message": "Too Many Requests"}),
        FakeResponse(200, meta_payload("X")),
    ])
    assert client.series_info("X").status == 200
    assert len(session.calls) == 2


def test_400_is_not_retried_and_message_is_redacted():
    msg = f"Bad Request. api_key {TEST_KEY} rejected"
    client, session = make_client([FakeResponse(400, {"error_code": 400, "error_message": msg})])
    with pytest.raises(FredError) as exc:
        client.series_info("NOPE")
    assert exc.value.status == 400
    assert TEST_KEY not in str(exc.value) and REDACTED in str(exc.value)
    assert len(session.calls) == 1


# --- parsing ----------------------------------------------------------------

def test_missing_value_dot_becomes_null_with_status():
    rows, rejected = parse_observations(
        obs_payload([_obs("2026-08-01", ".")]), mode="latest")
    assert rows[0].value is None and rows[0].value_status == "missing"
    assert rejected == []


def test_non_numeric_value_is_rejected_not_guessed():
    rows, rejected = parse_observations(
        obs_payload([_obs("2026-08-01", "n/a")]), mode="latest")
    assert rows == [] and "non-numeric" in rejected[0]


def test_vintage_history_marks_window_boundary_as_clipped():
    payload = obs_payload([
        _obs("2024-01-01", "1.0", rt="2024-06-01"),   # starts at window boundary
        _obs("2024-07-01", "2.0", rt="2024-08-14"),   # genuine first release date
    ])
    rows, _ = parse_observations(payload, mode="vintage_history",
                                 requested_realtime_start="2024-06-01")
    assert [r.vintage_basis for r in rows] == ["source_realtime_clipped", "source_realtime"]


def test_last_updated_converted_to_utc():
    assert parse_last_updated("2013-07-31 09:26:16-05") == "2013-07-31T14:26:16Z"
    assert parse_last_updated(None) is None


def test_metadata_mismatch_is_reported(cfg):
    warnings = check_metadata(cfg.indicator("cpi_core"),
                              meta_payload("CPILFESL", frequency_short="Q", sa="NSA")["seriess"][0])
    assert any("frequency" in w for w in warnings)
    assert any("seasonal_adjustment" in w for w in warnings)


# --- ingestion end-to-end (mocked) -----------------------------------------

def _ingest_latest(cfg, conn, store, run_id, values):
    client, _ = make_client([
        FakeResponse(200, meta_payload("INDPRO")),
        FakeResponse(200, obs_payload([_obs(d, v) for d, v in values])),
    ])
    return ingest_indicator(conn, client, store, cfg.indicator("industrial_production"),
                            run_id=run_id)


def test_rerun_does_not_duplicate_unchanged_rows(cfg, conn, store, run_id):
    values = [("2026-07-01", "100.0"), ("2026-08-01", "101.0")]
    first = _ingest_latest(cfg, conn, store, run_id, values)
    second = _ingest_latest(cfg, conn, store, run_id, values)
    assert (first.inserted, second.inserted, second.unchanged) == (2, 0, 2)
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_every_response_is_snapshotted_and_hashed(cfg, conn, store, run_id):
    _ingest_latest(cfg, conn, store, run_id, [("2026-08-01", "101.0")])
    rows = conn.execute("SELECT raw_path, raw_sha256 FROM fetch_log").fetchall()
    assert len(rows) == 2  # metadata + one observations page
    import hashlib
    for raw_path, digest in rows:
        f = store.root / raw_path
        assert hashlib.sha256(f.read_bytes()).hexdigest() == digest
        assert (f.with_name(f.name + ".meta.json")).exists()


def test_observations_link_back_to_their_snapshot(cfg, conn, store, run_id):
    _ingest_latest(cfg, conn, store, run_id, [("2026-08-01", "101.0")])
    (raw_path,) = conn.execute("""
        SELECT f.raw_path FROM observations o JOIN fetch_log f ON f.fetch_id = o.fetch_id
    """).fetchone()
    body = json.loads((store.root / raw_path).read_bytes())
    assert body["observations"][0]["value"] == "101.0"


def test_pagination_follows_count(cfg, conn, store, run_id):
    client, session = make_client([
        FakeResponse(200, meta_payload("INDPRO")),
        FakeResponse(200, obs_payload([_obs("2026-07-01", "100.0")], count=2, offset=0)),
        FakeResponse(200, obs_payload([_obs("2026-08-01", "101.0")], count=2, offset=1)),
    ])
    rep = ingest_indicator(conn, client, store, cfg.indicator("industrial_production"),
                           run_id=run_id)
    assert rep.inserted == 2
    assert session.calls[2][1]["offset"] == 1


def test_vintage_pull_sends_realtime_window_and_stores_vintages(cfg, conn, store, run_id):
    client, session = make_client([
        FakeResponse(200, meta_payload("PAYEMS")),
        FakeResponse(200, obs_payload([
            _obs("2026-07-01", "1000.0", rt="2026-08-07"),   # first print
            _obs("2026-07-01", "1005.0", rt="2026-09-04"),   # revised in next report
        ])),
    ])
    rep = ingest_indicator(conn, client, store, cfg.indicator("payrolls"),
                           run_id=run_id, defaults=cfg.defaults)
    params = session.calls[1][1]
    assert params["realtime_start"] == cfg.defaults["vintage_realtime_start"]
    assert params["realtime_end"] == "9999-12-31"
    assert rep.inserted == 2
    latest = conn.execute("""SELECT value FROM v_observations_latest
                             WHERE indicator_id = 'payrolls'""").fetchone()[0]
    assert latest == 1005.0


def test_source_vintage_conflict_keeps_stored_value(cfg, conn, store, run_id):
    for value in ("1000.0", "999.0"):
        client, _ = make_client([
            FakeResponse(200, meta_payload("PAYEMS")),
            FakeResponse(200, obs_payload([_obs("2026-07-01", value, rt="2026-08-07")])),
        ])
        rep = ingest_indicator(conn, client, store, cfg.indicator("payrolls"),
                               run_id=run_id, defaults=cfg.defaults)
    assert len(rep.conflicts) == 1
    assert conn.execute("SELECT value FROM observations").fetchone()[0] == 1000.0


def test_bad_series_id_is_logged_as_error_not_crash(cfg, conn, store, run_id):
    client, _ = make_client([FakeResponse(400, {"error_code": 400,
                                                "error_message": "Bad Request. The series does not exist."})])
    rep = ingest_indicator(conn, client, store, cfg.indicator("ppi_final_demand"), run_id=run_id)
    assert rep.status == "error"
    (status, err) = conn.execute("SELECT status, error_message FROM fetch_log").fetchone()
    assert status == "error" and "does not exist" in err
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
