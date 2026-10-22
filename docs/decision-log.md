# Macro Intelligence Dashboard: Decision Log

## Phase 1: Architecture
- Stack: Python, SQLite, Streamlit, Plotly, YAML config, pytest.
- Dashboard reads only from the database; it never calls APIs.
- Regimes are descriptive; predictive value is a Phase 9 hypothesis.

## Phase 2: Data sources
- ISM kept in MVP via manual entry (FRED deleted ISM series at ISM's request).
- Consensus: ~24 months of CPI and NFP releases.
- Latest-revised data for MVP trends/regimes; schema vintage-aware from day one.
- Release actuals use first-published values, never revised ones.
- NFCI is the primary financial-conditions measure (ICE BofA HY OAS on FRED limited to a rolling 3-year window since Apr 2026).
- Average hourly earnings counted in Labor only.

## Phase 3: Data model
- ISM: gitignored local CSV; ISM permission is not an MVP dependency.
- Gold kept in MVP from a labeled non-primary (third_party) source.
- One consensus provider across the dataset; provider not hard-coded; every forecast carries forecast_source.
- Surprises are a view; regime evidence normalized; methodology versioned by config hash.

## Phase 4: Ingestion
- Full raw-snapshot retention (every response and manual file, byte-for-byte, SHA-256 in fetch_log).
- Schema v2: vintage_basis 'source_realtime_clipped'; indicators.publisher; sources.collection_method 'reference_only'.
- CPI, core CPI, payrolls, unemployment, AHE pulled with full ALFRED vintage history from 2024-06-01; others use latest.
- Forecast/market-bar sources must have terms_checked_at set before their files load.
- Regime roles in series.yaml are provisional until Phase 7.

## Phase 5: Release value validation
### Decision 1 (2026-10-14): flag mismatches, never trust manual entries automatically
- Computed first prints use only data public on the release date (New York time); the first vintage must be dated exactly on the release date or the figure is not computed.
- Comparison rule "rounding-v1": exact decimal arithmetic; within_rounding only when the computed range (input rounding) and the published figure's rounding range genuinely overlap (touching endpoints do not count).
- Both values always kept (append-only, enforced by database triggers). Revisions stored separately from first prints.
- Year-over-year CPI computed from NSA indexes (CPIAUCNS, CPILFENS). AHE year-over-year from the SA series is an unverified assumption, tested by validation itself.
- Schema migrations are explicit (`migrate`), back up first, and build from frozen schema files.

### Decision 2 (2026-10-14): computed-only values can count as verified
- Three verified levels, distinguished everywhere: verified_crosschecked (computed and published agree), verified_computed (computed only), verified_override (documented decision).
- verified_computed requires: official distributor AND official publisher, first-release vintage, and a passing integrity check.
- Manual-only entries stay unverified until independently validated through an override.
- Computed/manual mismatches are flagged; unresolved figures are excluded from verified calculations.
- Verified surprises only when BOTH the actual is verified AND the consensus forecast is validated: provider terms verified, forecast_as_of before the release (a UTC timestamp if taken on release day), plain number no more precise than published, present in its raw snapshot.
- Every accepted value is re-derived each run from its stored source, vintage, method version and raw snapshot (SHA-256). Inputs must be the method's series, periods and first-release vintage; precision and rounding bounds are re-derived too. Failure makes the figure "unreproducible".
- Nothing is verified unless the latest pipeline run completed a full integrity sweep, so a run that stops early cannot leave old results in force.

### Decision 3 (2026-10-14): self-approved overrides allowed for the MVP
- The creator of an override may approve it. Such figures are verified (verified_override) but labelled `self_approved`; overrides approved by someone other than the decider are labelled `independently_approved`. The label is derived (approver vs decided_by), never typed in.
- Approval status propagates downstream: `actual_assurance` (crosschecked / computed / override_independent / override_self_approved) and the override id and approval status appear in v_release_status, v_release_monitor and v_surprises.
- Every override requires a documented reason (reason_code + justification of at least 10 characters), supporting evidence (evidence_source = a registered PRIMARY source, official or publisher tier, plus an evidence_reference naming the document, table or URL), decided_by/decided_at, and an approval with a UTC approval timestamp.
- Overrides with insufficient evidence are rejected: missing source or reference, unregistered or third-party source, placeholder references.
- Original computed and published values are never altered; the full decision and approval history is append-only and visible in v_override_audit. Every decision and approval is re-verified each run against its raw file snapshot.
- Future-proofing: approvals live in their own table (override_approvals), so an independent reviewer can be added later with an `approve` entry without editing anything. config/approval_policy.yaml (`require_independent_approval`, currently false, version ap-1) controls the rule; switching it to true (new version) makes self-approved overrides unverified until independently approved. Policy activations are recorded. If no policy is active, the strict rule applies.
- Schema v5 (migration v4 -> v5 refuses to run if overrides exist, because evidence sources and approval timestamps cannot be invented).

### Independent reviews
- Round 1 found 5 bugs (level off-by-one passing as rounding, overrides binding to unreviewed values, crash on invalid periods, float rounding in surprise view, override re-pointing undetected).
- Round 2 found 3 bugs (non-plain forecast numbers bypassing precision rules, stored rounding bounds not re-checked, stale checks after an early-stopped run) plus approval-date gaps.
- All fixed with regression tests (154 tests after Decision 3).
