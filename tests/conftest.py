"""
Shared fixtures. Nothing here touches the network or db/macro.db.

All numbers in test payloads are SYNTHETIC values chosen to exercise the
code paths. They are not real economic data.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.data_collection.fred_client import FredClient  # noqa: E402
from src.data_collection.raw_store import RawStore  # noqa: E402
from src.storage import db  # noqa: E402

TEST_KEY = "a" * 32


class FakeResponse:
    def __init__(self, status: int, payload, reason: str = ""):
        self.status_code = status
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.reason = reason


class FakeSession:
    """Returns queued responses in order and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url} {params}")
        return self.responses.pop(0)


def make_client(responses):
    session = FakeSession(responses)
    client = FredClient(TEST_KEY, session=session, sleep=lambda s: None)
    return client, session


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def conn(cfg):
    c = db.connect(":memory:")
    db.init_schema(c)
    db.sync_sources(c, cfg.sources)
    db.sync_indicators(c, cfg.indicators)
    db.sync_assets(c, cfg.assets)
    yield c
    c.close()


@pytest.fixture
def run_id(conn):
    return db.start_run(conn, trigger="test")


@pytest.fixture
def store(tmp_path):
    return RawStore(tmp_path / "raw")


def meta_payload(series_id, frequency_short="M", sa="SA", units="Index"):
    return {"realtime_start": "2026-10-10", "realtime_end": "2026-10-10",
            "seriess": [{"id": series_id, "title": "synthetic", "frequency": "Monthly",
                         "frequency_short": frequency_short, "units": units,
                         "seasonal_adjustment_short": sa,
                         "last_updated": "2026-10-09 07:45:01-05"}]}


def obs_payload(observations, count=None, offset=0):
    return {"count": len(observations) if count is None else count, "offset": offset,
            "limit": 100000, "observations": observations}


# ---------------------------------------------------------------------
# Phase 5 helpers (synthetic values only)
# ---------------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

TS = "2026-10-14T12:00:00Z"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
CPI_RELEASE_UTC = "2026-09-11T12:30:00Z"     # 08:30 New York
CPI_RELEASE_DATE = "2026-09-11"


# Test data is written with REAL raw snapshots, so the integrity checks
# verify it exactly as they verify live data.
_RAW = {}   # id(conn) -> (RawStore, run_id)


@pytest.fixture
def fetch_id(conn, run_id, store):
    _RAW[id(conn)] = (store, run_id)
    yield db.log_fetch(conn, run_id=run_id, source_id="FRED", target="test",
                       retrieved_at=TS, status="ok")
    _RAW.pop(id(conn), None)


def raw_root(conn):
    return _RAW[id(conn)][0].root if id(conn) in _RAW else None


def _series_fetch(conn, indicator):
    """One FRED-style snapshot per indicator, as real ingestion produces."""
    row = conn.execute("SELECT fetch_id, raw_path FROM fetch_log WHERE target = ?",
                       (f"{indicator}:observations",)).fetchone()
    if row:
        return row[0], row[1]
    store, run_id = _RAW[id(conn)]
    body = json.dumps({"observations": []}).encode()
    path, sha = store.save(source="fred", target=indicator, kind="observations_test",
                           body=body, meta={"test": True})
    fid = db.log_fetch(conn, run_id=run_id, source_id="FRED", target=f"{indicator}:observations",
                       retrieved_at=TS, status="ok", raw_path=path, raw_sha256=sha)
    return fid, path


def add_obs(conn, fetch_id, indicator, obs_date, vintage_date, value,
            basis="source_realtime"):
    """Insert an observation AND add it to its series' raw snapshot."""
    import hashlib
    fid, path = _series_fetch(conn, indicator)
    f = _RAW[id(conn)][0].root / path
    payload = json.loads(f.read_bytes())
    payload["observations"].append({
        "realtime_start": vintage_date, "realtime_end": "9999-12-31", "date": obs_date,
        "value": "." if value is None else repr(float(value))})
    body = json.dumps(payload).encode()
    f.write_bytes(body)
    conn.execute("UPDATE fetch_log SET raw_sha256 = ? WHERE fetch_id = ?",
                 (hashlib.sha256(body).hexdigest(), fid))
    conn.execute("""INSERT INTO observations (indicator_id, observation_date, vintage_date,
                    vintage_basis, value, value_status, fetch_id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (indicator, obs_date, vintage_date, basis, value,
                  "ok" if value is not None else "missing", fid))
    conn.commit()


def seed_cpi_history(conn, fetch_id, indicator="cpi_headline", last_value=320.000):
    """13 monthly first prints, 2025-07 .. 2026-07, each first published on the
    10th of the next month; July 2026 = last_value. Values carry 3 decimals."""
    months = [f"{2025 + (6 + i) // 12}-{(6 + i) % 12 + 1:02d}-01" for i in range(13)]
    for i, m in enumerate(months):
        y, mo = int(m[:4]), int(m[5:7])
        vy, vm = (y + 1, 1) if mo == 12 else (y, mo + 1)
        add_obs(conn, fetch_id, indicator, m, f"{vy}-{vm:02d}-10",
                round(last_value - (12 - i) * 0.731, 3))


def add_figure(conn, *, value_id=1, release_name="CPI", period="2026-08",
               release_time=CPI_RELEASE_UTC, indicator="cpi_headline", measure="mom_pct",
               unit="percent", decimals=1):
    ev = conn.execute("SELECT event_id FROM release_events WHERE release_name = ? "
                      "AND reference_period = ?", (release_name, period)).fetchone()
    event_id = ev[0] if ev else conn.execute(
        "INSERT INTO release_events (release_name, source_id, reference_period, "
        "release_time_utc, status) VALUES (?, 'BLS', ?, ?, 'released')",
        (release_name, period, release_time)).lastrowid
    conn.execute("INSERT INTO release_values (value_id, event_id, indicator_id, measure, unit, "
                 "published_decimals) VALUES (?, ?, ?, ?, ?, ?)",
                 (value_id, event_id, indicator, measure, unit, decimals))
    conn.commit()
    return value_id


def add_manual(conn, run_id, fetch_id, value_id, text, source="BLS"):
    """A manual candidate backed by a real actuals.csv snapshot."""
    from src.data_collection.manual_loader import ACTUAL_COLUMNS
    f = conn.execute("""SELECT re.release_name, re.reference_period, re.release_time_utc,
                               rv.indicator_id, rv.measure, rv.unit, rv.published_decimals
                        FROM release_values rv JOIN release_events re USING (event_id)
                        WHERE rv.value_id = ?""", (value_id,)).fetchone()
    row = [f[0], f[1], f[2], "BLS", f[3], f[4], f[5], str(f[6]), text, source, ""]
    body = (",".join(ACTUAL_COLUMNS) + "\n" + ",".join(row) + "\n").encode()
    store = _RAW[id(conn)][0]
    path, sha = store.save(source="manual", target=f"actuals_{value_id}", kind="actuals",
                           body=body, meta={}, extension="csv")
    fid = db.log_fetch(conn, run_id=run_id, source_id=source, target="manual:actuals.csv",
                       retrieved_at=TS, status="ok", raw_path=path, raw_sha256=sha)
    conn.execute("INSERT INTO release_value_candidates (value_id, origin, value, value_text, "
                 "source_id, fetch_id, run_id, recorded_at) VALUES (?, 'manual', ?, ?, ?, ?, ?, ?)",
                 (value_id, float(text), text, source, fid, run_id, TS))
    conn.commit()


def process(conn, cfg, run_id):
    """Compute, revise, validate and run integrity checks, as the pipeline does."""
    from src.pipeline import process_releases
    problems = []
    result = process_releases(conn, cfg, run_id, problems, now=NOW, raw_root=raw_root(conn))
    return result, problems


def status_row(conn, value_id=1):
    return conn.execute("SELECT * FROM v_release_status WHERE value_id = ?",
                        (value_id,)).fetchone()
