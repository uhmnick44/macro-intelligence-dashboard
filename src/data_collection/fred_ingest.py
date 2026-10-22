"""
FRED ingestion for one indicator: metadata check -> paged observations ->
raw snapshot -> parse -> vintage-aware insert.

Two pull modes (config/series.yaml `pull`):

  latest           FRED default real-time period (today). Each row's
                   realtime_start is FRED's "today", so it is stored with
                   vintage_basis = 'retrieval_date': we only know the value
                   was current when we retrieved it.

  vintage_history  realtime_start = config date, realtime_end = 9999-12-31.
                   FRED returns every real-time period that overlaps the
                   window, so each row is a true source vintage
                   ('source_realtime'). A row whose realtime_start equals the
                   requested start may have been clipped to the window
                   boundary, so it is labelled 'source_realtime_clipped'.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.data_collection.fred_client import FredClient, FredError, FredResponse
from src.data_collection.raw_store import RawStore
from src.storage.db import ObsRow, insert_observations, log_fetch

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FREQ_MAP = {"D": "daily", "W": "weekly", "M": "monthly", "Q": "quarterly"}
FRED_MISSING = "."
REALTIME_END_OPEN = "9999-12-31"


@dataclass
class IngestReport:
    indicator_id: str
    status: str = "ok"                       # ok | error
    rows_received: int = 0
    inserted: int = 0
    unchanged: int = 0
    rejected: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------
# Parsing (pure functions, unit-tested without network)
# ---------------------------------------------------------------------

def parse_observations(payload: dict, *, mode: str,
                       requested_realtime_start: str | None = None
                       ) -> tuple[list[ObsRow], list[str]]:
    """Convert a FRED observations payload into ObsRows. Never guesses values."""
    if "observations" not in payload:
        raise FredError("payload has no 'observations' key")

    rows, rejected = [], []
    for i, obs in enumerate(payload["observations"]):
        try:
            date, raw, rt_start = obs["date"], obs["value"], obs["realtime_start"]
        except (KeyError, TypeError):
            rejected.append(f"row {i}: missing date/value/realtime_start: {obs!r}")
            continue
        if not (DATE_RE.match(str(date)) and DATE_RE.match(str(rt_start))):
            rejected.append(f"row {i}: bad date format date={date!r} realtime_start={rt_start!r}")
            continue

        if raw == FRED_MISSING:
            value, status = None, "missing"
        else:
            try:
                value, status = float(raw), "ok"
            except (TypeError, ValueError):
                rejected.append(f"{date}: non-numeric value {raw!r} (not stored)")
                continue

        if mode == "latest":
            basis = "retrieval_date"
        elif mode == "vintage_history":
            basis = ("source_realtime_clipped"
                     if requested_realtime_start and rt_start == requested_realtime_start
                     else "source_realtime")
        else:
            raise ValueError(f"unknown pull mode {mode!r}")

        if rt_start < date:
            rejected.append(f"{date}: realtime_start {rt_start} precedes observation date")
            continue
        rows.append(ObsRow(date, rt_start, basis, value, status))
    return rows, rejected


def parse_last_updated(text: str | None) -> str | None:
    """FRED format '2013-07-31 09:26:16-05' -> '2013-07-31T14:26:16Z'."""
    if not text:
        return None
    t = text.strip()
    if re.search(r"[+-]\d{2}$", t):
        t += "00"
    try:
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S%z")
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_metadata(indicator: dict, info: dict) -> list[str]:
    """Compare configured metadata with what FRED reports. Returns warnings."""
    warnings = []
    fred_freq = FREQ_MAP.get(str(info.get("frequency_short", ""))[:1].upper())
    if fred_freq and fred_freq != indicator["frequency"]:
        warnings.append(f"frequency: config={indicator['frequency']} "
                        f"FRED={info.get('frequency')!r}")
    fred_sa = info.get("seasonal_adjustment_short")
    if fred_sa and fred_sa != indicator["seasonal_adjustment"]:
        warnings.append(f"seasonal_adjustment: config={indicator['seasonal_adjustment']} "
                        f"FRED={fred_sa!r}")
    if info.get("units"):
        warnings_units = info["units"]
        if warnings_units.lower() not in indicator["units"].lower() and \
           indicator["units"].lower() not in warnings_units.lower():
            warnings.append(f"units (informational): config={indicator['units']!r} "
                            f"FRED={warnings_units!r}")
    return warnings


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------

def _snapshot(store: RawStore, resp_or_err, *, series_id: str, kind: str,
              endpoint: str, params: dict, status: int | None, retrieved: datetime):
    body = resp_or_err if isinstance(resp_or_err, bytes) else resp_or_err.body
    return store.save(source="fred", target=series_id, kind=kind, body=body,
                      meta={"endpoint": endpoint, "params": params, "http_status": status},
                      retrieved_at=retrieved)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_indicator(conn: sqlite3.Connection, client: FredClient, store: RawStore,
                     indicator: dict, *, run_id: int, defaults: dict | None = None,
                     now: callable = lambda: datetime.now(timezone.utc)) -> IngestReport:
    defaults = defaults or {}
    ind_id, series_id, mode = indicator["indicator_id"], indicator["source_series_id"], indicator["pull"]
    report = IngestReport(ind_id)

    # 1. Metadata (also confirms the series ID exists)
    retrieved = now()
    try:
        meta = client.series_info(series_id)
    except FredError as exc:
        path, digest = (_snapshot(store, exc.body, series_id=series_id, kind="series_meta_error",
                                  endpoint="series", params={"series_id": series_id},
                                  status=exc.status, retrieved=retrieved)
                        if exc.body else (None, None))
        log_fetch(conn, run_id=run_id, source_id="FRED", target=f"{ind_id}:series_meta",
                  retrieved_at=_iso(retrieved), status="error", raw_path=path,
                  raw_sha256=digest, error_message=str(exc))
        report.status, report.error = "error", str(exc)
        return report

    path, digest = _snapshot(store, meta, series_id=series_id, kind="series_meta",
                             endpoint=meta.endpoint, params=meta.params,
                             status=meta.status, retrieved=retrieved)
    seriess = meta.payload.get("seriess") or []
    info = seriess[0] if seriess else {}
    last_updated = parse_last_updated(info.get("last_updated"))
    log_fetch(conn, run_id=run_id, source_id="FRED", target=f"{ind_id}:series_meta",
              retrieved_at=_iso(retrieved), status="ok" if info else "empty",
              source_last_updated=last_updated, raw_path=path, raw_sha256=digest,
              rows_received=len(seriess))
    report.warnings.extend(check_metadata(indicator, info) if info else ["no series metadata"])

    # 2. Observations, paged
    kwargs = {}
    rt_start = None
    if mode == "vintage_history":
        rt_start = str(indicator.get("vintage_realtime_start")
                       or defaults["vintage_realtime_start"])
        kwargs = {"realtime_start": rt_start, "realtime_end": REALTIME_END_OPEN,
                  "observation_start": str(indicator.get("vintage_observation_start")
                                           or defaults["vintage_observation_start"])}

    offset, page = 0, 0
    while True:
        retrieved = now()
        try:
            resp: FredResponse = client.observations(series_id, offset=offset, **kwargs)
        except FredError as exc:
            path, digest = (_snapshot(store, exc.body, series_id=series_id,
                                      kind=f"observations_p{page}_error",
                                      endpoint="series/observations",
                                      params={"series_id": series_id, "offset": offset, **kwargs},
                                      status=exc.status, retrieved=retrieved)
                            if exc.body else (None, None))
            log_fetch(conn, run_id=run_id, source_id="FRED", target=f"{ind_id}:observations",
                      retrieved_at=_iso(retrieved), status="error", raw_path=path,
                      raw_sha256=digest, error_message=str(exc))
            report.status, report.error = "error", str(exc)
            return report

        path, digest = _snapshot(store, resp, series_id=series_id,
                                 kind=f"observations_{mode}_p{page}", endpoint=resp.endpoint,
                                 params=resp.params, status=resp.status, retrieved=retrieved)
        obs = resp.payload.get("observations") or []
        fetch_id = log_fetch(conn, run_id=run_id, source_id="FRED",
                             target=f"{ind_id}:observations", retrieved_at=_iso(retrieved),
                             status="ok" if obs else "empty", source_last_updated=last_updated,
                             raw_path=path, raw_sha256=digest, rows_received=len(obs))

        rows, rejected = parse_observations(resp.payload, mode=mode,
                                            requested_realtime_start=rt_start)
        result = insert_observations(conn, ind_id, rows, fetch_id)
        report.rows_received += len(obs)
        report.inserted += result.inserted
        report.unchanged += result.unchanged
        report.rejected.extend(rejected)
        report.conflicts.extend(result.conflicts)

        total = int(resp.payload.get("count", len(obs)))
        offset += len(obs)
        page += 1
        if not obs or offset >= total:
            break

    if report.rows_received == 0:
        report.warnings.append("FRED returned no observations")
    return report
