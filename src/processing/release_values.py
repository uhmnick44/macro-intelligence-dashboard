"""
Compute each release figure's FIRST PRINT from vintage data, and its later
revisions, using only information available at the time.

First print (one computed candidate per figure and method version)
  inputs are read AS OF the release date (America/New_York). The
  reference period's observation must first appear in a vintage dated
  exactly on the release date; anything else means the release calendar
  or the vintage data is inconsistent, and the figure is NOT computed.

Revisions (release_value_revisions)
  the same formula re-evaluated at every later vintage date where its
  inputs changed. Stored separately so a backtest can never pick up a
  revised number by accident.

Reproducibility check
  every run recomputes stored first prints. A stored value that can no
  longer be reproduced is reported as a conflict, never silently replaced.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import ReleaseMethods
from src.processing import point_in_time as pit
from src.processing.transforms import (TransformError, apply, half_unit, infer_precision,
                                       round_published, shift_month)
from src.storage.db import utc_now

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class NotComputable(Exception):
    pass


class MethodVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Computed:
    value: float           # unrounded
    low: float
    high: float
    rounded: float         # rounded to published decimals
    as_of_date: str
    inputs: dict
    method: str


@dataclass
class ComputeReport:
    computed: int = 0
    unchanged: int = 0
    not_computable: int = 0
    no_method: int = 0
    revisions_added: int = 0
    conflicts: list[str] = field(default_factory=list)


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


# ---------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------

def register_method_version(conn: sqlite3.Connection, rm: ReleaseMethods) -> None:
    row = conn.execute("SELECT config_hash FROM release_method_versions WHERE version = ?",
                       (rm.version,)).fetchone()
    with conn:
        if row is None:
            conn.execute("INSERT INTO release_method_versions (version, config_hash, registered_at)"
                         " VALUES (?, ?, ?)", (rm.version, rm.config_hash, utc_now()))
        elif row[0] != rm.config_hash:
            raise MethodVersionError(
                f"config/release_methods.yaml changed but version {rm.version!r} was not bumped. "
                "Give the changed file a new version; existing computed values stay under "
                f"{rm.version!r}.")
        set_active(conn, "release_methods", rm.version)


def set_active(conn: sqlite3.Connection, kind: str, version: str) -> None:
    conn.execute("""INSERT INTO active_versions (kind, version, set_at) VALUES (?, ?, ?)
                    ON CONFLICT (kind) DO UPDATE SET version = excluded.version,
                                                     set_at = excluded.set_at
                    WHERE active_versions.version != excluded.version""",
                 (kind, version, utc_now()))


# ---------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------

class _Precision:
    """Input precision inferred ONLY from values public on the as-of date, so a
    computed first print never depends on data published after the release."""

    def __init__(self, conn):
        self.conn, self.cache = conn, {}

    def get(self, indicator_id: str, as_of: str) -> tuple[int, str]:
        key = (indicator_id, as_of)
        if key not in self.cache:
            vals = [r[0] for r in self.conn.execute(f"""
                SELECT value FROM observations
                WHERE indicator_id = ? AND value IS NOT NULL AND vintage_date <= ?
                  AND vintage_basis IN {pit.POINT_IN_TIME_BASES!r}""", (indicator_id, as_of))]
            self.cache[key] = infer_precision(vals)
        return self.cache[key]


def describe(method: dict) -> str:
    lag = method["lag_months"]
    base = "" if method["formula"] == "level" else f", lag {lag} month{'s' if lag != 1 else ''}"
    return (f"{method['formula']} of {method['input']}{base}; inputs as of release date "
            "(America/New_York) from first-release vintages; rounded half away from zero")


def compute_first_print(conn: sqlite3.Connection, fig: dict, method: dict,
                        precision: _Precision, now_utc: str) -> Computed:
    period = fig["reference_period"]
    if not MONTH_RE.match(period):
        raise NotComputable(f"reference period {period!r} is not monthly (YYYY-MM)")
    if fig["release_time_utc"] > now_utc:
        raise NotComputable(f"awaiting release (scheduled {fig['release_time_utc']})")

    release_date = pit.release_date_local(fig["release_time_utc"])
    inp, target = method["input"], f"{period}-01"

    first = pit.first_vintage(conn, inp, target)
    if first is None:
        raise NotComputable(f"no vintage data for {inp} {target}; run ingest after the release")
    if first.vintage_basis == "source_realtime_clipped":
        raise NotComputable(f"first stored vintage of {inp} {target} ({first.vintage_date}) is the "
                            "start of the vintage window; the true first print is unknown")
    if first.vintage_basis != "source_realtime":
        raise NotComputable(f"vintage basis {first.vintage_basis!r} cannot establish a first print")
    if first.vintage_date < release_date:
        raise NotComputable(f"{inp} {target} was already published on {first.vintage_date}, before "
                            f"this release ({release_date}); check reference_period and release time")
    if first.vintage_date > release_date:
        raise NotComputable(f"first vintage of {inp} {target} is {first.vintage_date}, after the "
                            f"release date {release_date}; check the release calendar")
    if first.value_status != "ok":
        raise NotComputable(f"{inp} {target} first print is missing in the source")

    used = [{"role": "current", **first.as_dict()}]
    base_value = None
    if method["formula"] != "level":
        base_date = shift_month(target, -method["lag_months"])
        base = pit.value_as_of(conn, inp, base_date, release_date)
        if base is None or base.value_status != "ok":
            raise NotComputable(f"no {inp} value for {base_date} as of {release_date}")
        base_value = base.value
        used.append({"role": "base", **base.as_dict()})

    k, note = precision.get(inp, release_date)
    try:
        res = apply(method["formula"], first.value, base_value, half_unit(k))
    except TransformError as exc:
        raise NotComputable(str(exc)) from None

    return Computed(value=res.value, low=res.low, high=res.high,
                    rounded=float(round_published(res.value, fig["published_decimals"])),
                    as_of_date=release_date,
                    inputs={"observations": used, "input_precision_decimals": k,
                            "precision_note": note, "formula": method["formula"],
                            "lag_months": method["lag_months"], "input_indicator": inp},
                    method=describe(method))


def _figures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT rv.value_id, rv.indicator_id, rv.measure, rv.published_decimals,
               re.reference_period, re.release_time_utc
        FROM release_values rv JOIN release_events re ON re.event_id = rv.event_id
        ORDER BY re.release_time_utc, rv.value_id
    """).fetchall()
    cols = ["value_id", "indicator_id", "measure", "published_decimals",
            "reference_period", "release_time_utc"]
    return [dict(zip(cols, r)) for r in rows]


def _log(conn, run_id, value_id, version, outcome, reason=None):
    conn.execute("""INSERT INTO release_computation_log
                    (run_id, value_id, method_version, outcome, reason, logged_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (run_id, value_id, version, outcome, reason, utc_now()))


def compute_first_prints(conn: sqlite3.Connection, rm: ReleaseMethods, *, run_id: int,
                         now: datetime | None = None) -> ComputeReport:
    now_utc = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report, precision = ComputeReport(), _Precision(conn)

    with conn:
        for fig in _figures(conn):
            vid = fig["value_id"]
            method = rm.methods.get((fig["indicator_id"], fig["measure"]))
            if method is None:
                _log(conn, run_id, vid, rm.version, "no_method",
                     f"no method for {fig['indicator_id']}/{fig['measure']} in release_methods.yaml")
                report.no_method += 1
                continue

            stored = conn.execute("""
                SELECT candidate_id, value, value_unrounded, bound_low, bound_high
                FROM release_value_candidates
                WHERE value_id = ? AND origin = 'computed' AND method_version = ?
            """, (vid, rm.version)).fetchone()

            try:
                comp = compute_first_print(conn, fig, method, precision, now_utc)
            except NotComputable as exc:
                if stored is not None:
                    msg = f"value_id {vid}: stored computed value can no longer be reproduced: {exc}"
                    _log(conn, run_id, vid, rm.version, "conflict", msg)
                    report.conflicts.append(msg)
                else:
                    _log(conn, run_id, vid, rm.version, "not_computable", str(exc))
                    report.not_computable += 1
                continue

            if stored is not None:
                if (_same(stored[2], comp.value) and stored[1] == comp.rounded
                        and _same(stored[3], comp.low) and _same(stored[4], comp.high)):
                    _log(conn, run_id, vid, rm.version, "unchanged")
                    report.unchanged += 1
                else:
                    msg = (f"value_id {vid}: recomputed {comp.value!r} "
                           f"[{comp.low!r}, {comp.high!r}] differs from stored {stored[2]!r} "
                           f"[{stored[3]!r}, {stored[4]!r}] under the same method version "
                           "(kept stored)")
                    _log(conn, run_id, vid, rm.version, "conflict", msg)
                    report.conflicts.append(msg)
                continue

            conn.execute("""
                INSERT INTO release_value_candidates
                    (value_id, origin, value, value_unrounded, bound_low, bound_high, source_id,
                     method, method_version, as_of_date, inputs, run_id, recorded_at)
                VALUES (?, 'computed', ?, ?, ?, ?, 'FRED', ?, ?, ?, ?, ?, ?)
            """, (vid, comp.rounded, comp.value, comp.low, comp.high, comp.method, rm.version,
                  comp.as_of_date, json.dumps(comp.inputs, sort_keys=True), run_id, utc_now()))
            _log(conn, run_id, vid, rm.version, "computed")
            report.computed += 1
    return report


# ---------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------

def compute_revisions(conn: sqlite3.Connection, rm: ReleaseMethods, *, run_id: int,
                      report: ComputeReport | None = None) -> ComputeReport:
    report = report or ComputeReport()
    precision = _Precision(conn)
    cands = conn.execute("""
        SELECT c.value_id, c.value_unrounded, c.as_of_date, c.inputs,
               rv.indicator_id, rv.measure, rv.published_decimals
        FROM release_value_candidates c JOIN release_values rv ON rv.value_id = c.value_id
        WHERE c.origin = 'computed' AND c.method_version = ?
    """, (rm.version,)).fetchall()

    with conn:
        for vid, first_value, as_of, inputs_json, ind, measure, decimals in cands:
            method = rm.methods.get((ind, measure))
            if method is None:
                continue
            obs = {o["role"]: o for o in json.loads(inputs_json)["observations"]}
            inp = obs["current"]["indicator_id"]
            dates_needed = [o["observation_date"] for o in obs.values()]
            prev = first_value
            for d in pit.later_vintage_dates(conn, inp, dates_needed, as_of):
                k, _ = precision.get(inp, d)
                cur = pit.value_as_of(conn, inp, obs["current"]["observation_date"], d)
                base = (pit.value_as_of(conn, inp, obs["base"]["observation_date"], d)
                        if "base" in obs else None)
                if cur is None or cur.value_status != "ok" or (
                        "base" in obs and (base is None or base.value_status != "ok")):
                    continue
                try:
                    res = apply(method["formula"], cur.value,
                                base.value if base else None, half_unit(k))
                except TransformError:
                    continue
                if _same(res.value, prev):
                    continue
                used = [{"role": "current", **cur.as_dict()}]
                if base:
                    used.append({"role": "base", **base.as_dict()})
                rounded = float(round_published(res.value, decimals))
                stored = conn.execute("""SELECT value_unrounded FROM release_value_revisions
                                         WHERE value_id = ? AND as_of_date = ? AND method_version = ?""",
                                      (vid, d, rm.version)).fetchone()
                if stored is None:
                    conn.execute("""
                        INSERT INTO release_value_revisions
                            (value_id, as_of_date, method_version, value_unrounded, value_rounded,
                             inputs, run_id, computed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (vid, d, rm.version, res.value, rounded,
                          json.dumps({"observations": used}, sort_keys=True), run_id, utc_now()))
                    report.revisions_added += 1
                elif not _same(stored[0], res.value):
                    report.conflicts.append(f"value_id {vid}: revision as of {d} recomputed as "
                                            f"{res.value!r}, stored {stored[0]!r} (kept stored)")
                prev = res.value
    return report
