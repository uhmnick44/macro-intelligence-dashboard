"""
End-to-end Phase 5 tests: first-print computation from vintages,
validation against manual entries, verification levels, look-ahead
safety, revisions, integrity checks, overrides and their audit trail.
All values are SYNTHETIC.

Run: pytest tests/test_release_validation.py -v

Scenario: CPI for 2026-08 released 2026-09-11 08:30 New York.
July 2026 index = 320.000, August first print = 320.960  ->  +0.3% m/m.
"""
import json
from dataclasses import replace

import pytest

from src.config import ReleaseMethods
from src.data_collection import manual_loader as ml
from src.processing.release_values import MethodVersionError, register_method_version
from tests.conftest import (CPI_RELEASE_DATE, NOW, add_figure, add_manual, add_obs, process,
                            seed_cpi_history, status_row)

OVR_HEADER = ",".join(ml.OVERRIDE_COLUMNS)
FIGURE = {"release_name": "CPI", "reference_period": "2026-08", "indicator_id": "cpi_headline",
          "measure": "mom_pct"}


@pytest.fixture
def cpi(conn, fetch_id):
    """History + August first print + one figure (value_id 1, CPI m/m)."""
    seed_cpi_history(conn, fetch_id)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, 320.960)
    add_figure(conn)
    return conn


def _override_file(tmp_path, *rows):
    p = tmp_path / "overrides.csv"
    p.write_text(OVR_HEADER + "\n" + "\n".join(rows) + "\n")
    return p


def _row(**fields):
    import csv
    import io
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow([fields.get(c, "") for c in ml.OVERRIDE_COLUMNS])
    return buf.getvalue()


DECIDER = "N. Tesconi"


def _resolve(oid, resolution, value="", reason="data_entry_error",
             evidence="BLS CPI news release, Table A", evidence_source="BLS",
             computed="0.3", manual="0.5", figure=FIGURE, approved_by="Reviewer",
             approved_at="2026-10-13T15:00:00Z", justification="Checked BLS news release table A"):
    """A resolve row. computed/manual = the values the analyst reviewed."""
    return _row(override_id=oid, action="resolve", **figure, reviewed_computed=computed,
                reviewed_manual=manual, resolution=resolution, override_value=value,
                reason_code=reason, justification=justification,
                evidence_source=evidence_source, evidence_reference=evidence,
                decided_by=DECIDER, decided_at="2026-10-13",
                approved_by=approved_by, approved_at=approved_at)


def _revoke(oid, target):
    return _row(override_id=oid, action="revoke", justification="Approved too early",
                decided_by=DECIDER, decided_at="2026-10-14", approved_by="Reviewer",
                approved_at="2026-10-14T09:00:00Z", target_override_id=target)


def _approve(oid, target, approved_by="Independent Reviewer",
             approved_at="2026-10-14T10:00:00Z"):
    return _row(override_id=oid, action="approve", justification="Re-checked against Table A",
                evidence_source="BLS", evidence_reference="BLS CPI news release, Table A",
                approved_by=approved_by, approved_at=approved_at, target_override_id=target)


def _load(conn, cfg, store, run_id, tmp_path, *rows):
    """Load overrides.csv, then run checks (as the pipeline does)."""
    rep = ml.load_overrides(conn, store, _override_file(tmp_path, *rows), run_id=run_id, now=NOW)
    process(conn, cfg, run_id)
    return rep


# --- computation -------------------------------------------------------------

def test_first_print_computed_from_release_date_vintage(cfg, cpi, run_id):
    process(cpi, cfg, run_id)
    row = cpi.execute("SELECT * FROM release_value_candidates WHERE origin = 'computed'").fetchone()
    assert row["value"] == 0.3 and row["as_of_date"] == CPI_RELEASE_DATE
    inputs = json.loads(row["inputs"])
    assert inputs["input_precision_decimals"] == 3
    used = {o["role"]: (o["observation_date"], o["vintage_date"]) for o in inputs["observations"]}
    assert used == {"current": ("2026-08-01", CPI_RELEASE_DATE),
                    "base": ("2026-07-01", "2026-08-10")}


def test_later_revision_never_leaks_into_first_print(cfg, cpi, fetch_id, run_id):
    # Both July and August revised in the October report BEFORE we ever compute.
    add_obs(cpi, fetch_id, "cpi_headline", "2026-07-01", "2026-10-14", 319.500)
    add_obs(cpi, fetch_id, "cpi_headline", "2026-08-01", "2026-10-14", 321.000)
    process(cpi, cfg, run_id)
    first = cpi.execute("SELECT value FROM release_value_candidates "
                        "WHERE origin = 'computed'").fetchone()[0]
    assert first == 0.3                              # as known on 2026-09-11
    rev = cpi.execute("SELECT as_of_date, value_rounded FROM release_value_revisions").fetchall()
    assert [tuple(r) for r in rev] == [("2026-10-14", 0.5)]   # kept separately


def test_vintage_after_release_date_is_not_computed(cfg, conn, fetch_id, run_id):
    seed_cpi_history(conn, fetch_id)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", "2026-09-12", 320.960)
    add_figure(conn)
    process(conn, cfg, run_id)
    row = status_row(conn)
    assert row["computed_value"] is None
    assert "after the release date" in row["last_computation_note"]


def test_clipped_first_vintage_is_not_a_first_print(cfg, conn, fetch_id, run_id):
    seed_cpi_history(conn, fetch_id)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, 320.960,
            basis="source_realtime_clipped")
    add_figure(conn)
    process(conn, cfg, run_id)
    assert "true first print is unknown" in status_row(conn)["last_computation_note"]


def test_missing_first_print_is_not_computed(cfg, conn, fetch_id, run_id):
    seed_cpi_history(conn, fetch_id)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, None)
    add_figure(conn)
    process(conn, cfg, run_id)
    assert "missing in the source" in status_row(conn)["last_computation_note"]


def test_future_release_awaits(cfg, conn, run_id):
    add_figure(conn, period="2099-01", release_time="2099-02-11T13:30:00Z")
    process(conn, cfg, run_id)
    row = status_row(conn)
    assert row["final_status"] == "awaiting_release"
    assert "awaiting release" in row["last_computation_note"]


def test_rerun_is_idempotent(cfg, cpi, run_id, fetch_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    process(cpi, cfg, run_id)
    (res, problems) = process(cpi, cfg, run_id)
    assert res["compute"].unchanged == 1 and res["validate"].new == 0 and problems == []
    assert cpi.execute("SELECT COUNT(*) FROM release_value_candidates").fetchone()[0] == 2
    assert cpi.execute("SELECT COUNT(*) FROM release_validations").fetchone()[0] == 1


def test_unreproducible_first_print_is_flagged_not_replaced(cfg, cpi, fetch_id, run_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    process(cpi, cfg, run_id)
    assert status_row(cpi)["final_status"] == "match"
    # a vintage dated BEFORE the release appears later: the stored first print
    # can no longer be reproduced from the data
    add_obs(cpi, fetch_id, "cpi_headline", "2026-08-01", "2026-09-05", 999.0)
    _, problems = process(cpi, cfg, run_id)
    assert any("can no longer be reproduced" in p for p in problems)
    assert cpi.execute("SELECT value FROM release_value_candidates "
                       "WHERE origin = 'computed'").fetchone()[0] == 0.3
    # a value that cannot be reproduced is no longer verified, even though it matched
    row = status_row(cpi)
    assert (row["final_status"], row["verified_value"]) == ("unreproducible", None)


def test_method_change_without_version_bump_is_refused(cfg, conn):
    register_method_version(conn, cfg.release_methods)
    edited = replace(cfg.release_methods, config_hash="different")
    with pytest.raises(MethodVersionError, match="not bumped"):
        register_method_version(conn, edited)


def test_new_method_version_keeps_old_values(cfg, cpi, run_id):
    process(cpi, cfg, run_id)
    rm2 = ReleaseMethods(version="rm-2", config_hash="h2", methods=cfg.release_methods.methods)
    process(cpi, replace(cfg, release_methods=rm2), run_id)
    versions = [r[0] for r in cpi.execute("SELECT method_version FROM release_value_candidates "
                                          "ORDER BY candidate_id")]
    assert versions == ["rm-1", "rm-2"]
    assert status_row(cpi)["method_version"] == "rm-2"


# --- validation outcomes -------------------------------------------------------

def test_match_is_verified_crosschecked(cfg, cpi, run_id, fetch_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert (row["final_status"], row["verification"], row["verified_value"]) == (
        "match", "verified_crosschecked", 0.3)


def test_mismatch_is_flagged_and_both_values_kept(cfg, cpi, run_id, fetch_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert row["final_status"] == "discrepancy" and row["verified_value"] is None
    assert (row["computed_value"], row["manual_value"]) == (0.3, 0.5)
    queue = cpi.execute("SELECT final_status, priority FROM v_release_review_queue").fetchall()
    assert [tuple(r) for r in queue] == [("discrepancy", 1)]


def test_rounding_difference_is_not_a_discrepancy(cfg, conn, fetch_id, run_id):
    # August 320.799 vs July 320.000 -> +0.2497%, which rounds to 0.2; the
    # published 0.3 is still consistent once input rounding is allowed for.
    seed_cpi_history(conn, fetch_id)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, 320.799)
    add_figure(conn)
    add_manual(conn, run_id, fetch_id, 1, "0.3")
    process(conn, cfg, run_id)
    row = status_row(conn)
    assert row["computed_value"] == 0.2
    assert (row["final_status"], row["verification"], row["verified_value"]) == (
        "within_rounding", "verified_crosschecked", 0.3)


def test_computed_only_from_official_source_is_verified_computed(cfg, cpi, run_id):
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert (row["final_status"], row["verification"], row["verified_value"]) == (
        "computed_only", "verified_computed", 0.3)
    assert row["verified_origin"] == "computed from official first-release vintage data"


def test_computed_missing_is_unverified(cfg, conn, run_id, fetch_id):
    add_figure(conn)                    # no vintage data at all
    add_manual(conn, run_id, fetch_id, 1, "0.3")
    process(conn, cfg, run_id)
    row = status_row(conn)
    assert (row["final_status"], row["verification"], row["verified_value"]) == (
        "manual_only", "unverified", None)
    assert "no vintage data" in row["last_computation_note"]


def test_both_missing_after_release_is_reported(cfg, conn, run_id):
    add_figure(conn)
    process(conn, cfg, run_id)
    assert status_row(conn)["final_status"] == "missing"


def test_unverified_values_excluded_from_surprises(cfg, cpi, run_id, fetch_id, conn):
    conn.execute("INSERT INTO sources (source_id, name, source_tier, collection_method, "
                 "terms_checked_at) VALUES ('TEST_CONSENSUS', 't', 'third_party', "
                 "'manual_entry', '2026-10-10')")
    conn.execute("INSERT INTO forecasts (value_id, forecast_source, forecast_value, collected_at, "
                 "collection_method) VALUES (1, 'TEST_CONSENSUS', 0.2, 'x', 'manual_entry')")
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    assert cpi.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 0
    mon = cpi.execute("SELECT final_status, actual, surprise, computed_value, manual_value "
                      "FROM v_release_monitor").fetchone()
    assert tuple(mon) == ("discrepancy", None, None, 0.3, 0.5)


# --- overrides -------------------------------------------------------------------

def test_override_resolves_discrepancy_with_audit_trail(cfg, cpi, run_id, fetch_id, store,
                                                        tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    rep = _load(cpi, cfg, store, run_id, tmp_path, _resolve("OVR-1", "use_computed"))
    assert rep.status == "ok" and rep.inserted == 1
    row = status_row(cpi)
    assert (row["final_status"], row["verification"], row["verified_value"]) == (
        "overridden", "verified_override", 0.3)
    o = cpi.execute("SELECT * FROM release_overrides").fetchone()
    a = cpi.execute("SELECT * FROM override_approvals").fetchone()
    assert (o["decided_by"], a["approved_by"]) == (DECIDER, "Reviewer")
    assert o["raw_sha256"] and o["validation_id"] and o["evidence_source"] == "BLS"
    # both original candidates are still there, untouched
    assert cpi.execute("SELECT COUNT(*) FROM release_value_candidates").fetchone()[0] == 2


def test_override_refused_when_values_already_agree(cfg, cpi, run_id, fetch_id, store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    process(cpi, cfg, run_id)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_manual", manual="0.3")), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "no override needed" in rep.errors[0]


def test_use_value_requires_evidence_and_published_precision(cfg, cpi, run_id, fetch_id, store,
                                                             tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_value", value="0.45", reason="source_correction",
                           evidence="")), run_id=run_id, now=NOW)
    errors = " ".join(rep.errors)
    assert rep.status == "rejected" and "decimals" in errors and "evidence" in errors


def test_revoke_returns_figure_to_review(cfg, cpi, run_id, fetch_id, store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    rep = _load(cpi, cfg, store, run_id, tmp_path, _resolve("OVR-1", "use_computed"),
                _revoke("OVR-2", "OVR-1"))
    assert rep.inserted == 2
    row = status_row(cpi)
    assert (row["final_status"], row["verification"]) == ("discrepancy", "unverified")


def test_second_active_override_is_refused(cfg, cpi, run_id, fetch_id, store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    ml.load_overrides(cpi, store, _override_file(tmp_path, _resolve("OVR-1", "use_computed")),
                      run_id=run_id, now=NOW)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_computed"), _resolve("OVR-2", "use_manual")),
        run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "revoke it first" in rep.errors[0]
    assert cpi.execute("SELECT COUNT(*) FROM release_overrides").fetchone()[0] == 1


def test_editing_a_recorded_override_is_a_conflict(cfg, cpi, run_id, fetch_id, store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    ml.load_overrides(cpi, store, _override_file(tmp_path, _resolve("OVR-1", "use_computed")),
                      run_id=run_id, now=NOW)
    rep = ml.load_overrides(cpi, store, _override_file(tmp_path, _resolve("OVR-1", "use_manual")),
                            run_id=run_id, now=NOW)
    assert len(rep.conflicts) == 1
    assert cpi.execute("SELECT resolution FROM release_overrides").fetchone()[0] == "use_computed"


def test_override_goes_stale_when_reviewed_values_change(cfg, conn, run_id, fetch_id, store,
                                                         tmp_path):
    # 1) only a manual value exists; analyst verifies it against BLS and approves it
    add_figure(conn)
    add_manual(conn, run_id, fetch_id, 1, "0.5")
    process(conn, cfg, run_id)
    _load(conn, cfg, store, run_id, tmp_path,
          _resolve("OVR-1", "use_manual", reason="verified_against_primary_source", computed=""))
    assert status_row(conn)["verification"] == "verified_override"
    # 2) vintage data arrives; computation disagrees with the approved value
    seed_cpi_history(conn, fetch_id)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, 320.960)
    process(conn, cfg, run_id)
    row = status_row(conn)
    assert (row["final_status"], row["verified_value"]) == ("discrepancy", None)
    stale = conn.execute("SELECT stale_overrides FROM v_release_review_queue").fetchone()[0]
    assert stale == "OVR-1"


# --- actuals file --------------------------------------------------------------

ACT_HEADER = ",".join(ml.ACTUAL_COLUMNS)


def _actuals(tmp_path, value="0.3", time="2026-09-11T12:30:00Z", source="BLS"):
    p = tmp_path / "actuals.csv"
    p.write_text(f"{ACT_HEADER}\nCPI,2026-08,{time},BLS,cpi_headline,mom_pct,percent,1,"
                 f"{value},{source},\n")
    return p


def test_actuals_file_creates_manual_candidate(cfg, conn, store, run_id, tmp_path):
    rep = ml.load_actuals(conn, store, _actuals(tmp_path), run_id=run_id, now=NOW)
    assert rep.status == "ok" and rep.inserted == 1
    row = conn.execute("SELECT value_text, source_id FROM release_value_candidates").fetchone()
    assert tuple(row) == ("0.3", "BLS")


def test_actual_with_extra_precision_rejected(conn, store, run_id, tmp_path):
    rep = ml.load_actuals(conn, store, _actuals(tmp_path, value="0.27"), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "more decimals" in rep.errors[0]


def test_actual_for_future_release_rejected(conn, store, run_id, tmp_path):
    rep = ml.load_actuals(conn, store, _actuals(tmp_path, time="2099-01-01T13:30:00Z"),
                          run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "in the future" in rep.errors[0]


def test_changed_actual_is_conflict_not_overwrite(conn, store, run_id, tmp_path):
    ml.load_actuals(conn, store, _actuals(tmp_path, value="0.3"), run_id=run_id, now=NOW)
    rep = ml.load_actuals(conn, store, _actuals(tmp_path, value="0.4"), run_id=run_id, now=NOW)
    assert len(rep.conflicts) == 1 and "overrides.csv" in rep.conflicts[0]
    assert conn.execute("SELECT value_text FROM release_value_candidates").fetchone()[0] == "0.3"


# --- release monitor: previous value as published vs as revised ----------------

def test_monitor_separates_previous_as_published_from_revised(cfg, conn, fetch_id, run_id):
    seed_cpi_history(conn, fetch_id)            # July 2026 first printed 2026-08-10
    # July figure: 320.000 / 319.269 -> +0.229% -> 0.2
    add_figure(conn, value_id=1, period="2026-07", release_time="2026-08-10T12:30:00Z")
    add_manual(conn, run_id, fetch_id, 1, "0.2")
    # September report: August first print AND a revision to July (320.100)
    add_obs(conn, fetch_id, "cpi_headline", "2026-07-01", CPI_RELEASE_DATE, 320.100)
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, 320.960)
    add_figure(conn, value_id=2, period="2026-08")
    add_manual(conn, run_id, fetch_id, 2, "0.3")
    process(conn, cfg, run_id)

    row = conn.execute("SELECT actual, verification, previous_as_published, previous_revised "
                       "FROM v_release_monitor WHERE value_id = 2").fetchone()
    assert tuple(row) == (0.3, "verified_crosschecked", 0.2, 0.3)
    # July's own first print is untouched by its later revision
    assert status_row(conn, 1)["verified_value"] == 0.2


# --- regressions from the independent review -----------------------------------

def test_level_typo_of_one_unit_is_a_discrepancy(cfg, conn, fetch_id, run_id):
    # unemployment rate published to 1 decimal; stored series also has 1 decimal,
    # so 4.3 and 4.4 can never be the same underlying number
    for i in range(13):
        m = f"{2025 + (8 + i) // 12}-{(8 + i) % 12 + 1:02d}-01"
        y, mo = int(m[:4]), int(m[5:7])
        add_obs(conn, fetch_id, "unemployment_rate", m,
                f"{y + (mo == 12)}-{mo % 12 + 1:02d}-05", round(4.0 + (i % 4) * 0.1, 1))
    add_obs(conn, fetch_id, "unemployment_rate", "2026-09-01", "2026-10-02", 4.3)
    add_figure(conn, release_name="Employment Situation", period="2026-09",
               release_time="2026-10-02T12:30:00Z", indicator="unemployment_rate",
               measure="rate_pct")
    add_manual(conn, run_id, fetch_id, 1, "4.4")
    process(conn, cfg, run_id)
    row = status_row(conn)
    assert (row["computed_value"], row["final_status"]) == (4.3, "discrepancy")


def test_override_refused_if_values_changed_since_review(cfg, cpi, run_id, fetch_id, store,
                                                         tmp_path):
    # analyst reviewed computed 0.3 vs manual 0.5, but a new method version now
    # computes a different value before the override is first loaded
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_computed", computed="0.4")), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "values changed since review" in rep.errors[0]
    assert status_row(cpi)["final_status"] == "discrepancy"


def test_repointing_a_recorded_override_is_a_conflict(cfg, cpi, run_id, fetch_id, store,
                                                      tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    ml.load_overrides(cpi, store, _override_file(tmp_path, _resolve("OVR-1", "use_computed")),
                      run_id=run_id, now=NOW)
    other = dict(FIGURE, reference_period="2026-07")
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_computed", figure=other)), run_id=run_id, now=NOW)
    assert rep.unchanged == 0 and len(rep.conflicts) == 1


def test_override_refused_when_unreproducible(cfg, cpi, fetch_id, run_id, store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    add_obs(cpi, fetch_id, "cpi_headline", "2026-08-01", "2026-09-05", 999.0)
    process(cpi, cfg, run_id)
    rep = ml.load_overrides(cpi, store, _override_file(tmp_path, _resolve("OVR-1", "use_manual")),
                            run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "cannot be reproduced" in rep.errors[0]


def test_invalid_reference_period_rejected_cleanly(conn, store, run_id, tmp_path):
    for bad in ("2026-13", "2026-99", "2026-Q1-05", "2026-02-30"):
        rep = ml.load_actuals(conn, store, _actuals_period(tmp_path, bad), run_id=run_id, now=NOW)
        assert rep.status == "rejected" and "reference_period" in rep.errors[0], bad
    assert conn.execute("SELECT COUNT(*) FROM release_events").fetchone()[0] == 0


def _actuals_period(tmp_path, period):
    p = tmp_path / "actuals.csv"
    p.write_text(f"{ACT_HEADER}\nCPI,{period},2026-09-11T12:30:00Z,BLS,cpi_headline,mom_pct,"
                 "percent,1,0.3,BLS,\n")
    return p


def test_actual_not_attached_when_release_time_conflicts(conn, store, run_id, tmp_path):
    ml.load_actuals(conn, store, _actuals(tmp_path), run_id=run_id, now=NOW)
    p = tmp_path / "actuals2.csv"
    p.write_text(f"{ACT_HEADER}\nCPI,2026-08,2026-09-11T12:30:00Z,BLS,cpi_headline,yoy_pct,"
                 "percent,1,2.9,BLS,\n")
    conn.execute("UPDATE release_events SET release_time_utc = '2026-09-12T12:30:00Z'")
    conn.commit()
    rep = ml.load_actuals(conn, store, p, run_id=run_id, now=NOW)
    assert rep.inserted == 0 and "row skipped" in rep.conflicts[0]


# --- verification levels and integrity (Phase 5 decision 2) -------------------------

def test_computed_only_from_non_official_source_stays_unverified(cfg, cpi, run_id):
    cpi.execute("UPDATE sources SET source_tier = 'third_party' WHERE source_id = 'FRED'")
    cpi.commit()
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert (row["verification"], row["verified_value"]) == ("unverified", None)
    assert "not from an official source" in row["unverified_reason"]


def test_override_refused_when_computed_value_already_verified(cfg, cpi, run_id, store,
                                                               tmp_path):
    process(cpi, cfg, run_id)                     # computed_only -> verified_computed
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_computed", manual="")), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "verified_computed" in rep.errors[0]


def _snapshot_of(conn, target):
    path = conn.execute("SELECT raw_path FROM fetch_log WHERE target = ?",
                        (target,)).fetchone()[0]
    from tests.conftest import raw_root
    return raw_root(conn) / path


def test_tampered_raw_snapshot_makes_value_unreproducible(cfg, cpi, run_id):
    process(cpi, cfg, run_id)
    assert status_row(cpi)["verification"] == "verified_computed"
    f = _snapshot_of(cpi, "cpi_headline:observations")
    f.write_bytes(f.read_bytes().replace(b"320.96", b"320.97"))
    _, problems = process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert (row["final_status"], row["verification"], row["verified_value"]) == (
        "unreproducible", "unverified", None)
    assert "SHA-256" in row["unverified_reason"]
    assert any(p.startswith("reproducibility: computed") for p in problems)


def test_missing_raw_snapshot_makes_value_unreproducible(cfg, cpi, run_id):
    process(cpi, cfg, run_id)
    _snapshot_of(cpi, "cpi_headline:observations").unlink()
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert row["final_status"] == "unreproducible"
    assert "raw snapshot missing" in row["unverified_reason"]


def test_tampered_manual_snapshot_blocks_crosscheck(cfg, cpi, run_id, fetch_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    process(cpi, cfg, run_id)
    assert status_row(cpi)["verification"] == "verified_crosschecked"
    f = _snapshot_of(cpi, "manual:actuals.csv")
    f.write_bytes(f.read_bytes() + b"\n")
    process(cpi, cfg, run_id)
    assert status_row(cpi)["verification"] == "unverified"


def test_tampered_override_snapshot_revokes_verification(cfg, cpi, run_id, fetch_id, store,
                                                         tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    _load(cpi, cfg, store, run_id, tmp_path, _resolve("OVR-1", "use_computed"))
    assert status_row(cpi)["verification"] == "verified_override"
    path = cpi.execute("SELECT raw_path FROM release_overrides").fetchone()[0]
    f = store.root / path
    f.write_bytes(f.read_bytes().replace(b"use_computed", b"use_manual"))
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert (row["final_status"], row["verification"]) == ("unreproducible", "unverified")


def test_integrity_history_only_grows_on_change(cfg, cpi, run_id, fetch_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    process(cpi, cfg, run_id)
    n = cpi.execute("SELECT COUNT(*) FROM integrity_checks").fetchone()[0]
    process(cpi, cfg, run_id)
    assert cpi.execute("SELECT COUNT(*) FROM integrity_checks").fetchone()[0] == n == 2


def test_every_verified_value_is_reproducible_from_stored_references(cfg, cpi, run_id):
    """The stored references alone are enough to re-derive the value."""
    process(cpi, cfg, run_id)
    cand = cpi.execute("SELECT * FROM release_value_candidates WHERE origin = 'computed'"
                       ).fetchone()
    inputs = json.loads(cand["inputs"])
    assert inputs["formula"] == "pct_change" and cand["method_version"] == "rm-1"
    for o in inputs["observations"]:
        fetch = cpi.execute("SELECT raw_path, raw_sha256 FROM fetch_log WHERE fetch_id = ?",
                            (o["fetch_id"],)).fetchone()
        assert fetch["raw_path"] and fetch["raw_sha256"]
    detail = cpi.execute("SELECT detail FROM integrity_checks WHERE subject = 'computed'"
                         ).fetchone()[0]
    assert "recomputed pct_change from 2 stored observation(s) with method rm-1" in detail


# --- overrides need evidence and approval ----------------------------------------

@pytest.mark.parametrize("kwargs, message", [
    ({"evidence": ""}, "evidence_reference must identify"),
    ({"evidence": "-"}, "evidence_reference must identify"),
    ({"evidence_source": ""}, "evidence_source is required"),
    ({"evidence_source": "NOPE"}, "is not registered"),
    ({"justification": "ok"}, "justification is required"),
    ({"approved_by": ""}, "approved_by is required"),
    ({"approved_at": "2026-10-13"}, "UTC timestamp"),
    ({"approved_at": "2026-10-12T09:00:00Z"}, "approved_at is before decided_at"),
    ({"approved_at": "2099-01-01T00:00:00Z"}, "in the future"),
])
def test_override_requires_evidence_and_approval(cfg, cpi, run_id, fetch_id, store, tmp_path,
                                                 kwargs, message):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_computed", **kwargs)), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and message in " ".join(rep.errors)


# --- surprises need a verified actual AND a validated pre-release forecast ---------

FC_HEADER = ",".join(ml.FORECAST_COLUMNS)


def _forecast(conn, store, run_id, tmp_path, as_of="2026-09-10T20:00:00Z", value="0.2"):
    conn.execute("INSERT OR IGNORE INTO sources (source_id, name, source_tier, collection_method, "
                 "terms_checked_at) VALUES ('TEST_CONSENSUS', 'test-only provider', "
                 "'third_party', 'manual_entry', '2026-10-10')")
    conn.commit()
    p = tmp_path / "forecasts.csv"
    p.write_text(f"{FC_HEADER}\nCPI,2026-08,2026-09-11T12:30:00Z,BLS,cpi_headline,mom_pct,"
                 f"percent,1,{value},TEST_CONSENSUS,{as_of},\n")
    return ml.load_forecasts(conn, store, p, run_id=run_id)


def test_verified_surprise_with_validated_pre_release_forecast(cfg, cpi, run_id, fetch_id,
                                                               store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.3")
    assert _forecast(cpi, store, run_id, tmp_path).status == "ok"
    process(cpi, cfg, run_id)
    row = cpi.execute("SELECT actual, actual_verification, forecast_value, raw_surprise "
                      "FROM v_surprises").fetchone()
    assert tuple(row) == (0.3, "verified_crosschecked", 0.2, 0.1)


def test_computed_actual_also_yields_verified_surprise(cfg, cpi, run_id, store, tmp_path):
    _forecast(cpi, store, run_id, tmp_path)
    process(cpi, cfg, run_id)
    row = cpi.execute("SELECT actual_verification, raw_surprise FROM v_surprises").fetchone()
    assert tuple(row) == ("verified_computed", 0.1)


@pytest.mark.parametrize("as_of, reason", [
    ("", "no forecast_as_of"),
    ("2026-09-11", "release day with no time"),
])
def test_forecast_that_cannot_be_shown_pre_release_blocks_surprise(cfg, cpi, run_id, store,
                                                                   tmp_path, as_of, reason):
    assert _forecast(cpi, store, run_id, tmp_path, as_of=as_of).status == "ok"
    process(cpi, cfg, run_id)
    assert status_row(cpi)["verification"] == "verified_computed"   # actual is fine
    assert cpi.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 0
    fc = cpi.execute("SELECT forecast_check, forecast_check_detail FROM v_forecast_status"
                     ).fetchone()
    assert fc[0] == "fail" and reason in fc[1]
    mon = cpi.execute("SELECT actual, surprise FROM v_release_monitor").fetchone()
    assert tuple(mon) == (0.3, None)


def test_tampered_forecast_snapshot_blocks_surprise(cfg, cpi, run_id, store, tmp_path):
    _forecast(cpi, store, run_id, tmp_path)
    process(cpi, cfg, run_id)
    assert cpi.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 1
    f = _snapshot_of(cpi, "manual:forecasts.csv")
    f.write_bytes(f.read_bytes().replace(b",0.2,", b",0.1,"))
    process(cpi, cfg, run_id)
    assert cpi.execute("SELECT COUNT(*) FROM v_surprises").fetchone()[0] == 0


# --- regressions from the second independent review ----------------------------------

@pytest.mark.parametrize("value", ["25e-2", "inf", "-Infinity", "0.2%"])
def test_forecast_must_be_a_plain_number(cpi, store, run_id, tmp_path, value):
    rep = _forecast(cpi, store, run_id, tmp_path, value=value)
    assert rep.status == "rejected" and "plain number" in " ".join(rep.errors)


def test_backfilled_vintages_that_change_precision_are_flagged(cfg, conn, fetch_id, run_id):
    # Only 5 months of history at first: precision cannot be inferred, so the
    # strictest (6 decimals) is used. Later, older vintages (all published
    # before the release) are backfilled and the inferred precision becomes 3.
    # The stored bounds no longer reproduce, so the value must not stay verified.
    for i, m in enumerate(["2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]):
        add_obs(conn, fetch_id, "cpi_headline", f"{m}-01", f"{m[:5]}{int(m[5:]) + 1:02d}-10",
                round(317.076 + i * 0.731, 3))
    add_obs(conn, fetch_id, "cpi_headline", "2026-08-01", CPI_RELEASE_DATE, 320.960)
    add_figure(conn)
    process(conn, cfg, run_id)
    assert status_row(conn)["verification"] == "verified_computed"
    for i in range(9):
        m = 2025 * 12 + 5 + i          # 2025-06 .. 2026-02
        y, mo = divmod(m, 12)
        add_obs(conn, fetch_id, "cpi_headline", f"{y}-{mo + 1:02d}-01", "2026-03-01",
                round(311.0 + i * 0.123, 3))
    _, problems = process(conn, cfg, run_id)
    row = status_row(conn)
    assert (row["final_status"], row["verification"]) == ("unreproducible", "unverified")
    assert any("reproducib" in p for p in problems)


def test_run_that_stops_early_leaves_nothing_verified(cfg, cpi, run_id):
    from src.storage import db
    process(cpi, cfg, run_id)
    assert status_row(cpi)["verification"] == "verified_computed"
    new_run = db.start_run(cpi, trigger="test")
    edited = replace(cfg, release_methods=replace(cfg.release_methods, config_hash="edited"))
    _, problems = process(cpi, edited, new_run)
    assert any("not bumped" in p for p in problems)
    row = status_row(cpi)
    assert row["verification"] == "unverified" and "did not complete" in row["unverified_reason"]


def test_override_cannot_predate_the_release(cfg, cpi, run_id, fetch_id, store, tmp_path):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    row = _resolve("OVR-1", "use_computed").replace("2026-10-13", "2020-01-01")
    rep = ml.load_overrides(cpi, store, _override_file(tmp_path, row), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "before the release" in " ".join(rep.errors)


def test_evidence_from_third_party_source_rejected(cfg, cpi, run_id, fetch_id, store,
                                                   tmp_path):
    cpi.execute("INSERT INTO sources (source_id, name, source_tier, collection_method, "
                "terms_checked_at) VALUES ('AGGREGATOR', 'calendar site', 'third_party', "
                "'manual_entry', '2026-10-10')")
    cpi.commit()
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, _resolve("OVR-1", "use_computed", evidence_source="AGGREGATOR")),
        run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "primary source" in rep.errors[0]


# --- self-approved vs independently approved overrides (Phase 5 decision 3) ------

def _discrepancy(cpi, cfg, run_id, fetch_id):
    add_manual(cpi, run_id, fetch_id, 1, "0.5")
    process(cpi, cfg, run_id)


def test_self_approved_override_is_verified_and_labelled(cfg, cpi, run_id, fetch_id, store,
                                                         tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    _load(cpi, cfg, store, run_id, tmp_path, _resolve("OVR-1", "use_computed", approved_by=DECIDER))
    row = status_row(cpi)
    assert (row["verification"], row["override_approval_status"], row["actual_assurance"]) == (
        "verified_override", "self_approved", "override_self_approved")
    assert row["verified_origin"] == "override:use_computed (self_approved)"


def test_independent_approval_is_distinguished(cfg, cpi, run_id, fetch_id, store, tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    _load(cpi, cfg, store, run_id, tmp_path, _resolve("OVR-1", "use_computed"))
    row = status_row(cpi)
    assert (row["override_approval_status"], row["actual_assurance"]) == (
        "independently_approved", "override_independent")


def test_later_independent_approval_upgrades_without_editing(cfg, cpi, run_id, fetch_id, store,
                                                             tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    first = _resolve("OVR-1", "use_computed", approved_by=DECIDER)
    _load(cpi, cfg, store, run_id, tmp_path, first)
    assert status_row(cpi)["override_approval_status"] == "self_approved"
    rep = _load(cpi, cfg, store, run_id, tmp_path, first, _approve("APR-1", "OVR-1"))
    assert (rep.inserted, rep.unchanged) == (1, 1)
    assert status_row(cpi)["actual_assurance"] == "override_independent"
    audit = [tuple(r) for r in cpi.execute(
        "SELECT entry_id, entry_type, person, approval_type FROM v_override_audit "
        "ORDER BY entry_time, entry_type DESC")]
    assert audit == [("OVR-1", "resolve", DECIDER, None),
                     ("OVR-1", "approval", DECIDER, "self"),
                     ("APR-1", "approval", "Independent Reviewer", "independent")]


def test_same_person_cannot_approve_twice(cfg, cpi, run_id, fetch_id, store, tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    first = _resolve("OVR-1", "use_computed", approved_by=DECIDER)
    _load(cpi, cfg, store, run_id, tmp_path, first)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, first, _approve("APR-1", "OVR-1", approved_by=DECIDER)), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "already approved" in rep.errors[0]


def test_cannot_approve_a_revoked_override(cfg, cpi, run_id, fetch_id, store, tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    rows = [_resolve("OVR-1", "use_computed", approved_by=DECIDER), _revoke("OVR-2", "OVR-1")]
    _load(cpi, cfg, store, run_id, tmp_path, *rows)
    rep = ml.load_overrides(cpi, store, _override_file(
        tmp_path, *rows, _approve("APR-1", "OVR-1")), run_id=run_id, now=NOW)
    assert rep.status == "rejected" and "revoked" in rep.errors[0]


def _policy(cfg, version, required):
    from src.config import ApprovalPolicy
    return replace(cfg, approval_policy=ApprovalPolicy(version, required, f"hash-{version}"))


def test_independent_reviewer_requirement_can_be_switched_on(cfg, cpi, run_id, fetch_id, store,
                                                             tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    first = _resolve("OVR-1", "use_computed", approved_by=DECIDER)
    _load(cpi, cfg, store, run_id, tmp_path, first)
    assert status_row(cpi)["verification"] == "verified_override"

    strict = _policy(cfg, "ap-2", True)
    process(cpi, strict, run_id)
    row = status_row(cpi)
    assert (row["final_status"], row["verification"]) == ("overridden", "unverified")
    assert "awaiting independent approval (policy ap-2)" in row["unverified_reason"]
    queue = cpi.execute("SELECT value_id FROM v_release_review_queue").fetchall()
    assert len(queue) == 1

    ml.load_overrides(cpi, store, _override_file(tmp_path, first, _approve("APR-1", "OVR-1")),
                      run_id=run_id, now=NOW)
    process(cpi, strict, run_id)
    assert status_row(cpi)["actual_assurance"] == "override_independent"
    history = [r[0] for r in cpi.execute("SELECT version FROM approval_policy_activations "
                                         "ORDER BY activation_id")]
    assert history == ["ap-1", "ap-2"]


def test_policy_change_without_version_bump_is_refused(cfg, cpi, run_id):
    process(cpi, cfg, run_id)
    edited = replace(cfg, approval_policy=replace(cfg.approval_policy, config_hash="edited"))
    _, problems = process(cpi, edited, run_id)
    assert any("approval_policy.yaml changed" in p for p in problems)


def test_override_preserves_original_values(cfg, cpi, run_id, fetch_id, store, tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    _load(cpi, cfg, store, run_id, tmp_path,
          _resolve("OVR-1", "use_value", value="0.4", reason="source_correction"))
    row = status_row(cpi)
    assert (row["verified_value"], row["computed_value"], row["manual_value"]) == (0.4, 0.3, 0.5)
    stored = [tuple(r) for r in cpi.execute(
        "SELECT origin, value FROM release_value_candidates ORDER BY candidate_id")]
    assert stored == [("manual", 0.5), ("computed", 0.3)]


def test_override_status_propagates_to_surprises(cfg, cpi, run_id, fetch_id, store, tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    _forecast(cpi, store, run_id, tmp_path)
    first = _resolve("OVR-1", "use_computed", approved_by=DECIDER)
    _load(cpi, cfg, store, run_id, tmp_path, first)
    row = cpi.execute("SELECT actual, raw_surprise, actual_assurance, actual_override_id, "
                      "actual_override_approval FROM v_surprises").fetchone()
    assert tuple(row) == (0.3, 0.1, "override_self_approved", "OVR-1", "self_approved")
    mon = cpi.execute("SELECT actual_assurance, override_approval_status, surprise "
                      "FROM v_release_monitor").fetchone()
    assert tuple(mon) == ("override_self_approved", "self_approved", 0.1)
    # an independent approval changes the label on the SAME surprise
    _load(cpi, cfg, store, run_id, tmp_path, first, _approve("APR-1", "OVR-1"))
    assert cpi.execute("SELECT actual_assurance FROM v_surprises").fetchone()[0] == \
        "override_independent"


def test_tampered_approval_snapshot_makes_override_unreproducible(cfg, cpi, run_id, fetch_id,
                                                                  store, tmp_path):
    _discrepancy(cpi, cfg, run_id, fetch_id)
    first = _resolve("OVR-1", "use_computed", approved_by=DECIDER)
    _load(cpi, cfg, store, run_id, tmp_path, first, _approve("APR-1", "OVR-1"))
    path = cpi.execute("SELECT raw_path FROM override_approvals WHERE approval_id = 'APR-1'"
                       ).fetchone()[0]
    f = store.root / path
    f.write_bytes(f.read_bytes().replace(b"Independent Reviewer", b"Someone Else"))
    process(cpi, cfg, run_id)
    row = status_row(cpi)
    assert (row["final_status"], row["verification"]) == ("unreproducible", "unverified")
