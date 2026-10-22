"""
Loaders for manually maintained CSV files in data/manual/.

  ism.csv               ISM PMI actuals (gitignored; never committed)
  forecasts.csv         consensus forecasts for scheduled releases
  actuals.csv           published release figures, entered by hand (Phase 5)
  overrides.csv         documented validation overrides (Phase 5; loaded
                        AFTER validation, see src/pipeline.py)
  fomc_meetings.csv     FOMC meeting calendar and decisions
  market_bars/*.csv     daily bars from non-primary vendors (e.g. gold)

Rules shared by every loader
  1. The file is snapshotted to data/raw/manual/ before parsing.
  2. Every row is validated first. One bad row rejects the whole file:
     nothing is inserted, and every error is reported with its line number.
  3. Existing rows are never overwritten. A re-load of identical data is a
     no-op; a different value for an existing key is reported as a conflict.
  4. A source used for forecasts or market bars must be registered in
     config/sources.yaml with terms_checked_at set.
  5. Forecasts: one consensus provider across the whole dataset (Phase 3
     decision). A second provider is rejected unless explicitly allowed.

Templates with the exact headers live in data/manual/templates/.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.data_collection.raw_store import RawStore
from src.processing.point_in_time import release_date_local
from src.processing.transforms import round_published
from src.storage.db import ObsRow, insert_observations, log_fetch

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PERIOD_RE = re.compile(r"^\d{4}-(\d{2}|Q[1-4])(-\d{2})?$")

ISM_COLUMNS = ["indicator_id", "observation_month", "release_date", "value"]
FORECAST_COLUMNS = ["release_name", "reference_period", "release_time_utc", "release_source_id",
                    "indicator_id", "measure", "unit", "published_decimals",
                    "forecast_value", "forecast_source", "forecast_as_of", "notes"]
FOMC_COLUMNS = ["meeting_start_date", "meeting_end_date", "meeting_type", "has_sep", "decision",
                "target_lower", "target_upper", "change_bps", "dissents", "statement_url"]
BAR_COLUMNS = ["asset_id", "bar_interval", "bar_time", "instrument_symbol",
               "open", "high", "low", "close", "volume", "source_id"]
ACTUAL_COLUMNS = ["release_name", "reference_period", "release_time_utc", "release_source_id",
                  "indicator_id", "measure", "unit", "published_decimals",
                  "actual_value", "actual_source", "notes"]
OVERRIDE_COLUMNS = ["override_id", "action", "release_name", "reference_period", "indicator_id",
                    "measure", "reviewed_computed", "reviewed_manual", "resolution",
                    "override_value", "reason_code", "justification", "evidence_source",
                    "evidence_reference", "decided_by", "decided_at", "approved_by",
                    "approved_at", "target_override_id"]
MEASURES = {"level", "mom_pct", "yoy_pct", "change", "rate_pct"}
RESOLUTIONS = {"use_computed", "use_manual", "use_value"}
REASON_CODES = {"data_entry_error", "computation_method_limitation", "source_correction",
                "verified_against_primary_source", "other"}
OVERRIDE_ACTIONS = {"resolve", "revoke", "approve"}
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
# Minimum lengths are typo guards ("-", "ok"), not judgments of quality.
MIN_EVIDENCE_CHARS = 8
MIN_JUSTIFICATION_CHARS = 10
# Supporting evidence must come from a primary source: the agency or the
# original publisher, never a third-party aggregator.
EVIDENCE_TIERS = {"official", "publisher"}


@dataclass
class LoadReport:
    file: str
    status: str = "ok"            # ok | rejected | error
    rows: int = 0
    inserted: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


class _Rejected(Exception):
    pass


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _is_date(s: str) -> bool:
    """A real calendar date in YYYY-MM-DD (rejects 2026-02-30, 2026-9-1)."""
    if not DATE_RE.match(s or ""):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _is_month(s: str) -> bool:
    return bool(MONTH_RE.match(s or "")) and 1 <= int(s[5:7]) <= 12


def _is_ts(s: str) -> bool:
    if not TS_RE.match(s or ""):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def _is_period(s: str) -> bool:
    """YYYY-MM (month 01-12), YYYY-Qn (n 1-4) or a real date YYYY-MM-DD."""
    return _is_month(s) or bool(re.match(r"^\d{4}-Q[1-4]$", s or "")) or _is_date(s)


def _mark_fetch_failed(conn, fetch_id: int, message: str) -> None:
    """The file was logged 'ok' before its rows were written; if writing
    failed (and was rolled back), the log must say so."""
    with conn:
        conn.execute("UPDATE fetch_log SET status = 'error', error_message = ? "
                     "WHERE fetch_id = ?", (message[:2000], fetch_id))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path, columns: list[str]) -> tuple[bytes, list[tuple[int, dict]]]:
    body = path.read_bytes()
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    header = [h.strip() for h in (reader.fieldnames or [])]
    if header != columns:
        raise _Rejected(f"header must be exactly {columns}, got {header}")
    rows = []
    for line_no, raw in enumerate(reader, start=2):
        if None in raw:   # DictReader puts surplus fields under the key None
            raise _Rejected(f"line {line_no}: more fields than header columns "
                            "(a value containing a comma must be in double quotes)")
        row = {k.strip(): (v.strip() if isinstance(v, str) else "") for k, v in raw.items()}
        if all(not v for v in row.values()):
            continue  # blank line
        rows.append((line_no, row))
    return body, rows


def _num(v: str, name: str, errs: list, line: int, *, required=True, integer=False):
    if v in ("", None):
        if required:
            errs.append(f"line {line}: {name} is required")
        return None
    try:
        return int(v) if integer else float(v)
    except ValueError:
        errs.append(f"line {line}: {name}={v!r} is not a number")
        return None


def _verified_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()


def _require_verified(conn, source_id: str, errs: list, line: int | str) -> None:
    src = _verified_source(conn, source_id)
    if src is None:
        errs.append(f"line {line}: source {source_id!r} is not registered in config/sources.yaml")
    elif not src["terms_checked_at"]:
        errs.append(f"line {line}: source {source_id!r} has no terms_checked_at; "
                    "verify coverage and permitted usage before loading its data")


def _snapshot_and_log(conn, store, *, run_id, source_id, path: Path, body: bytes,
                      kind: str, rows: int, status: str, error: str | None = None) -> int:
    retrieved = _now()
    raw_path, digest = store.save(source="manual", target=path.stem, kind=kind, body=body,
                                  meta={"original_file": path.name}, retrieved_at=retrieved,
                                  extension="csv")
    return log_fetch(conn, run_id=run_id, source_id=source_id, target=f"manual:{path.name}",
                     retrieved_at=_iso(retrieved), status=status, raw_path=raw_path,
                     raw_sha256=digest, rows_received=rows, error_message=error)


def _fail(conn, store, report: LoadReport, *, run_id, source_id, path, body, kind):
    """Reject the whole file. The file is still snapshotted (full retention);
    it is logged in fetch_log only when its source is registered."""
    report.status = "rejected"
    if body is None:
        return report
    if source_id and _verified_source(conn, source_id):
        _snapshot_and_log(conn, store, run_id=run_id, source_id=source_id, path=path,
                          body=body, kind=f"{kind}_rejected", rows=report.rows,
                          status="error", error="; ".join(report.errors[:20]))
    else:
        store.save(source="manual", target=path.stem, kind=f"{kind}_rejected", body=body,
                   meta={"original_file": path.name, "errors": report.errors[:20]},
                   extension="csv")
    return report


def _check_release_fields(r: dict, line: int, errs: list, indicators: set, sources: set):
    """Validation shared by every file that defines a release figure."""
    if not r["release_name"]:
        errs.append(f"line {line}: release_name is required")
    if not _is_period(r["reference_period"]):
        errs.append(f"line {line}: reference_period must be YYYY-MM, YYYY-Qn or YYYY-MM-DD "
                    "(a real month or date)")
    if not _is_ts(r["release_time_utc"]):
        errs.append(f"line {line}: release_time_utc must be YYYY-MM-DDTHH:MM:SSZ (UTC)")
    if r["release_source_id"] not in sources:
        errs.append(f"line {line}: release_source_id {r['release_source_id']!r} not registered")
    if r["indicator_id"] not in indicators:
        errs.append(f"line {line}: unknown indicator_id {r['indicator_id']!r}")
    if r["measure"] not in MEASURES:
        errs.append(f"line {line}: measure must be one of {sorted(MEASURES)}")
    if not r["unit"]:
        errs.append(f"line {line}: unit is required")
    decimals = _num(r["published_decimals"], "published_decimals", errs, line, integer=True)
    if decimals is not None and not 0 <= decimals <= 4:
        errs.append(f"line {line}: published_decimals must be 0-4")
        decimals = None
    return decimals


def _text_decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def _get_or_create_release_value(conn, r: dict, decimals: int, line: int,
                                 report: "LoadReport") -> int | None:
    """Find or create the event and figure. Returns None on a definition conflict."""
    ev = conn.execute("""SELECT event_id, release_time_utc FROM release_events
                         WHERE release_name = ? AND reference_period = ?""",
                      (r["release_name"], r["reference_period"])).fetchone()
    if ev is None:
        event_id = conn.execute("""
            INSERT INTO release_events (release_name, source_id, reference_period,
                                        release_time_utc, status)
            VALUES (?, ?, ?, ?, 'scheduled')
        """, (r["release_name"], r["release_source_id"], r["reference_period"],
              r["release_time_utc"])).lastrowid
    else:
        event_id = ev["event_id"]
        if ev["release_time_utc"] != r["release_time_utc"]:
            report.conflicts.append(
                f"line {line}: {r['release_name']} {r['reference_period']} stored time "
                f"{ev['release_time_utc']} != file {r['release_time_utc']} "
                "(kept stored; row skipped)")
            return None

    rv = conn.execute("""SELECT value_id, unit, published_decimals FROM release_values
                         WHERE event_id = ? AND indicator_id = ? AND measure = ?""",
                      (event_id, r["indicator_id"], r["measure"])).fetchone()
    if rv is None:
        return conn.execute("""
            INSERT INTO release_values (event_id, indicator_id, measure, unit, published_decimals)
            VALUES (?, ?, ?, ?, ?)
        """, (event_id, r["indicator_id"], r["measure"], r["unit"], decimals)).lastrowid
    if (rv["unit"], rv["published_decimals"]) != (r["unit"], decimals):
        report.conflicts.append(f"line {line}: unit/decimals differ from stored release value "
                                f"({rv['unit']!r}, {rv['published_decimals']}) (kept stored)")
        return None
    return rv["value_id"]


# ---------------------------------------------------------------------
# ISM
# ---------------------------------------------------------------------

def load_ism(conn: sqlite3.Connection, store: RawStore, path: Path, *, run_id: int) -> LoadReport:
    report = LoadReport(path.name)
    try:
        body, rows = _read(path, ISM_COLUMNS)
    except _Rejected as exc:
        report.errors.append(str(exc))
        return _fail(conn, store, report, run_id=run_id, source_id="ISM", path=path,
                     body=path.read_bytes(), kind="ism")
    report.rows = len(rows)

    ism_ids = {r["indicator_id"] for r in conn.execute(
        "SELECT indicator_id FROM indicators WHERE source_id = 'ISM'")}
    parsed, errs, seen = [], report.errors, set()
    for line, r in rows:
        if r["indicator_id"] not in ism_ids:
            errs.append(f"line {line}: indicator_id {r['indicator_id']!r} is not an ISM indicator")
        if not _is_month(r["observation_month"]):
            errs.append(f"line {line}: observation_month must be YYYY-MM")
        if not _is_date(r["release_date"]):
            errs.append(f"line {line}: release_date must be a real date YYYY-MM-DD")
        value = _num(r["value"], "value", errs, line)
        obs_date = f"{r['observation_month']}-01"
        if _is_date(r["release_date"]) and r["release_date"] < obs_date:
            errs.append(f"line {line}: release_date precedes the observation month")
        key = (r["indicator_id"], obs_date, r["release_date"])
        if key in seen:
            errs.append(f"line {line}: duplicate row for {key}")
        seen.add(key)
        parsed.append((r["indicator_id"], ObsRow(obs_date, r["release_date"],
                                                  "release_date", value, "ok")))

    if errs:
        return _fail(conn, store, report, run_id=run_id, source_id="ISM", path=path,
                     body=body, kind="ism")

    fetch_id = _snapshot_and_log(conn, store, run_id=run_id, source_id="ISM", path=path,
                                 body=body, kind="ism", rows=len(rows), status="ok")
    by_ind: dict[str, list[ObsRow]] = {}
    for ind, row in parsed:
        by_ind.setdefault(ind, []).append(row)
    try:
        with conn:      # all indicators in one transaction
            for ind, ind_rows in by_ind.items():
                res = insert_observations(conn, ind, ind_rows, fetch_id, commit=False)
                report.inserted += res.inserted
                report.unchanged += res.unchanged
                report.conflicts.extend(res.conflicts)
    except sqlite3.Error as exc:
        _mark_fetch_failed(conn, fetch_id, f"insert failed, nothing written: {exc}")
        report.status, report.inserted, report.unchanged = "rejected", 0, 0
        report.errors.append(f"database rule: {exc} (nothing written)")
    return report


# ---------------------------------------------------------------------
# Consensus forecasts
# ---------------------------------------------------------------------

def load_forecasts(conn: sqlite3.Connection, store: RawStore, path: Path, *, run_id: int,
                   allow_multiple_sources: bool = False) -> LoadReport:
    report = LoadReport(path.name)
    try:
        body, rows = _read(path, FORECAST_COLUMNS)
    except _Rejected as exc:
        report.errors.append(str(exc))
        return _fail(conn, store, report, run_id=run_id, source_id=None, path=path,
                     body=path.read_bytes(), kind="forecasts")
    report.rows = len(rows)
    errs = report.errors

    file_sources = {r["forecast_source"] for _, r in rows if r["forecast_source"]}
    if len(file_sources) > 1:
        errs.append(f"one forecast_source per file required, got {sorted(file_sources)}")
    existing_sources = {r[0] for r in conn.execute("SELECT DISTINCT forecast_source FROM forecasts")}
    if not allow_multiple_sources and len(file_sources | existing_sources) > 1:
        errs.append(f"more than one consensus provider: file={sorted(file_sources)} "
                    f"database={sorted(existing_sources)}. One provider is required "
                    "across the dataset (pass allow_multiple_sources=True for a new variation).")
    for s in sorted(file_sources):
        _require_verified(conn, s, errs, "-")

    indicators = {r["indicator_id"] for r in conn.execute("SELECT indicator_id FROM indicators")}
    sources = {r["source_id"] for r in conn.execute("SELECT source_id FROM sources")}
    parsed, seen = [], set()
    for line, r in rows:
        decimals = _check_release_fields(r, line, errs, indicators, sources)
        if not r["forecast_source"]:
            errs.append(f"line {line}: forecast_source is required")
        asof = r["forecast_as_of"]
        if asof and not (_is_ts(asof) or _is_date(asof)):
            errs.append(f"line {line}: forecast_as_of must be a date or UTC timestamp")
        elif asof and _is_ts(r["release_time_utc"]):
            # a consensus snapshot taken after the release could contain the actual
            late = (asof >= r["release_time_utc"] if _is_ts(asof)
                    else asof > release_date_local(r["release_time_utc"]))
            if late:
                errs.append(f"line {line}: forecast_as_of {asof} is not before the release "
                            f"({r['release_time_utc']}); a forecast must predate the release")
        ftext = r["forecast_value"]
        if ftext and not NUMBER_RE.match(ftext):
            errs.append(f"line {line}: forecast_value {ftext!r} must be a plain number "
                        "(no exponents, units, commas, % signs or inf/nan)")
        elif ftext and decimals is not None and _text_decimals(ftext) > decimals:
            errs.append(f"line {line}: forecast_value {ftext} has more decimals than the "
                        f"{decimals} published; surprises are measured at published precision")
        fval = _num(ftext, "forecast_value", errs, line)
        key = (r["release_name"], r["reference_period"], r["indicator_id"], r["measure"],
               r["forecast_source"])
        if key in seen:
            errs.append(f"line {line}: duplicate forecast {key}")
        seen.add(key)
        parsed.append((line, r, decimals, fval))

    source_for_log = next(iter(file_sources)) if len(file_sources) == 1 else None
    if errs:
        return _fail(conn, store, report, run_id=run_id, source_id=source_for_log,
                     path=path, body=body, kind="forecasts")

    fetch_id = _snapshot_and_log(conn, store, run_id=run_id, source_id=source_for_log,
                                 path=path, body=body, kind="forecasts", rows=len(rows),
                                 status="ok")
    collected = _iso(_now())
    try:
        _insert_forecasts(conn, parsed, report, fetch_id, collected)
    except sqlite3.Error as exc:
        _mark_fetch_failed(conn, fetch_id, f"insert failed, nothing written: {exc}")
        report.status, report.inserted, report.unchanged = "rejected", 0, 0
        report.errors.append(f"database rule: {exc} (nothing written)")
    return report


def _insert_forecasts(conn, parsed, report, fetch_id, collected):
    with conn:
        for line, r, decimals, fval in parsed:
            value_id = _get_or_create_release_value(conn, r, decimals, line, report)
            if value_id is None:
                continue

            fc = conn.execute("""SELECT forecast_value FROM forecasts
                                 WHERE value_id = ? AND forecast_source = ?""",
                              (value_id, r["forecast_source"])).fetchone()
            if fc is not None:
                if fc["forecast_value"] == fval:
                    report.unchanged += 1
                else:
                    report.conflicts.append(
                        f"line {line}: forecast stored {fc['forecast_value']} != file {fval} "
                        "(kept stored)")
                continue
            conn.execute("""
                INSERT INTO forecasts (value_id, forecast_source, forecast_value, forecast_as_of,
                                       collected_at, collection_method, fetch_id, notes)
                VALUES (?, ?, ?, ?, ?, 'manual_entry', ?, ?)
            """, (value_id, r["forecast_source"], fval, r["forecast_as_of"] or None,
                  collected, fetch_id, r["notes"] or None))
            report.inserted += 1
    return report


# ---------------------------------------------------------------------
# FOMC meetings
# ---------------------------------------------------------------------

def load_fomc(conn: sqlite3.Connection, store: RawStore, path: Path, *, run_id: int) -> LoadReport:
    report = LoadReport(path.name)
    try:
        body, rows = _read(path, FOMC_COLUMNS)
    except _Rejected as exc:
        report.errors.append(str(exc))
        return _fail(conn, store, report, run_id=run_id, source_id="FED_BOARD", path=path,
                     body=path.read_bytes(), kind="fomc")
    report.rows = len(rows)
    errs, parsed = report.errors, []

    for line, r in rows:
        for col in ("meeting_start_date", "meeting_end_date"):
            if not _is_date(r[col]):
                errs.append(f"line {line}: {col} must be YYYY-MM-DD")
        if r["meeting_end_date"] < r["meeting_start_date"]:
            errs.append(f"line {line}: meeting ends before it starts")
        if r["meeting_type"] not in ("scheduled", "unscheduled"):
            errs.append(f"line {line}: meeting_type must be scheduled|unscheduled")
        if r["decision"] not in ("", "hike", "cut", "hold"):
            errs.append(f"line {line}: decision must be blank, hike, cut or hold")
        decided = bool(r["decision"])
        rec = {
            "meeting_end_date": r["meeting_end_date"],
            "meeting_start_date": r["meeting_start_date"],
            "meeting_type": r["meeting_type"],
            "has_sep": _num(r["has_sep"], "has_sep", errs, line, required=False, integer=True),
            "decision": r["decision"] or None,
            "target_lower": _num(r["target_lower"], "target_lower", errs, line, required=decided),
            "target_upper": _num(r["target_upper"], "target_upper", errs, line, required=decided),
            "change_bps": _num(r["change_bps"], "change_bps", errs, line, required=decided,
                               integer=True),
            "dissents": _num(r["dissents"], "dissents", errs, line, required=False, integer=True),
            "statement_url": r["statement_url"] or None,
        }
        if not decided and any(rec[k] is not None for k in ("target_lower", "target_upper",
                                                            "change_bps")):
            errs.append(f"line {line}: a future/undecided meeting must leave the rate fields blank")
        parsed.append((line, rec))

    if errs:
        return _fail(conn, store, report, run_id=run_id, source_id="FED_BOARD", path=path,
                     body=body, kind="fomc")

    _snapshot_and_log(conn, store, run_id=run_id, source_id="FED_BOARD", path=path,
                      body=body, kind="fomc", rows=len(rows), status="ok")
    cols = list(parsed[0][1].keys()) if parsed else []
    try:
        with conn:
            for line, rec in parsed:
                ex = conn.execute("SELECT * FROM fomc_meetings WHERE meeting_end_date = ?",
                                  (rec["meeting_end_date"],)).fetchone()
                if ex is None:
                    conn.execute(f"""INSERT INTO fomc_meetings ({', '.join(cols)}, source_id)
                                     VALUES ({', '.join('?' * len(cols))}, 'FED_BOARD')""",
                                 [rec[c] for c in cols])
                    report.inserted += 1
                elif all(ex[c] == rec[c] for c in cols):
                    report.unchanged += 1
                elif ex["decision"] is None and rec["decision"] is not None:
                    # A scheduled meeting has now happened: fill in the decision.
                    conn.execute(f"""UPDATE fomc_meetings SET {', '.join(f'{c} = ?' for c in cols)}
                                     WHERE meeting_end_date = ?""",
                                 [rec[c] for c in cols] + [rec["meeting_end_date"]])
                    report.inserted += 1
                else:
                    report.conflicts.append(
                        f"line {line}: meeting {rec['meeting_end_date']} differs from stored "
                        "record (kept stored)")
    except sqlite3.IntegrityError as exc:
        report.status = "rejected"
        report.errors.append(f"database constraint: {exc}")
    return report


# ---------------------------------------------------------------------
# Market bars (non-primary vendors)
# ---------------------------------------------------------------------

def load_market_bars(conn: sqlite3.Connection, store: RawStore, path: Path, *,
                     run_id: int) -> LoadReport:
    report = LoadReport(path.name)
    try:
        body, rows = _read(path, BAR_COLUMNS)
    except _Rejected as exc:
        report.errors.append(str(exc))
        return _fail(conn, store, report, run_id=run_id, source_id=None, path=path,
                     body=path.read_bytes(), kind="market_bars")
    report.rows = len(rows)
    errs = report.errors

    file_sources = {r["source_id"] for _, r in rows}
    if len(file_sources) != 1:
        errs.append(f"one source_id per file required, got {sorted(file_sources)}")
    for s in sorted(file_sources):
        _require_verified(conn, s, errs, "-")

    assets = {r["asset_id"]: r["indicator_id"] for r in conn.execute(
        "SELECT asset_id, indicator_id FROM assets")}
    parsed, seen = [], set()
    for line, r in rows:
        if r["asset_id"] not in assets:
            errs.append(f"line {line}: unknown asset_id {r['asset_id']!r}")
        elif assets[r["asset_id"]] is not None:
            errs.append(f"line {line}: asset {r['asset_id']} is sourced from observations "
                        "(FRED); bars for it would duplicate the series")
        if r["bar_interval"] != "1d":
            errs.append(f"line {line}: MVP accepts daily bars only (bar_interval=1d)")
        if not _is_date(r["bar_time"]):
            errs.append(f"line {line}: daily bar_time must be the session date YYYY-MM-DD")
        if not r["instrument_symbol"]:
            errs.append(f"line {line}: instrument_symbol is required (contract or 'spot')")
        o = _num(r["open"], "open", errs, line, required=False)
        h = _num(r["high"], "high", errs, line, required=False)
        lo = _num(r["low"], "low", errs, line, required=False)
        c = _num(r["close"], "close", errs, line)
        v = _num(r["volume"], "volume", errs, line, required=False)
        if h is not None and lo is not None and h < lo:
            errs.append(f"line {line}: high < low")
        if c is not None and h is not None and lo is not None and not lo <= c <= h:
            errs.append(f"line {line}: close outside high/low range")
        key = (r["asset_id"], r["bar_interval"], r["bar_time"], r["source_id"])
        if key in seen:
            errs.append(f"line {line}: duplicate bar {key}")
        seen.add(key)
        parsed.append((line, r, o, h, lo, c, v))

    source = next(iter(file_sources)) if len(file_sources) == 1 else None
    if errs:
        return _fail(conn, store, report, run_id=run_id, source_id=source, path=path,
                     body=body, kind="market_bars")

    fetch_id = _snapshot_and_log(conn, store, run_id=run_id, source_id=source, path=path,
                                 body=body, kind="market_bars", rows=len(rows), status="ok")
    try:
        _insert_bars(conn, parsed, report, fetch_id)
    except sqlite3.Error as exc:
        _mark_fetch_failed(conn, fetch_id, f"insert failed, nothing written: {exc}")
        report.status, report.inserted, report.unchanged = "rejected", 0, 0
        report.errors.append(f"database rule: {exc} (nothing written)")
    return report


def _insert_bars(conn, parsed, report, fetch_id):
    with conn:
        for line, r, o, h, lo, c, v in parsed:
            ex = conn.execute("""SELECT instrument_symbol, open, high, low, close, volume
                                 FROM market_prices WHERE asset_id = ? AND bar_interval = ?
                                 AND bar_time = ? AND source_id = ?""",
                              (r["asset_id"], r["bar_interval"], r["bar_time"],
                               r["source_id"])).fetchone()
            new = (r["instrument_symbol"], o, h, lo, c, v)
            if ex is not None:
                if tuple(ex) == new:
                    report.unchanged += 1
                else:
                    report.conflicts.append(f"line {line}: bar {r['bar_time']} differs from "
                                            "stored bar (kept stored)")
                continue
            conn.execute("""
                INSERT INTO market_prices (asset_id, bar_interval, bar_time, instrument_symbol,
                                           open, high, low, close, volume, source_id, fetch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (r["asset_id"], r["bar_interval"], r["bar_time"], r["instrument_symbol"],
                  o, h, lo, c, v, r["source_id"], fetch_id))
            report.inserted += 1
    return report


# ---------------------------------------------------------------------
# Published actuals (manual entry, Phase 5)
# ---------------------------------------------------------------------

def load_actuals(conn: sqlite3.Connection, store: RawStore, path: Path, *, run_id: int,
                 now: datetime | None = None) -> LoadReport:
    """
    Published release figures typed in from the source (e.g. the BLS news
    release). Stored as 'manual' candidates; they are NOT trusted on their
    own: validation compares them with the computed first print.

    Extra checks: the value may not have more decimals than published, and
    the release must already have happened.
    """
    now_utc = _iso(now or _now())
    report = LoadReport(path.name)
    try:
        body, rows = _read(path, ACTUAL_COLUMNS)
    except _Rejected as exc:
        report.errors.append(str(exc))
        return _fail(conn, store, report, run_id=run_id, source_id=None, path=path,
                     body=path.read_bytes(), kind="actuals")
    report.rows = len(rows)
    errs = report.errors

    file_sources = {r["actual_source"] for _, r in rows if r["actual_source"]}
    if len(file_sources) > 1:
        errs.append(f"one actual_source per file required, got {sorted(file_sources)}")
    for s in sorted(file_sources):
        src = _verified_source(conn, s)
        if src is None:
            errs.append(f"line -: source {s!r} is not registered in config/sources.yaml")
        elif src["source_tier"] == "third_party" and not src["terms_checked_at"]:
            errs.append(f"line -: source {s!r} is third-party and has no terms_checked_at")

    indicators = {r["indicator_id"] for r in conn.execute("SELECT indicator_id FROM indicators")}
    sources = {r["source_id"] for r in conn.execute("SELECT source_id FROM sources")}
    parsed, seen = [], set()
    for line, r in rows:
        decimals = _check_release_fields(r, line, errs, indicators, sources)
        if not r["actual_source"]:
            errs.append(f"line {line}: actual_source is required")
        text = r["actual_value"]
        if not NUMBER_RE.match(text or ""):
            errs.append(f"line {line}: actual_value {text!r} must be a plain number "
                        "(no units, commas or % signs)")
        elif decimals is not None and _text_decimals(text) > decimals:
            errs.append(f"line {line}: actual_value {text} has more decimals than the "
                        f"{decimals} published; enter the figure exactly as published")
        if _is_ts(r["release_time_utc"]) and r["release_time_utc"] > now_utc:
            errs.append(f"line {line}: release_time_utc {r['release_time_utc']} is in the future; "
                        "an actual cannot exist before its release")
        key = (r["release_name"], r["reference_period"], r["indicator_id"], r["measure"])
        if key in seen:
            errs.append(f"line {line}: duplicate actual {key}")
        seen.add(key)
        parsed.append((line, r, decimals))

    source = next(iter(file_sources)) if len(file_sources) == 1 else None
    if errs:
        return _fail(conn, store, report, run_id=run_id, source_id=source, path=path,
                     body=body, kind="actuals")

    fetch_id = _snapshot_and_log(conn, store, run_id=run_id, source_id=source, path=path,
                                 body=body, kind="actuals", rows=len(rows), status="ok")
    try:
        _insert_actuals(conn, parsed, report, fetch_id, run_id)
    except sqlite3.Error as exc:
        _mark_fetch_failed(conn, fetch_id, f"insert failed, nothing written: {exc}")
        report.status, report.inserted, report.unchanged = "rejected", 0, 0
        report.errors.append(f"database rule: {exc} (nothing written)")
    return report


def _insert_actuals(conn, parsed, report, fetch_id, run_id):
    recorded = _iso(_now())
    with conn:
        for line, r, decimals in parsed:
            value_id = _get_or_create_release_value(conn, r, decimals, line, report)
            if value_id is None:
                continue
            stored = conn.execute("""SELECT value_text, source_id FROM release_value_candidates
                                     WHERE value_id = ? AND origin = 'manual'""",
                                  (value_id,)).fetchone()
            if stored is not None:
                if (stored["value_text"], stored["source_id"]) == (r["actual_value"],
                                                                   r["actual_source"]):
                    report.unchanged += 1
                else:
                    report.conflicts.append(
                        f"line {line}: stored actual {stored['value_text']} "
                        f"({stored['source_id']}) != file {r['actual_value']} "
                        f"({r['actual_source']}). Kept stored; use overrides.csv to correct it.")
                continue
            conn.execute("""
                INSERT INTO release_value_candidates
                    (value_id, origin, value, value_text, source_id, fetch_id, run_id, recorded_at)
                VALUES (?, 'manual', ?, ?, ?, ?, ?, ?)
            """, (value_id, float(r["actual_value"]), r["actual_value"], r["actual_source"],
                  fetch_id, run_id, recorded))
            report.inserted += 1
    return report


# ---------------------------------------------------------------------
# Validation overrides (Phase 5)
# ---------------------------------------------------------------------

def _figure_status(conn, r: dict):
    return conn.execute("""
        SELECT * FROM v_release_status
        WHERE release_name = ? AND reference_period = ? AND indicator_id = ? AND measure = ?
    """, (r["release_name"], r["reference_period"], r["indicator_id"], r["measure"])).fetchone()


def _fmt(value, decimals) -> str:
    return "none" if value is None else str(round_published(value, decimals))


def _reviewed_matches(text: str, current, decimals: int) -> bool:
    """Blank means 'no value existed when reviewed'."""
    if not text:
        return current is None
    if current is None or not NUMBER_RE.match(text):
        return False
    return round_published(float(text), decimals) == round_published(current, decimals)


def _stored_identity(conn, override_id: str):
    return conn.execute("""
        SELECT re.release_name, re.reference_period, rv.indicator_id, rv.measure
        FROM release_overrides o
        JOIN release_validations v ON v.validation_id = o.validation_id
        JOIN release_values rv     ON rv.value_id = v.value_id
        JOIN release_events re     ON re.event_id = rv.event_id
        WHERE o.override_id = ?""", (override_id,)).fetchone()


def _check_evidence(conn, r: dict, line: int, errs: list, required: bool) -> None:
    """Sufficient evidence = a registered PRIMARY source + a specific reference."""
    src_id, ref = (r["evidence_source"] or "").strip(), (r["evidence_reference"] or "").strip()
    if not required and not src_id and not ref:
        return
    if not src_id:
        errs.append(f"line {line}: evidence_source is required (e.g. BLS, BEA)")
    else:
        src = _verified_source(conn, src_id)
        if src is None:
            errs.append(f"line {line}: evidence_source {src_id!r} is not registered in "
                        "config/sources.yaml")
        elif src["source_tier"] not in EVIDENCE_TIERS:
            errs.append(f"line {line}: evidence_source {src_id!r} is {src['source_tier']}; "
                        "evidence must come from a primary source (official or publisher)")
    if len(ref) < MIN_EVIDENCE_CHARS or not re.search(r"[A-Za-z0-9]", ref):
        errs.append(f"line {line}: evidence_reference must identify the document, table or URL "
                    f"checked (at least {MIN_EVIDENCE_CHARS} characters)")


def _same(a, b) -> bool:
    return (a or "") == (b or "")


def _same_decision(conn, old, apr, r) -> bool:
    pairs = [("action", "action"), ("resolution", "resolution"),
             ("override_value", "override_value_text"), ("reason_code", "reason_code"),
             ("justification", "justification"), ("evidence_source", "evidence_source"),
             ("evidence_reference", "evidence_reference"), ("decided_by", "decided_by"),
             ("decided_at", "decided_at"), ("target_override_id", "revokes_override_id"),
             ("reviewed_computed", "reviewed_computed_text"),
             ("reviewed_manual", "reviewed_manual_text")]
    if any(not _same(r[c], old[col]) for c, col in pairs):
        return False
    if apr is None or not (_same(r["approved_by"], apr["approved_by"])
                           and _same(r["approved_at"], apr["approved_at"])):
        return False
    if old["action"] == "resolve":
        ident = _stored_identity(conn, old["override_id"])
        return ident is not None and tuple(ident) == (
            r["release_name"], r["reference_period"], r["indicator_id"], r["measure"])
    return True


def _same_approval(apr, r) -> bool:
    return (r["action"] == "approve"
            and all(_same(r[c], apr[col]) for c, col in (
                ("target_override_id", "override_id"), ("approved_by", "approved_by"),
                ("approved_at", "approved_at"), ("justification", "note"),
                ("evidence_source", "evidence_source"),
                ("evidence_reference", "evidence_reference"))))


def load_overrides(conn: sqlite3.Connection, store: RawStore, path: Path, *, run_id: int,
                   now: datetime | None = None) -> LoadReport:
    """
    The review log: documented human decisions on release figures, and their
    approvals. Must run AFTER validation. Every row is one log entry with a
    unique override_id.

    resolve  approves a value for a figure: use_computed | use_manual | use_value.
             Needs a documented reason (reason_code + justification), supporting
             evidence (evidence_source: a registered PRIMARY source such as BLS;
             evidence_reference: the document, table or URL checked), the values
             reviewed (reviewed_computed / reviewed_manual, blank = absent),
             decided_by/decided_at, and its first approval (approved_by +
             approved_at as a UTC timestamp). The decider may approve their own
             decision (MVP policy); the figure is then labelled self_approved.
    approve  adds a further approval to an existing resolve (target_override_id),
             e.g. an independent reviewer. Needs justification and evidence.
    revoke   withdraws a resolve (target_override_id); needs a reason and approval.

    Rules: append-only (a recorded entry can never be edited or re-pointed);
    a resolve whose reviewed values are not the figure's current values is
    refused; overrides are refused where none is needed (already verified)
    and where a stored value is unreproducible; nothing may predate the release
    or be dated in the future. Original candidate values are never touched.
    """
    now_dt = now or _now()
    today, now_ts = now_dt.strftime("%Y-%m-%d"), _iso(now_dt)
    report = LoadReport(path.name)
    try:
        body, rows = _read(path, OVERRIDE_COLUMNS)
    except _Rejected as exc:
        report.errors.append(str(exc))
        return _fail(conn, store, report, run_id=run_id, source_id=None, path=path,
                     body=path.read_bytes(), kind="overrides")
    report.rows = len(rows)
    errs = report.errors

    stored_ovr = {r["override_id"]: r for r in conn.execute("SELECT * FROM release_overrides")}
    stored_apr = {r["approval_id"]: r for r in conn.execute("SELECT * FROM override_approvals")}
    active = {r["validation_id"]: r["override_id"] for r in conn.execute("""
        SELECT o.validation_id, o.override_id FROM release_overrides o
        WHERE o.action = 'resolve' AND NOT EXISTS (
            SELECT 1 FROM release_overrides x
            WHERE x.action = 'revoke' AND x.revokes_override_id = o.override_id)""")}
    resolves = {oid: {"validation_id": r["validation_id"], "decided_at": r["decided_at"]}
                for oid, r in stored_ovr.items() if r["action"] == "resolve"}
    revoked = {r["revokes_override_id"] for r in stored_ovr.values() if r["action"] == "revoke"}
    approvers: dict[str, set] = {}
    for a in stored_apr.values():
        approvers.setdefault(a["override_id"], set()).add(a["approved_by"].strip().lower())

    seen, plan = set(), []
    for line, r in rows:
        oid, action = r["override_id"], r["action"]
        if not oid:
            errs.append(f"line {line}: override_id is required")
            continue
        if oid in seen:
            errs.append(f"line {line}: duplicate override_id {oid}")
        seen.add(oid)
        if action not in OVERRIDE_ACTIONS:
            errs.append(f"line {line}: action must be one of {sorted(OVERRIDE_ACTIONS)}")
            continue

        # ---- recorded entries are compared, never re-bound or edited ----
        if oid in stored_ovr:
            plan.append(("existing_decision", line, r, None))
            continue
        if oid in stored_apr:
            plan.append(("existing_approval", line, r, None))
            continue

        # ---- rules common to every new entry ----
        if len((r["justification"] or "").strip()) < MIN_JUSTIFICATION_CHARS:
            errs.append(f"line {line}: justification is required: document the reason "
                        f"(at least {MIN_JUSTIFICATION_CHARS} characters)")
        if not r["approved_by"]:
            errs.append(f"line {line}: approved_by is required")
        if not _is_ts(r["approved_at"]):
            errs.append(f"line {line}: approved_at must be a UTC timestamp "
                        "YYYY-MM-DDTHH:MM:SSZ")
        elif r["approved_at"] > now_ts:
            errs.append(f"line {line}: approved_at {r['approved_at']} is in the future")
        _check_evidence(conn, r, line, errs, required=(action != "revoke"))

        if action == "approve":
            target = r["target_override_id"]
            for col in ("resolution", "override_value", "reviewed_computed", "reviewed_manual",
                        "decided_by", "decided_at"):
                if r[col]:
                    errs.append(f"line {line}: approve rows must leave {col} blank")
            if target not in resolves:
                errs.append(f"line {line}: {target or '(blank)'} is not a known resolve override")
                continue
            if target in revoked:
                errs.append(f"line {line}: {target} has been revoked; approve a new decision")
            who = (r["approved_by"] or "").strip().lower()
            if who in approvers.get(target, set()):
                errs.append(f"line {line}: {r['approved_by']} has already approved {target}")
            approvers.setdefault(target, set()).add(who)
            if _is_ts(r["approved_at"]) and r["approved_at"][:10] < resolves[target]["decided_at"]:
                errs.append(f"line {line}: approved_at is before the decision it approves")
            plan.append(("approve", line, r, None))
            continue

        # ---- decisions (resolve / revoke) ----
        if not r["decided_by"]:
            errs.append(f"line {line}: decided_by is required")
        if not _is_date(r["decided_at"]):
            errs.append(f"line {line}: decided_at must be a real date YYYY-MM-DD")
        elif r["decided_at"] > today:
            errs.append(f"line {line}: decided_at {r['decided_at']} is in the future")
        elif _is_ts(r["approved_at"]) and r["approved_at"][:10] < r["decided_at"]:
            errs.append(f"line {line}: approved_at is before decided_at")

        if action == "revoke":
            target = r["target_override_id"]
            if target not in resolves:
                errs.append(f"line {line}: {target or '(blank)'} is not a known resolve override")
            elif target in revoked:
                errs.append(f"line {line}: {target} is already revoked")
            else:
                revoked.add(target)
                if active.get(resolves[target]["validation_id"]) == target:
                    del active[resolves[target]["validation_id"]]
            for col in ("resolution", "override_value", "reviewed_computed", "reviewed_manual"):
                if r[col]:
                    errs.append(f"line {line}: revoke must leave {col} blank")
            if r["reason_code"] and r["reason_code"] not in REASON_CODES:
                errs.append(f"line {line}: reason_code must be one of {sorted(REASON_CODES)}")
            plan.append(("revoke", line, r, None))
            continue

        # ---- resolve ----
        if r["target_override_id"]:
            errs.append(f"line {line}: resolve must leave target_override_id blank")
        if r["reason_code"] not in REASON_CODES:
            errs.append(f"line {line}: reason_code must be one of {sorted(REASON_CODES)}")
        if r["resolution"] not in RESOLUTIONS:
            errs.append(f"line {line}: resolution must be one of {sorted(RESOLUTIONS)}")
            continue
        st = _figure_status(conn, r)
        if st is None:
            errs.append(f"line {line}: no release figure {r['release_name']} "
                        f"{r['reference_period']} {r['indicator_id']}/{r['measure']}")
            continue
        d = st["published_decimals"]
        released = release_date_local(st["release_time_utc"])
        if _is_date(r["decided_at"]) and r["decided_at"] < released:
            errs.append(f"line {line}: decided_at {r['decided_at']} is before the release "
                        f"({released}); a decision cannot predate the values it reviews")
        if st["final_status"] == "unreproducible":
            errs.append(f"line {line}: a stored value cannot be reproduced; investigate before "
                        "approving anything")
            continue
        if st["validation_id"] is None:
            errs.append(f"line {line}: nothing to override: the figure has no values yet")
            continue
        if st["validation_status"] in ("match", "within_rounding") \
                or st["verification"] in ("verified_crosschecked", "verified_computed"):
            errs.append(f"line {line}: no override needed: the figure is already "
                        f"{st['verification']} ({st['validation_status']})")
            continue
        if not (_reviewed_matches(r["reviewed_computed"], st["computed_value"], d)
                and _reviewed_matches(r["reviewed_manual"], st["manual_value"], d)):
            errs.append(
                f"line {line}: values changed since review: reviewed computed="
                f"{r['reviewed_computed'] or 'none'} manual={r['reviewed_manual'] or 'none'}, "
                f"current computed={_fmt(st['computed_value'], d)} "
                f"manual={_fmt(st['manual_value'], d)}. Review the current values again.")
            continue
        if r["resolution"] == "use_computed" and st["computed_candidate_id"] is None:
            errs.append(f"line {line}: use_computed but no computed value exists")
        if r["resolution"] == "use_manual" and st["manual_candidate_id"] is None:
            errs.append(f"line {line}: use_manual but no manual value exists")
        if r["resolution"] == "use_value":
            text = r["override_value"]
            if not NUMBER_RE.match(text or ""):
                errs.append(f"line {line}: use_value needs a plain numeric override_value")
            elif _text_decimals(text) > d:
                errs.append(f"line {line}: override_value has more than {d} decimals")
        elif r["override_value"]:
            errs.append(f"line {line}: override_value is only allowed with use_value")
        vid = st["validation_id"]
        if vid in active:
            errs.append(f"line {line}: override {active[vid]} is already active for these "
                        "values; revoke it first")
        else:
            active[vid] = oid
        resolves[oid] = {"validation_id": vid, "decided_at": r["decided_at"]}
        approvers.setdefault(oid, set()).add((r["approved_by"] or "").strip().lower())
        plan.append(("resolve", line, r, vid))

    if errs:
        report.status = "rejected"
        store.save(source="manual", target=path.stem, kind="overrides_rejected", body=body,
                   meta={"original_file": path.name, "errors": errs[:20]}, extension="csv")
        return report

    raw_path, digest = store.save(source="manual", target=path.stem, kind="overrides", body=body,
                                  meta={"original_file": path.name}, extension="csv")
    recorded = _iso(_now())
    try:
        with conn:
            for kind, line, r, validation_id in plan:
                if kind == "existing_decision":
                    oid = r["override_id"]
                    if _same_decision(conn, stored_ovr[oid], stored_apr.get(oid), r):
                        report.unchanged += 1
                    else:
                        report.conflicts.append(
                            f"line {line}: entry {oid} differs from the recorded decision. The "
                            "log is append-only: add a revoke and a new entry instead.")
                    continue
                if kind == "existing_approval":
                    if _same_approval(stored_apr[r["override_id"]], r):
                        report.unchanged += 1
                    else:
                        report.conflicts.append(
                            f"line {line}: entry {r['override_id']} differs from the recorded "
                            "approval. The log is append-only.")
                    continue
                if kind in ("resolve", "revoke"):
                    is_value = kind == "resolve" and r["resolution"] == "use_value"
                    conn.execute("""
                        INSERT INTO release_overrides
                            (override_id, action, validation_id, resolution, override_value,
                             override_value_text, revokes_override_id, reviewed_computed_text,
                             reviewed_manual_text, reason_code, justification, evidence_source,
                             evidence_reference, decided_by, decided_at, run_id, raw_path,
                             raw_sha256, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (r["override_id"], kind, validation_id, r["resolution"] or None,
                          float(r["override_value"]) if is_value else None,
                          r["override_value"] if is_value else None,
                          r["target_override_id"] or None if kind == "revoke" else None,
                          (r["reviewed_computed"] or None) if kind == "resolve" else None,
                          (r["reviewed_manual"] or None) if kind == "resolve" else None,
                          r["reason_code"] or None, r["justification"],
                          r["evidence_source"] or None, r["evidence_reference"] or None,
                          r["decided_by"], r["decided_at"], run_id, raw_path, digest, recorded))
                    target = r["override_id"]
                else:  # approve
                    target = r["target_override_id"]
                conn.execute("""
                    INSERT INTO override_approvals
                        (approval_id, override_id, approved_by, approved_at, note,
                         evidence_source, evidence_reference, run_id, raw_path, raw_sha256,
                         recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (r["override_id"], target, r["approved_by"], r["approved_at"],
                      r["justification"] if kind == "approve" else None,
                      (r["evidence_source"] or None) if kind == "approve" else None,
                      (r["evidence_reference"] or None) if kind == "approve" else None,
                      run_id, raw_path, digest, recorded))
                report.inserted += 1
    except sqlite3.IntegrityError as exc:
        report.status, report.inserted, report.unchanged = "rejected", 0, 0
        report.errors.append(f"database rule: {exc} (nothing written)")
        store.save(source="manual", target=path.stem, kind="overrides_rejected", body=body,
                   meta={"original_file": path.name, "errors": report.errors}, extension="csv")
    return report
