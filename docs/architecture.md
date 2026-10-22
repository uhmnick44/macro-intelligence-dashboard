# Architecture

Economic data → historical context → trend → surprise → macro regime →
Fed implications → market context.

Status: Phases 1–5 built (architecture, data sources, data model, ingestion,
release value validation). Trend calculations (Phase 6) are next.

## Layers

Each layer only talks to the one next to it.

```
[Sources]       FRED / ALFRED · manual CSVs (ISM, consensus, FOMC, gold)
    ↓
[Ingestion]     API client + manual loaders → immutable raw snapshots (SHA-256)
    ↓
[Storage]       SQLite: observations (vintage-aware), releases, candidates,
                validations, overrides, approvals, integrity checks
    ↓
[Analytics]     first prints → validation → integrity checks → (Phase 6+)
                trend engine → pillar states → regime engine
    ↓
[Presentation]  Streamlit + Plotly (Phase 8): reads the database only
```

Design rule: the dashboard never calls an API. It reads processed results
from the database, which keeps it fast, testable and reproducible.

## Pipeline

`python -m src.pipeline ingest` runs, in order:

1. **FRED** — metadata check, paged observations, raw snapshot, vintage-aware insert.
2. **Manual files** — ISM, FOMC, forecasts, actuals, market bars. One bad row
   rejects the whole file; every file is snapshotted.
3. **First prints** — each release figure computed from data public on the release
   date (`config/release_methods.yaml`), plus later revisions kept separately.
4. **Validation** — computed vs published, rounding-aware (`rounding-v1`).
5. **Integrity checks** — every accepted value re-derived from its stored source,
   vintage, method version and raw snapshot.
6. **Overrides** — documented review decisions and approvals, then their checks.

## Verification levels

| Level | Meaning |
|---|---|
| `verified_crosschecked` | computed and published values agree |
| `verified_computed` | computed from an official first-release vintage only |
| `verified_override` | documented decision with evidence and approval (`self_approved` or `independently_approved`) |
| `unverified` | everything else; excluded from calculations |

Surprises (`v_surprises`) require a verified actual **and** a validated
pre-release consensus forecast, and carry the actual's assurance label.

## Principles

- **Never fabricate.** Missing data is NULL with an explicit status.
- **Never overwrite.** History tables are append-only, enforced by database triggers.
- **No look-ahead.** Point-in-time reads exclude data published after the as-of date.
- **Versioned methodology.** Method and policy files are hashed; changing one
  without a new version is refused. Schema versions are frozen in
  `src/storage/schema_history/` and upgraded only by the explicit `migrate` command.
- **Regimes are descriptive.** Whether a regime predicts market returns is a
  hypothesis for the Phase 9 event studies, not an assumption.

## Folder structure

```
config/          series, sources, release methods, approval policy (YAML)
data/manual/     local CSV inputs (gitignored); templates/ holds headers only
data/raw/        immutable raw snapshots (gitignored)
db/              SQLite database (gitignored)
docs/            architecture, data sources, decision log
src/
  data_collection/   FRED client, ingestion, manual loaders, raw store
  processing/        transforms, point-in-time reads, first prints,
                     validation, integrity checks, approvals
  storage/           schema, migrations, database access
  analysis/, regime_engine/   Phases 6–7
tests/           pytest suite (synthetic data only; no network)
```

## Roadmap

| Phase | Scope |
|---|---|
| 6 | Trend engine: 3m / 6m / YoY changes, direction with a deadband |
| 7 | Regime methodology: pillar states, growth × inflation quadrant, conflicts |
| 8 | Streamlit dashboard |
| 9 | Market reaction / event studies (daily first, intraday later) |
| 10 | Testing, documentation, GitHub presentation |
