# Macro Intelligence Dashboard

Economic data → historical context → trend → surprise → macro regime →
Fed implications → market context.

Status: Phase 5 (release value validation) complete. Full documentation comes in Phase 10.

## Documentation

- [Architecture](docs/architecture.md): layers, pipeline, verification levels, roadmap
- [Data sources](docs/data-sources.md): indicators, constraints, vintages
- [Decision log](docs/decision-log.md): every design decision by phase

## Setup

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then add your FRED API key
python -m src.pipeline init-db
python -m src.pipeline ingest
python -m src.pipeline review     # figures needing a human decision
pytest -v
```

Upgrading a database created before Phase 5: `python -m src.pipeline migrate`
(backs up the file first; the pipeline refuses old databases until you do).

## Data provenance rules

- Every API response and manual file is saved byte-for-byte under `data/raw/`
  (gitignored) and its SHA-256 recorded in `fetch_log`.
- Missing values are stored as NULL with `value_status = 'missing'`; nothing is imputed.
- Existing rows are never overwritten; changed values become new vintages or
  are reported as conflicts.
- ISM data is licensed: it lives only in a local, gitignored `data/manual/ism.csv`.

## Manual data files

Copy a header from `data/manual/templates/` into `data/manual/`:

| File | Contents |
|---|---|
| `ism.csv` | ISM PMI actuals (local only) |
| `forecasts.csv` | Consensus forecasts: one provider, registered in `config/sources.yaml` with `terms_checked_at`; give `forecast_as_of` so the forecast can be validated as pre-release |
| `actuals.csv` | Published release figures, typed exactly as published (cross-checked; never verified on their own) |
| `overrides.csv` | Documented review decisions (append-only; see below) |
| `fomc_meetings.csv` | FOMC dates and decisions from federalreserve.gov |
| `market_bars/*.csv` | Daily bars from non-primary vendors (e.g. gold), one source per file |

## Release value validation (Phase 5)

Every release figure (e.g. CPI m/m for 2026-08) can have two independent values:

- **computed**: derived from the first-release vintage, using only data public on
  the release date (`config/release_methods.yaml`, versioned and hashed);
- **manual**: the published figure from `actuals.csv`.

They are compared at published precision, allowing for rounding of the inputs.
Each figure then gets a **verification level**; only verified values feed calculations:

| Verification | When | Value used |
|---|---|---|
| `verified_crosschecked` | computed and published agree (`match` / `within_rounding`) | published figure |
| `verified_computed` | only a computed value; distributor and publisher are official; first-release vintage | computed value |
| `verified_override` | a documented decision with evidence and approval | the value it selects |
| `unverified` | discrepancies, manual-only values, failed or not-yet-run checks | none |

Every verified level also requires the value to pass its **integrity check** in the
latest pipeline run (`integrity_checks`, `integrity_sweeps`): it is re-derived from its
stored source, vintage, method version and raw snapshot (SHA-256 verified). If
anything no longer reproduces, the figure becomes `unreproducible` and drops out.

**Surprises** (`v_surprises`) are calculated only when the actual is verified AND
the consensus forecast is validated: provider terms verified, `forecast_as_of`
before the release (a UTC timestamp if taken on release day), no more precise
than the published figure, and present in its raw snapshot.

Both values are always kept. Nothing is overwritten (database triggers enforce it).
Later revisions are stored separately from first prints, so backtests can't
pick up revised numbers by accident.

**Review workflow:** run `python -m src.pipeline review`, check the figure against
the primary source, then add a row to `data/manual/overrides.csv` (the review
output prints a ready-to-fill row). The file is an append-only log with three entry
types:

| Entry | Purpose | Required |
|---|---|---|
| `resolve` | choose `use_computed`, `use_manual` or `use_value` | reason code + justification, `evidence_source` (a registered primary source, e.g. BLS) + `evidence_reference` (document, table or URL), the values reviewed, `decided_by`/`decided_at`, `approved_by` + `approved_at` (UTC timestamp) |
| `approve` | add a further approval to a resolve (`target_override_id`) | justification, evidence, `approved_by` + `approved_at` |
| `revoke` | withdraw a resolve (`target_override_id`) | justification, `decided_by`/`decided_at`, approval |

A resolve is refused if its reviewed values are no longer current, if the figure is
already verified, or if its evidence is insufficient (missing, a third-party source,
or a placeholder reference). If the values change after approval, the override goes
stale and the figure returns to the queue. Recorded entries are never edited, and the
original computed and published values are never altered: `v_override_audit` shows
the complete history.

**Approvals.** Approval status is derived, never typed: `self_approved` when only the
decider approved, `independently_approved` once someone else has. It is carried into
`v_release_status`, `v_release_monitor` and `v_surprises` (`actual_assurance` =
`override_self_approved` / `override_independent`), so any surprise built on a
self-approved value stays identifiable. The MVP policy (`config/approval_policy.yaml`)
allows self-approval; setting `require_independent_approval: true` (with a new
version) makes self-approved overrides unverified until an `approve` entry by
someone else is added. Policy activations are recorded.

**Schema versions** are frozen in `src/storage/schema_history/`. `migrate` upgrades
step by step from those frozen files and backs up the database first.
