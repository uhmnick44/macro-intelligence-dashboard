"""
Pipeline entry point.

    python -m src.pipeline init-db          create / verify the database
    python -m src.pipeline ingest           FRED + manual files
    python -m src.pipeline ingest --only cpi_core,payrolls
    python -m src.pipeline ingest --skip-fred      manual files only
    python -m src.pipeline review           list release figures needing review
    python -m src.pipeline migrate          upgrade an older database (backs up first)

Ingest order: FRED -> manual files -> compute first prints and revisions
-> validate -> integrity checks -> overrides.csv -> integrity checks of
overrides. Overrides load after validation because they bind to the
validation they review.

Data-quality flags (discrepancies, unverified values, forecasts without an
as-of time) are reported but do not fail a run; reproducibility failures do.

Run status: success = every step ok; partial = something failed or was
skipped but other data loaded; failed = nothing could run.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from src.config import PROJECT_ROOT, load_config, load_settings
from src.data_collection import manual_loader
from src.data_collection.fred_client import FredClient
from src.data_collection.fred_ingest import ingest_indicator
from src.data_collection.raw_store import RawStore
from src.processing import approvals, integrity, release_values, validation
from src.storage import db, migrations


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def init_db(settings, cfg):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    db.sync_sources(conn, cfg.sources)
    db.sync_indicators(conn, cfg.indicators)
    db.sync_assets(conn, cfg.assets)
    return conn


def run_ingest(settings, cfg, *, only: set[str] | None = None, skip_fred: bool = False,
               client: FredClient | None = None, trigger: str = "manual") -> dict:
    conn = init_db(settings, cfg)
    store = RawStore(settings.raw_dir)
    run_id = db.start_run(conn, trigger=trigger, code_version=_git_commit())
    summary = {"run_id": run_id, "fred": [], "manual": [], "problems": []}
    try:
        return _run(conn, store, settings, cfg, summary, only, skip_fred, client)
    except Exception as exc:
        # never leave a run marked 'running': record the abort, then re-raise
        db.finish_run(conn, run_id, "failed", f"aborted: {type(exc).__name__}: {exc}"[:4000])
        conn.close()
        raise


def _load_file(conn, store, path, loader, run_id, summary):
    """One manual file. An unexpected error is reported; other files still load."""
    try:
        rep = loader(conn, store, path, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - isolate one bad file from the rest of the run
        summary["problems"].append(f"{path.name}: unexpected error ({type(exc).__name__}: {exc})")
        return
    summary["manual"].append((path.name, rep))
    if rep.status != "ok":
        summary["problems"].append(f"{path.name}: rejected ({len(rep.errors)} errors)")
    if rep.conflicts:
        summary["problems"].append(f"{path.name}: {len(rep.conflicts)} conflicts")


def _run(conn, store, settings, cfg, summary, only, skip_fred, client) -> dict:
    run_id = summary["run_id"]

    # ---- FRED ----
    fred_inds = [i for i in cfg.indicators
                 if i["pull"] in ("latest", "vintage_history") and i.get("is_active", True)
                 and (only is None or i["indicator_id"] in only)]
    if skip_fred:
        pass
    elif client is None and not settings.fred_api_key:
        summary["problems"].append("FRED_API_KEY not set: all FRED series skipped (missing)")
    else:
        client = client or FredClient(settings.fred_api_key)
        for ind in fred_inds:
            rep = ingest_indicator(conn, client, store, ind, run_id=run_id, defaults=cfg.defaults)
            summary["fred"].append(rep)
            if rep.status != "ok":
                summary["problems"].append(f"{rep.indicator_id}: {rep.error}")
            if rep.conflicts or rep.rejected:
                summary["problems"].append(
                    f"{rep.indicator_id}: {len(rep.conflicts)} conflicts, "
                    f"{len(rep.rejected)} rejected rows")

    # ---- Manual files ----
    m = settings.manual_dir
    jobs = [(m / "ism.csv", manual_loader.load_ism),
            (m / "fomc_meetings.csv", manual_loader.load_fomc),
            (m / "forecasts.csv", manual_loader.load_forecasts),
            (m / "actuals.csv", manual_loader.load_actuals)]
    jobs += [(p, manual_loader.load_market_bars)
             for p in sorted((m / "market_bars").glob("*.csv"))] if (m / "market_bars").is_dir() else []
    for path, loader in jobs:
        if not path.exists():
            summary["manual"].append((path.name, "not present"))
            continue
        _load_file(conn, store, path, loader, run_id, summary)

    # ---- Release values: compute, revise, validate, then overrides ----
    summary["releases"] = process_releases(conn, cfg, run_id, summary["problems"],
                                           raw_root=settings.raw_dir)
    ovr = m / "overrides.csv"
    if summary["releases"] is not None and ovr.exists():
        _load_file(conn, store, ovr, manual_loader.load_overrides, run_id, summary)
        rep = integrity.run_checks(conn, run_id=run_id, rm=cfg.release_methods,
                                   raw_root=settings.raw_dir, subjects=("override",))
        _note_integrity(rep, summary["problems"])
        summary["releases"]["integrity"].failures.extend(rep.failures)
    summary["release_status"] = release_status_counts(conn)
    summary["assurance"] = {r[0]: r[1] for r in conn.execute(
        "SELECT actual_assurance, COUNT(*) FROM v_release_status "
        "WHERE actual_assurance IS NOT NULL GROUP BY actual_assurance")}
    summary["review_queue"] = [dict(r) for r in conn.execute(
        "SELECT * FROM v_release_review_queue ORDER BY priority, release_time_utc")]
    summary["forecast_issues"] = forecast_issues(conn)

    loaded_any = any(r.status == "ok" for r in summary["fred"]) or any(
        not isinstance(r, str) and r.status == "ok" for _, r in summary["manual"])
    status = ("success" if not summary["problems"] else "partial" if loaded_any else "failed")
    db.finish_run(conn, run_id, status, "; ".join(summary["problems"])[:4000] or None)
    summary["status"] = status
    conn.close()
    return summary


def _note_integrity(rep, problems: list[str]) -> None:
    for subject, ref, severity, detail in rep.failures:
        if severity == integrity.REPRO:
            problems.append(f"reproducibility: {subject} {ref}: {detail}")


def process_releases(conn, cfg, run_id: int, problems: list[str], *, now=None, raw_root=None):
    rm = cfg.release_methods
    if rm is None:
        problems.append("config/release_methods.yaml missing: release values not computed")
        return None
    if cfg.approval_policy is None:
        problems.append("config/approval_policy.yaml missing: release values not processed")
        return None
    try:
        release_values.register_method_version(conn, rm)
        approvals.register_policy(conn, cfg.approval_policy)
    except (release_values.MethodVersionError, approvals.PolicyVersionError) as exc:
        problems.append(str(exc))
        return None
    comp = release_values.compute_first_prints(conn, rm, run_id=run_id, now=now)
    release_values.compute_revisions(conn, rm, run_id=run_id, report=comp)
    val = validation.validate_all(conn, run_id=run_id, method_version=rm.version)
    for c in comp.conflicts + val.conflicts:
        problems.append(f"reproducibility: {c}")
    checks = integrity.run_checks(conn, run_id=run_id, rm=rm, raw_root=raw_root)
    _note_integrity(checks, problems)
    return {"compute": comp, "validate": val, "integrity": checks}


def release_status_counts(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT verification, COUNT(*) FROM v_release_status GROUP BY verification")}


def forecast_issues(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM v_forecast_status WHERE forecast_check IS NOT 'pass' "
        "ORDER BY release_time_utc")]


def print_review_queue(rows) -> None:
    if not rows:
        print("Review queue: empty")
        return
    print(f"Review queue ({len(rows)}):")
    for r in rows:
        d = r["published_decimals"]
        comp = "" if r["computed_value"] is None else f"{r['computed_value']:.{d}f}"
        man = "" if r["manual_value"] is None else f"{r['manual_value']:.{d}f}"
        print(f"  [{r['final_status']}] {r['release_name']} {r['reference_period']} "
              f"{r['indicator_id']}/{r['measure']}  computed={comp or 'none'} "
              f"manual={man or 'none'}")
        if r["unverified_reason"]:
            print(f"      why unverified: {r['unverified_reason']}")
        if r["computed_value"] is None and r["last_computation_note"]:
            print(f"      not computed: {r['last_computation_note']}")
        if r["stale_overrides"]:
            print(f"      stale override(s): {r['stale_overrides']} (values changed since "
                  "approval; review again)")
        if r["final_status"] == "overridden" and r["override_id"]:
            print(f"      overrides.csv approve row: <ID>,approve,,,,,,,,,,<note>,<evidence_source>,"
                  f"<evidence_reference>,,,<reviewer other than {r['override_decided_by']}>,"
                  f"<YYYY-MM-DDTHH:MM:SSZ>,{r['override_id']}")
        if r["final_status"] in ("discrepancy", "manual_only", "computed_only"):
            print(f"      overrides.csv row: <ID>,resolve,{r['release_name']},"
                  f"{r['reference_period']},{r['indicator_id']},{r['measure']},{comp},{man},"
                  "<resolution>,,<reason_code>,<justification>,<evidence_source>,"
                  "<evidence_reference>,<decided_by>,<YYYY-MM-DD>,<approved_by>,"
                  "<YYYY-MM-DDTHH:MM:SSZ>,")


def print_forecast_issues(rows) -> None:
    if rows:
        print(f"Forecasts not validated ({len(rows)}): excluded from surprises")
        for r in rows:
            print(f"  {r['release_name']} {r['reference_period']} {r['indicator_id']}/"
                  f"{r['measure']} {r['forecast_value']} ({r['forecast_source']}): "
                  f"{r['forecast_check_detail'] or 'not checked yet'}")


def print_summary(summary: dict) -> None:
    print(f"\nRun {summary['run_id']}: {summary['status'].upper()}")
    for r in summary["fred"]:
        line = (f"  {r.indicator_id:<22} {r.status:<5} received={r.rows_received:<6} "
                f"new={r.inserted:<6} unchanged={r.unchanged}")
        print(line)
        for w in r.warnings:
            print(f"      warning: {w}")
        for x in (r.rejected + r.conflicts)[:5]:
            print(f"      {x}")
        if r.error:
            print(f"      error: {r.error}")
    for name, r in summary["manual"]:
        if isinstance(r, str):
            print(f"  {name:<22} {r}")
        else:
            print(f"  {name:<22} {r.status:<8} rows={r.rows} new={r.inserted} "
                  f"unchanged={r.unchanged}")
            for x in (r.errors + r.conflicts)[:10]:
                print(f"      {x}")
    rel = summary.get("releases")
    if rel:
        c, k = rel["compute"], rel["integrity"]
        print(f"\nRelease values: computed={c.computed} unchanged={c.unchanged} "
              f"not_computable={c.not_computable} no_method={c.no_method} "
              f"revisions_added={c.revisions_added}")
        print(f"Integrity checks: {k.checked} run, {k.passed} passed, {len(k.failures)} failed")
    if summary.get("release_status"):
        print("Verification: " + ", ".join(f"{k}={v}" for k, v in
                                           sorted(summary["release_status"].items())))
    if summary.get("assurance"):
        print("Assurance:    " + ", ".join(f"{k}={v}" for k, v in
                                           sorted(summary["assurance"].items())))
    if "review_queue" in summary:
        print_review_queue(summary["review_queue"])
    if "forecast_issues" in summary:
        print_forecast_issues(summary["forecast_issues"])
    if summary["problems"]:
        print("\nProblems:")
        for p in summary["problems"]:
            print(f"  - {p}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("migrate")
    sub.add_parser("review")
    ing = sub.add_parser("ingest")
    ing.add_argument("--only", help="comma-separated indicator_ids")
    ing.add_argument("--skip-fred", action="store_true")
    args = parser.parse_args(argv)

    settings, cfg = load_settings(), load_config()
    if args.cmd == "init-db":
        init_db(settings, cfg).close()
        print(f"Database ready: {settings.db_path}")
        return 0

    if args.cmd == "migrate":
        conn = db.connect(settings.db_path)
        for line in migrations.migrate(conn, db_path=settings.db_path):
            print(line)
        conn.close()
        return 0
    if args.cmd == "review":
        conn = init_db(settings, cfg)
        print_review_queue([dict(r) for r in conn.execute(
            "SELECT * FROM v_release_review_queue ORDER BY priority, release_time_utc")])
        print_forecast_issues(forecast_issues(conn))
        conn.close()
        return 0

    only = set(args.only.split(",")) if args.only else None
    if only:
        unknown = only - {i["indicator_id"] for i in cfg.indicators}
        if unknown:
            parser.error(f"unknown indicator_ids: {sorted(unknown)}")
    summary = run_ingest(settings, cfg, only=only, skip_fred=args.skip_fred)
    print_summary(summary)
    return 0 if summary["status"] == "success" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, migrations.MigrationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
