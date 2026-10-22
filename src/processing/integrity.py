"""
Reproducibility and validation checks (integrity_checks table).

Every pipeline run re-checks each value that could be accepted, using only
what is stored: its source, vintage, formula version and raw-data
reference. A value is accepted (verified) only while its latest check passes.

  computed  1. the inputs are the right ones: the method's input series,
               the reference period (and base period), the first-release
               vintage dated on the release date, and the latest base
               vintage as of that date
            2. every input observation still exists, unchanged, in the
               observations table (same indicator, date, vintage)
            3. its fetch_log row points to a raw snapshot whose SHA-256
               still matches, and the observation (date, vintage date,
               value) is present in that snapshot
            4. the input precision, re-derived from data public on the
               release date, equals the stored one
            5. re-running the formula (active method version) gives the
               stored value, rounded value and rounding bounds
  manual    the entered value is present in its raw CSV snapshot (SHA-256 verified)
  override  the decision is present, unchanged, in its raw CSV snapshot
  forecast  provider registered with verified terms; forecast_as_of present
            and before the release; precision <= published; value present
            in its raw CSV snapshot

Severity of a failure
  reproducibility  stored data or references no longer support the value
                   (fails the run)
  data_quality     the value is intact but does not meet a rule, e.g. a
                   forecast without an as-of time (reported, not a run failure)

A check row is appended only when a subject's result changes, so the table
is a change history and its latest row per subject is the current state.
A completed sweep of all subjects is recorded in integrity_sweeps; nothing
counts as verified unless the LATEST pipeline run completed a sweep, so a
run that stops early can never leave old "pass" results in force.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.config import ReleaseMethods
from src.processing import point_in_time as pit
from src.processing.point_in_time import release_date_local
from src.processing.release_values import _Precision
from src.processing.transforms import (TransformError, apply, half_unit, round_published,
                                       shift_month)
from src.storage.db import utc_now

SUBJECTS = ("computed", "manual", "override", "forecast")
REPRO, QUALITY = "reproducibility", "data_quality"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str
    severity: str | None = None


def _pass(detail: str) -> Outcome:
    return Outcome(True, detail)


def _fail(detail: str, severity: str = REPRO) -> Outcome:
    return Outcome(False, detail, severity)


@dataclass
class IntegrityReport:
    checked: int = 0
    passed: int = 0
    recorded: int = 0
    failures: list[tuple[str, str, str, str]] = field(default_factory=list)
    # (subject, subject_ref, severity, detail)


# ---------------------------------------------------------------------
# Raw snapshots
# ---------------------------------------------------------------------

def _raw_body(raw_root: Path | None, raw_path: str | None, sha: str | None):
    """Return (bytes, None) if the snapshot exists and matches its hash."""
    if raw_root is None:
        return None, "raw data directory not configured"
    if not raw_path or not sha:
        return None, "no raw snapshot reference recorded"
    f = Path(raw_root) / raw_path
    if not f.is_file():
        return None, f"raw snapshot missing: {raw_path}"
    body = f.read_bytes()
    if hashlib.sha256(body).hexdigest() != sha:
        return None, f"raw snapshot {raw_path} no longer matches its recorded SHA-256"
    return body, None


def _csv_rows(body: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]


_PLAIN_NUMBER = __import__("re").compile(r"^-?\d+(\.\d+)?$")


def _same_number(text: str, value: float) -> bool:
    """Plain decimal text equal to value (FRED's '.' and 'inf'/'1e5' never match)."""
    if not isinstance(text, str) or not _PLAIN_NUMBER.match(text.strip()):
        return False
    return math.isclose(float(text), value, rel_tol=0, abs_tol=1e-12)


def _close(a, b) -> bool:
    return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def _fetch(conn, fetch_id):
    return conn.execute("SELECT * FROM fetch_log WHERE fetch_id = ?", (fetch_id,)).fetchone()


# ---------------------------------------------------------------------
# Individual checks (pure with respect to the database: read only)
# ---------------------------------------------------------------------

def check_computed(conn, cand, fig, rm: ReleaseMethods, raw_root) -> Outcome:
    method = rm.methods.get((fig["indicator_id"], fig["measure"]))
    if method is None or cand["method_version"] != rm.version:
        return _fail(f"method {cand['method_version']} is not the active method {rm.version}")
    inputs = json.loads(cand["inputs"])
    k = inputs.get("input_precision_decimals")
    if k is None:
        return _fail("stored inputs have no input precision")
    as_of, inp = cand["as_of_date"], method["input"]

    # 1. the right inputs
    target = f"{fig['reference_period']}-01"
    expected = {"current": target}
    if method["formula"] != "level":
        expected["base"] = shift_month(target, -method["lag_months"])
    obs = {o.get("role"): o for o in inputs.get("observations", [])}
    if set(obs) != set(expected):
        return _fail(f"stored inputs have roles {sorted(obs)}, method needs {sorted(expected)}")
    for role, o in obs.items():
        if o["indicator_id"] != inp or o["observation_date"] != expected[role]:
            return _fail(f"{role} input is {o['indicator_id']} {o['observation_date']}, method "
                         f"needs {inp} {expected[role]}")
    if as_of != release_date_local(fig["release_time_utc"]):
        return _fail(f"as-of date {as_of} is not the release date")
    first = pit.first_vintage(conn, inp, target)
    if first is None or first.vintage_date != as_of or first.vintage_basis != "source_realtime" \
            or obs["current"]["vintage_date"] != as_of:
        return _fail("current input is not the first-release vintage dated on the release date")
    if "base" in obs:
        latest = pit.value_as_of(conn, inp, expected["base"], as_of)
        if latest is None or latest.vintage_date != obs["base"]["vintage_date"]:
            return _fail("base input is not the latest vintage public on the release date")
    k_now, _ = _Precision(conn).get(inp, as_of)
    if k_now != k:
        return _fail(f"input precision re-derived as {k_now} decimals, stored {k}")

    values, raw_paths = {}, set()
    for o in obs.values():
        key = f"{o['indicator_id']} {o['observation_date']} vintage {o['vintage_date']}"
        row = conn.execute("""
            SELECT value, value_status, vintage_basis, fetch_id FROM observations
            WHERE indicator_id = ? AND observation_date = ? AND vintage_date = ?
        """, (o["indicator_id"], o["observation_date"], o["vintage_date"])).fetchone()
        if row is None:
            return _fail(f"stored observation no longer exists: {key}")
        if row["value"] is None or not math.isclose(row["value"], o["value"], abs_tol=1e-12):
            return _fail(f"stored observation changed: {key}")
        if "fetch_id" in o and o["fetch_id"] != row["fetch_id"]:
            return _fail(f"observation {key} points to a different fetch than recorded")
        fetch = _fetch(conn, row["fetch_id"])
        if fetch is None:
            return _fail(f"no fetch_log entry for {key}")
        if fetch["source_id"] != cand["source_id"]:
            return _fail(f"{key} came from {fetch['source_id']}, not {cand['source_id']}")
        if fetch["source_id"] == "FRED" and not fetch["target"].startswith(f"{o['indicator_id']}:"):
            return _fail(f"{key}: raw snapshot belongs to {fetch['target']}")
        body, err = _raw_body(raw_root, fetch["raw_path"], fetch["raw_sha256"])
        if err:
            return _fail(f"{key}: {err}")
        try:
            payload = json.loads(body)
        except ValueError:
            return _fail(f"{key}: raw snapshot is not JSON")
        found = any(x.get("date") == o["observation_date"]
                    and x.get("realtime_start") == o["vintage_date"]
                    and _same_number(x.get("value"), row["value"])
                    for x in payload.get("observations", []))
        if not found:
            return _fail(f"{key} (value {row['value']}) not found in raw snapshot "
                         f"{fetch['raw_path']}")
        values[o["role"]] = row["value"]
        raw_paths.add(fetch["raw_path"])

    if "current" not in values:
        return _fail("stored inputs have no current-period observation")
    try:
        res = apply(method["formula"], values["current"], values.get("base"), half_unit(k))
    except TransformError as exc:
        return _fail(f"recomputation failed: {exc}")
    rounded = float(round_published(res.value, fig["published_decimals"]))
    if not (_close(res.value, cand["value_unrounded"]) and rounded == cand["value"]
            and _close(res.low, cand["bound_low"]) and _close(res.high, cand["bound_high"])):
        return _fail(f"recomputation gives {res.value!r} [{res.low!r}, {res.high!r}] (rounded "
                     f"{rounded}), stored {cand['value_unrounded']!r} [{cand['bound_low']!r}, "
                     f"{cand['bound_high']!r}] (rounded {cand['value']})")
    return _pass(f"recomputed {method['formula']} from {len(values)} stored observation(s) "
                 f"with method {rm.version}; {len(raw_paths)} raw snapshot(s) verified by SHA-256")


def check_manual(conn, cand, fig, raw_root) -> Outcome:
    fetch = _fetch(conn, cand["fetch_id"])
    if fetch is None:
        return _fail("no fetch_log entry for the manual entry")
    body, err = _raw_body(raw_root, fetch["raw_path"], fetch["raw_sha256"])
    if err:
        return _fail(err)
    for r in _csv_rows(body):
        if (r.get("release_name"), r.get("reference_period"), r.get("indicator_id"),
                r.get("measure"), r.get("actual_value"), r.get("actual_source")) == (
                fig["release_name"], fig["reference_period"], fig["indicator_id"],
                fig["measure"], cand["value_text"], cand["source_id"]):
            return _pass(f"value {cand['value_text']} ({cand['source_id']}) present in "
                         f"{fetch['raw_path']} (SHA-256 verified)")
    return _fail(f"value {cand['value_text']} not found in raw snapshot {fetch['raw_path']}")


_DECISION_FIELDS = {  # overrides.csv column -> release_overrides column
    "action": "action", "resolution": "resolution", "override_value": "override_value_text",
    "reviewed_computed": "reviewed_computed_text", "reviewed_manual": "reviewed_manual_text",
    "reason_code": "reason_code", "justification": "justification",
    "evidence_source": "evidence_source", "evidence_reference": "evidence_reference",
    "decided_by": "decided_by", "decided_at": "decided_at",
    "target_override_id": "revokes_override_id",
}
_APPROVAL_FIELDS = {"approved_by": "approved_by", "approved_at": "approved_at"}
_APPROVE_ROW_FIELDS = {"target_override_id": "override_id", "justification": "note",
                       "evidence_source": "evidence_source",
                       "evidence_reference": "evidence_reference"}


def _row_for(body: bytes, entry_id: str):
    return next((r for r in _csv_rows(body) if r.get("override_id") == entry_id), None)


def check_override(conn, ovr, fig, raw_root) -> Outcome:
    """The decision AND every approval of it must be present, unchanged, in the
    raw snapshots of the overrides files that introduced them."""
    body, err = _raw_body(raw_root, ovr["raw_path"], ovr["raw_sha256"])
    if err:
        return _fail(err)
    r = _row_for(body, ovr["override_id"])
    if r is None:
        return _fail(f"override {ovr['override_id']} not found in raw snapshot {ovr['raw_path']}")
    ident = (r.get("release_name"), r.get("reference_period"), r.get("indicator_id"),
             r.get("measure"))
    if ident != (fig["release_name"], fig["reference_period"], fig["indicator_id"],
                 fig["measure"]):
        return _fail(f"raw snapshot names figure {ident}, decision is bound to "
                     f"{fig['release_name']} {fig['reference_period']} "
                     f"{fig['indicator_id']}/{fig['measure']}")
    diffs = [c for c, col in _DECISION_FIELDS.items() if (r.get(c) or "") != (ovr[col] or "")]
    if diffs:
        return _fail(f"recorded decision differs from its raw snapshot in {diffs}")
    if ovr["action"] == "resolve" and not (ovr["evidence_source"]
                                           and (ovr["evidence_reference"] or "").strip()):
        return _fail("override has no supporting evidence")

    approvals = conn.execute("SELECT * FROM override_approvals WHERE override_id = ? "
                             "ORDER BY approved_at, approval_id", (ovr["override_id"],)).fetchall()
    if not approvals:
        return _fail("override has no approval")
    who = []
    for a in approvals:
        abody, aerr = _raw_body(raw_root, a["raw_path"], a["raw_sha256"])
        if aerr:
            return _fail(f"approval {a['approval_id']}: {aerr}")
        ar = _row_for(abody, a["approval_id"])
        if ar is None:
            return _fail(f"approval {a['approval_id']} not found in raw snapshot {a['raw_path']}")
        fields = dict(_APPROVAL_FIELDS)
        if a["approval_id"] != ovr["override_id"]:     # a separate 'approve' entry
            fields.update(_APPROVE_ROW_FIELDS)
            if ar.get("action") != "approve":
                return _fail(f"approval {a['approval_id']} is not an approve entry in its snapshot")
        bad = [c for c, col in fields.items() if (ar.get(c) or "") != (a[col] or "")]
        if bad:
            return _fail(f"approval {a['approval_id']} differs from its raw snapshot in {bad}")
        kind = ("self" if a["approved_by"].strip().lower() == ovr["decided_by"].strip().lower()
                else "independent")
        who.append(f"{a['approved_by']} ({kind}, {a['approved_at']})")
    return _pass(f"decision and {len(approvals)} approval(s) present in raw snapshots "
                 f"(SHA-256 verified): " + "; ".join(who))


def _text_decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def check_forecast(conn, fc, fig, raw_root) -> Outcome:
    src = conn.execute("SELECT * FROM sources WHERE source_id = ?",
                       (fc["forecast_source"],)).fetchone()
    if src is None:
        return _fail(f"forecast source {fc['forecast_source']} is not registered", QUALITY)
    if not src["terms_checked_at"]:
        return _fail(f"forecast source {fc['forecast_source']} has no verified terms", QUALITY)
    asof = fc["forecast_as_of"]
    if not asof:
        return _fail("no forecast_as_of: cannot show the forecast predates the release", QUALITY)
    release = fig["release_time_utc"]
    late = asof >= release if "T" in asof else asof > release_date_local(release)
    if late:
        return _fail(f"forecast_as_of {asof} is not before the release {release}", QUALITY)
    if "T" not in asof and asof == release_date_local(release):
        return _fail(f"forecast_as_of {asof} is the release day with no time; cannot show it "
                     "predates the release (give a UTC timestamp)", QUALITY)

    fetch = _fetch(conn, fc["fetch_id"]) if fc["fetch_id"] is not None else None
    if fetch is None:
        return _fail("no raw snapshot reference for the forecast")
    body, err = _raw_body(raw_root, fetch["raw_path"], fetch["raw_sha256"])
    if err:
        return _fail(err)
    for r in _csv_rows(body):
        if (r.get("release_name"), r.get("reference_period"), r.get("indicator_id"),
                r.get("measure"), r.get("forecast_source")) != (
                fig["release_name"], fig["reference_period"], fig["indicator_id"],
                fig["measure"], fc["forecast_source"]):
            continue
        text = r.get("forecast_value", "")
        if not _PLAIN_NUMBER.match(text):
            return _fail(f"forecast {text!r} is not a plain number", QUALITY)
        if not _same_number(text, fc["forecast_value"]):
            return _fail(f"forecast in raw snapshot is {text}, stored {fc['forecast_value']}")
        if _text_decimals(text) > fig["published_decimals"]:
            return _fail(f"forecast {text} is more precise than the published figure", QUALITY)
        if (r.get("forecast_as_of") or "") != asof:
            return _fail("forecast_as_of differs from its raw snapshot")
        return _pass(f"pre-release forecast ({asof}) present in {fetch['raw_path']} "
                     "(SHA-256 verified); provider terms verified")
    return _fail(f"forecast not found in raw snapshot {fetch['raw_path']}")


# ---------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------

_FIGURE_SQL = """
    SELECT rv.value_id, rv.indicator_id, rv.measure, rv.published_decimals,
           re.release_name, re.reference_period, re.release_time_utc
    FROM release_values rv JOIN release_events re ON re.event_id = rv.event_id
"""


def _record(conn, run_id, value_id, subject, ref, out: Outcome, report: IntegrityReport):
    report.checked += 1
    if out.ok:
        report.passed += 1
    else:
        report.failures.append((subject, ref, out.severity, out.detail))
    last = conn.execute("""SELECT outcome, detail FROM integrity_checks
                           WHERE subject = ? AND subject_ref = ?
                           ORDER BY check_id DESC LIMIT 1""", (subject, ref)).fetchone()
    outcome = "pass" if out.ok else "fail"
    if last is not None and (last[0], last[1]) == (outcome, out.detail):
        return
    conn.execute("""INSERT INTO integrity_checks
                    (run_id, value_id, subject, subject_ref, outcome, severity, detail, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (run_id, value_id, subject, ref, outcome, out.severity, out.detail, utc_now()))
    report.recorded += 1


def run_checks(conn: sqlite3.Connection, *, run_id: int, rm: ReleaseMethods,
               raw_root: Path | None, subjects=SUBJECTS) -> IntegrityReport:
    report = IntegrityReport()
    figs = {r["value_id"]: r for r in conn.execute(_FIGURE_SQL)}
    with conn:
        if "computed" in subjects:
            for c in conn.execute("""SELECT * FROM release_value_candidates
                                     WHERE origin = 'computed' AND method_version = ?""",
                                  (rm.version,)).fetchall():
                out = check_computed(conn, c, figs[c["value_id"]], rm, raw_root)
                _record(conn, run_id, c["value_id"], "computed", str(c["candidate_id"]), out, report)
        if "manual" in subjects:
            for c in conn.execute("SELECT * FROM release_value_candidates "
                                  "WHERE origin = 'manual'").fetchall():
                out = check_manual(conn, c, figs[c["value_id"]], raw_root)
                _record(conn, run_id, c["value_id"], "manual", str(c["candidate_id"]), out, report)
        if "override" in subjects:
            for o in conn.execute("""
                SELECT o.*, v.value_id FROM release_overrides o
                JOIN release_validations v ON v.validation_id = o.validation_id
                WHERE o.action = 'resolve' AND NOT EXISTS (
                    SELECT 1 FROM release_overrides r
                    WHERE r.action = 'revoke' AND r.revokes_override_id = o.override_id)
            """).fetchall():
                _record(conn, run_id, o["value_id"], "override", o["override_id"],
                        check_override(conn, o, figs[o["value_id"]], raw_root), report)
        if "forecast" in subjects:
            for f in conn.execute("SELECT * FROM forecasts").fetchall():
                out = check_forecast(conn, f, figs[f["value_id"]], raw_root)
                _record(conn, run_id, f["value_id"], "forecast", str(f["forecast_id"]), out, report)
        if set(subjects) == set(SUBJECTS):
            conn.execute("""INSERT OR IGNORE INTO integrity_sweeps
                            (run_id, completed_at, checked, failed) VALUES (?, ?, ?, ?)""",
                         (run_id, utc_now(), report.checked, len(report.failures)))
    return report
