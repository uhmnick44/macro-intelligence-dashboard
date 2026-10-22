"""
Pipeline-level tests (mocked network, temporary directories).
Run: pytest tests/test_pipeline.py -v
"""
from src.config import Settings, load_config
from src.pipeline import run_ingest
from tests.conftest import FakeResponse, make_client, meta_payload, obs_payload


def _settings(tmp_path, key=None):
    return Settings(fred_api_key=key, db_path=tmp_path / "macro.db",
                    raw_dir=tmp_path / "raw", manual_dir=tmp_path / "manual")


def test_missing_api_key_is_reported_not_hidden(tmp_path):
    summary = run_ingest(_settings(tmp_path), load_config())
    assert summary["status"] == "failed"
    assert any("FRED_API_KEY" in p for p in summary["problems"])


def test_one_bad_series_gives_partial_run(tmp_path):
    client, _ = make_client([
        FakeResponse(200, meta_payload("INDPRO")),
        FakeResponse(200, obs_payload([{"realtime_start": "2026-10-10",
                                        "realtime_end": "2026-10-10",
                                        "date": "2026-08-01", "value": "101.0"}])),
        FakeResponse(400, {"error_code": 400, "error_message": "Bad Request."}),
    ])
    summary = run_ingest(_settings(tmp_path, key="x"), load_config(),
                         only={"industrial_production", "ppi_final_demand"}, client=client)
    assert summary["status"] == "partial"
    statuses = {r.indicator_id: r.status for r in summary["fred"]}
    assert statuses == {"industrial_production": "ok", "ppi_final_demand": "error"}


def test_config_validates():
    cfg = load_config()
    inputs = [i for i in cfg.indicators if i["regime_role"] == "input"]
    assert all(i["polarity"] != 0 for i in inputs)
    # average hourly earnings is assigned to exactly one pillar
    assert cfg.indicator("avg_hourly_earnings")["pillar"] == "labor"


def test_overrides_load_after_validation_in_one_run(tmp_path):
    """actuals.csv + overrides.csv in the same run: the override must bind to
    the validation created earlier in that run."""
    from src.data_collection import manual_loader as ml
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / "actuals.csv").write_text(
        ",".join(ml.ACTUAL_COLUMNS) + "\n"
        "CPI,2026-08,2026-09-11T12:30:00Z,BLS,cpi_headline,mom_pct,percent,1,0.3,BLS,\n")
    (manual / "overrides.csv").write_text(
        ",".join(ml.OVERRIDE_COLUMNS) + "\n"
        "OVR-1,resolve,CPI,2026-08,cpi_headline,mom_pct,,0.3,use_manual,,"
        "verified_against_primary_source,Matches BLS news release table A,BLS,"
        "BLS CPI news release Table A,tester,2026-10-13,tester,2026-10-13T15:00:00Z,\n")
    summary = run_ingest(_settings(tmp_path), load_config(), skip_fred=True)
    assert summary["release_status"] == {"verified_override": 1}
    import sqlite3
    status = sqlite3.connect(tmp_path / "macro.db").execute(
        "SELECT override_approval_status FROM v_release_status").fetchone()[0]
    assert status == "self_approved"
    assert summary["review_queue"] == []


def test_bad_manual_file_does_not_stop_the_run(tmp_path):
    from src.data_collection import manual_loader as ml
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / "actuals.csv").write_text(
        ",".join(ml.ACTUAL_COLUMNS) + "\n"
        "CPI,2026-13,2026-09-11T12:30:00Z,BLS,cpi_headline,mom_pct,percent,1,0.3,BLS,\n")
    summary = run_ingest(_settings(tmp_path), load_config(), skip_fred=True)
    assert summary["status"] in ("partial", "failed")
    assert any("actuals.csv: rejected" in p for p in summary["problems"])
    assert "release_status" in summary          # release processing still ran


def test_aborted_run_is_recorded_as_failed(tmp_path):
    import sqlite3

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("unexpected failure")

    from src.data_collection.fred_client import FredClient
    client = FredClient("x" * 32, session=Boom(), sleep=lambda s: None)
    settings = _settings(tmp_path, key="x")
    try:
        run_ingest(settings, load_config(), only={"industrial_production"}, client=client)
        raise AssertionError("expected the run to abort")
    except RuntimeError as exc:
        assert "unexpected failure" in str(exc)
    status, notes = sqlite3.connect(settings.db_path).execute(
        "SELECT status, notes FROM ingestion_runs").fetchone()
    assert status == "failed" and "aborted" in notes
