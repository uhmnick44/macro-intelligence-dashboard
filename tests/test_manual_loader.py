"""
Manual CSV loader tests (synthetic values only).
Run: pytest tests/test_manual_loader.py -v

TEST_CONSENSUS / TEST_GOLD_VENDOR are test-only placeholders, not real providers.
"""
import pytest

from src.data_collection import manual_loader as ml

FC_HEADER = ",".join(ml.FORECAST_COLUMNS)
BAR_HEADER = ",".join(ml.BAR_COLUMNS)


def _register(conn, source_id, verified=True, tier="third_party"):
    conn.execute("""INSERT INTO sources (source_id, name, source_tier, collection_method,
                                         terms_checked_at)
                    VALUES (?, 'test source', ?, 'manual_entry', ?)""",
                 (source_id, tier, "2026-10-10" if verified else None))
    conn.commit()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def _fc_row(source="TEST_CONSENSUS", period="2026-08", value="0.2", measure="mom_pct"):
    return (f"CPI,{period},2026-09-11T12:30:00Z,BLS,cpi_headline,{measure},percent,1,"
            f"{value},{source},2026-09-10,")


# --- ISM ----------------------------------------------------------------

def test_ism_rows_stored_with_release_date_vintage(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "ism.csv",
               "indicator_id,observation_month,release_date,value\n"
               "ism_manufacturing,2026-08,2026-09-01,50.0\n")
    rep = ml.load_ism(conn, store, p, run_id=run_id)
    assert rep.status == "ok" and rep.inserted == 1
    row = conn.execute("SELECT observation_date, vintage_date, vintage_basis FROM observations"
                       ).fetchone()
    assert tuple(row) == ("2026-08-01", "2026-09-01", "release_date")


def test_ism_one_bad_row_rejects_whole_file(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "ism.csv",
               "indicator_id,observation_month,release_date,value\n"
               "ism_manufacturing,2026-07,2026-08-01,49.0\n"
               "ism_manufacturing,2026-08,2026-09-01,fifty\n")
    rep = ml.load_ism(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "line 3" in rep.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_ism_rejects_non_ism_indicator(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "ism.csv",
               "indicator_id,observation_month,release_date,value\n"
               "cpi_core,2026-08,2026-09-01,50.0\n")
    assert ml.load_ism(conn, store, p, run_id=run_id).status == "rejected"


def test_wrong_header_rejected(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "ism.csv", "indicator,month,value\nism_manufacturing,2026-08,50\n")
    rep = ml.load_ism(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "header" in rep.errors[0]


# --- Forecasts --------------------------------------------------------------

def test_forecast_requires_registered_source(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "forecasts.csv", f"{FC_HEADER}\n{_fc_row()}\n")
    rep = ml.load_forecasts(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "not registered" in " ".join(rep.errors)


def test_forecast_source_must_have_terms_checked(conn, store, run_id, tmp_path):
    _register(conn, "TEST_CONSENSUS", verified=False)
    p = _write(tmp_path, "forecasts.csv", f"{FC_HEADER}\n{_fc_row()}\n")
    rep = ml.load_forecasts(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "terms_checked_at" in " ".join(rep.errors)


def test_forecast_creates_release_value_without_actual(conn, store, run_id, tmp_path):
    _register(conn, "TEST_CONSENSUS")
    p = _write(tmp_path, "forecasts.csv", f"{FC_HEADER}\n{_fc_row()}\n")
    rep = ml.load_forecasts(conn, store, p, run_id=run_id)
    assert rep.status == "ok" and rep.inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM release_values").fetchone()[0] == 1
    # a forecast never creates an actual (v3: actuals are validated candidates)
    assert conn.execute("SELECT COUNT(*) FROM release_value_candidates").fetchone()[0] == 0
    # surprise view stays empty until an actual exists (no fabricated surprise)
    assert conn.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 0


def test_second_consensus_provider_rejected(conn, store, run_id, tmp_path):
    _register(conn, "TEST_CONSENSUS")
    _register(conn, "OTHER_CONSENSUS")
    ml.load_forecasts(conn, store, _write(tmp_path, "forecasts.csv",
                                          f"{FC_HEADER}\n{_fc_row()}\n"), run_id=run_id)
    rep = ml.load_forecasts(conn, store, _write(tmp_path, "forecasts2.csv",
                            f"{FC_HEADER}\n{_fc_row(source='OTHER_CONSENSUS', period='2026-09')}\n"),
                            run_id=run_id)
    assert rep.status == "rejected" and "more than one consensus provider" in rep.errors[0]


def test_reload_is_idempotent_and_changed_forecast_is_conflict(conn, store, run_id, tmp_path):
    _register(conn, "TEST_CONSENSUS")
    p = _write(tmp_path, "forecasts.csv", f"{FC_HEADER}\n{_fc_row()}\n")
    ml.load_forecasts(conn, store, p, run_id=run_id)
    again = ml.load_forecasts(conn, store, p, run_id=run_id)
    assert again.unchanged == 1 and again.inserted == 0
    changed = ml.load_forecasts(conn, store, _write(tmp_path, "forecasts.csv",
                                f"{FC_HEADER}\n{_fc_row(value='0.3')}\n"), run_id=run_id)
    assert len(changed.conflicts) == 1
    assert conn.execute("SELECT forecast_value FROM forecasts").fetchone()[0] == 0.2


# --- FOMC -----------------------------------------------------------------

FOMC_HEADER = ",".join(ml.FOMC_COLUMNS)


def test_fomc_future_meeting_then_decision_filled(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "fomc_meetings.csv",
               f"{FOMC_HEADER}\n2026-12-08,2026-12-09,scheduled,1,,,,,,\n")
    assert ml.load_fomc(conn, store, p, run_id=run_id).inserted == 1
    p = _write(tmp_path, "fomc_meetings.csv",
               f"{FOMC_HEADER}\n2026-12-08,2026-12-09,scheduled,1,hold,1.00,1.25,0,0,\n")
    assert ml.load_fomc(conn, store, p, run_id=run_id).inserted == 1
    assert conn.execute("SELECT decision FROM fomc_meetings").fetchone()[0] == "hold"


def test_fomc_inconsistent_decision_rejected_by_schema(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "fomc_meetings.csv",
               f"{FOMC_HEADER}\n2026-12-08,2026-12-09,scheduled,1,cut,1.00,1.25,25,0,\n")
    rep = ml.load_fomc(conn, store, p, run_id=run_id)
    assert rep.status == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM fomc_meetings").fetchone()[0] == 0


# --- Market bars (gold) -----------------------------------------------------

def test_gold_bar_loaded_with_third_party_label(conn, store, run_id, tmp_path):
    _register(conn, "TEST_GOLD_VENDOR")
    p = _write(tmp_path, "gold.csv",
               f"{BAR_HEADER}\nGOLD,1d,2026-10-09,GCZ6,10,12,9,11,500,TEST_GOLD_VENDOR\n")
    assert ml.load_market_bars(conn, store, p, run_id=run_id).inserted == 1
    tier = conn.execute("SELECT source_tier FROM v_market_daily WHERE asset_id = 'GOLD'"
                        ).fetchone()[0]
    assert tier == "third_party"


def test_bars_for_fred_backed_asset_rejected(conn, store, run_id, tmp_path):
    _register(conn, "TEST_GOLD_VENDOR")
    p = _write(tmp_path, "spx.csv",
               f"{BAR_HEADER}\nSPX,1d,2026-10-09,spot,10,12,9,11,,TEST_GOLD_VENDOR\n")
    rep = ml.load_market_bars(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "duplicate the series" in rep.errors[0]


def test_bar_close_outside_range_rejected(conn, store, run_id, tmp_path):
    _register(conn, "TEST_GOLD_VENDOR")
    p = _write(tmp_path, "gold.csv",
               f"{BAR_HEADER}\nGOLD,1d,2026-10-09,GCZ6,10,12,9,15,,TEST_GOLD_VENDOR\n")
    assert ml.load_market_bars(conn, store, p, run_id=run_id).status == "rejected"


def test_rejected_file_is_still_snapshotted(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "gold.csv",
               f"{BAR_HEADER}\nGOLD,1d,2026-10-09,GCZ6,10,12,9,11,,UNREGISTERED\n")
    ml.load_market_bars(conn, store, p, run_id=run_id)
    assert any(f.suffix == ".csv" for f in store.root.rglob("*"))


def test_forecast_more_precise_than_published_rejected(conn, store, run_id, tmp_path):
    _register(conn, "TEST_CONSENSUS")
    p = _write(tmp_path, "forecasts.csv", f"{FC_HEADER}\n{_fc_row(value='0.25')}\n")
    rep = ml.load_forecasts(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "more decimals" in " ".join(rep.errors)


def test_forecast_snapshot_after_release_rejected(conn, store, run_id, tmp_path):
    _register(conn, "TEST_CONSENSUS")
    row = _fc_row().replace("2026-09-10,", "2026-09-12,")
    rep = ml.load_forecasts(conn, store, _write(tmp_path, "forecasts.csv",
                                                f"{FC_HEADER}\n{row}\n"), run_id=run_id)
    assert rep.status == "rejected" and "predate the release" in " ".join(rep.errors)


def test_unquoted_comma_rejected_cleanly(conn, store, run_id, tmp_path):
    p = _write(tmp_path, "ism.csv", "indicator_id,observation_month,release_date,value\n"
               "ism_manufacturing,2026-08,2026-09-01,50.0,extra\n")
    rep = ml.load_ism(conn, store, p, run_id=run_id)
    assert rep.status == "rejected" and "double quotes" in rep.errors[0]
