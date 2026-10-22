-- =====================================================================
-- Macro Intelligence Dashboard: database schema (v3)
--
-- v3 changes (Phase 5, migration: src/storage/migrations.py):
--   1. release_values holds only the definition of a release figure.
--      Its value columns (actual, prior_*, actual_origin, fetch_id) are
--      removed. Values now live in release_value_candidates.
--   2. New: release_method_versions, active_versions,
--      release_value_candidates, release_computation_log,
--      release_validations, release_overrides, release_value_revisions.
--   3. Append-only triggers on observations and on every Phase 5 table.
--   4. Views: v_surprises now uses verified values only; new
--      v_release_status, v_release_monitor, v_release_review_queue.
--
-- v2 changes (Phase 4):
--   1. vintage_basis adds 'source_realtime_clipped'
--   2. indicators.publisher (original publisher when distributed via FRED)
--   3. sources.collection_method adds 'reference_only'
-- File: src/storage/schema.sql
--
-- Target: SQLite >= 3.37 (STRICT tables).
-- SQLite foreign keys are OFF by default. Every connection must run:
--   PRAGMA foreign_keys = ON;
--
-- Conventions
--   Dates:        'YYYY-MM-DD'
--   Timestamps:   UTC, 'YYYY-MM-DDTHH:MM:SSZ'. Eastern Time is derived
--                 in Python (zoneinfo), never stored separately.
--   Missing data: NULL plus an explicit status column. Never imputed.
--   "Pillar up":  growth accelerating, inflation heating,
--                 labor strengthening, financial conditions tightening.
--   polarity:     +1 = a rise in the indicator pushes its pillar "up";
--                 -1 = a rise pushes it "down" (e.g. unemployment, claims).
-- =====================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '3');

-- ---------------------------------------------------------------------
-- 1. PROVENANCE
-- ---------------------------------------------------------------------

-- source_tier:
-- collection_method 'reference_only' = we never collect from this source
--   directly; it is referenced as a publisher (e.g. BLS publishes CPI,
--   but we retrieve CPI through FRED).
--
--   official    = government agency, central bank, or its official
--                 distributor (BLS, BEA, Federal Reserve, FRED)
--   publisher   = private original publisher of the data (e.g. ISM)
--   third_party = redistributor, vendor or aggregator (consensus
--                 providers, non-primary gold data)
CREATE TABLE IF NOT EXISTS sources (
    source_id              TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    source_tier            TEXT NOT NULL
                           CHECK (source_tier IN ('official', 'publisher', 'third_party')),
    collection_method      TEXT NOT NULL
                           CHECK (collection_method IN ('api', 'manual_entry', 'file_import', 'reference_only')),
    url                    TEXT,
    usage_terms            TEXT,
    redistribution_allowed INTEGER CHECK (redistribution_allowed IN (0, 1)),  -- NULL = not yet verified
    terms_checked_at       TEXT,
    notes                  TEXT
) STRICT;

-- One version per analytics configuration (config/regime_rules.yaml).
-- config_hash = SHA-256 of that file. Changing the file requires a new version.
CREATE TABLE IF NOT EXISTS methodology_versions (
    version     TEXT PRIMARY KEY,
    config_hash TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id       INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    run_trigger  TEXT NOT NULL CHECK (run_trigger IN ('manual', 'scheduled', 'test')),
    code_version TEXT,   -- git commit hash
    notes        TEXT
) STRICT;

-- One row per series / file retrieved in a run.
CREATE TABLE IF NOT EXISTS fetch_log (
    fetch_id            INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    source_id           TEXT NOT NULL REFERENCES sources(source_id),
    target              TEXT NOT NULL,   -- indicator_id, asset_id or manual file name
    retrieved_at        TEXT NOT NULL,
    source_last_updated TEXT,            -- as reported by the source; NULL if not provided
    raw_path            TEXT,            -- immutable snapshot under data/raw/
    raw_sha256          TEXT,
    rows_received       INTEGER,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
    error_message       TEXT
) STRICT;

-- ---------------------------------------------------------------------
-- 2. ECONOMIC SERIES
-- ---------------------------------------------------------------------

-- Synced from config/series.yaml (the YAML is the source of truth).
CREATE TABLE IF NOT EXISTS indicators (
    indicator_id        TEXT PRIMARY KEY,   -- internal stable key, e.g. 'cpi_headline'
    name                TEXT NOT NULL,
    pillar              TEXT NOT NULL
                        CHECK (pillar IN ('growth', 'inflation', 'labor', 'financial', 'none')),
    source_id           TEXT NOT NULL REFERENCES sources(source_id),
    source_series_id    TEXT,               -- e.g. 'CPIAUCSL'; NULL for manual series
    publisher           TEXT,               -- original publisher, e.g. 'BLS' for CPI via FRED
    frequency           TEXT NOT NULL
                        CHECK (frequency IN ('daily', 'weekly', 'monthly', 'quarterly')),
    units               TEXT NOT NULL,
    seasonal_adjustment TEXT NOT NULL CHECK (seasonal_adjustment IN ('SA', 'SAAR', 'NSA', 'NA')),
    polarity            INTEGER NOT NULL CHECK (polarity IN (-1, 0, 1)),
    regime_role         TEXT NOT NULL CHECK (regime_role IN ('input', 'context', 'display')),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    limitations         TEXT,
    UNIQUE (source_id, source_series_id)
) STRICT;

-- Vintage-aware from day one.
-- vintage_basis records what vintage_date actually means:
--   source_realtime = true vintage from the source (ALFRED real-time period)
--   retrieval_date  = only known to be the value as of our retrieval (MVP default)
--   release_date    = manual entry stamped with the official release date (e.g. ISM)
--   source_realtime_clipped = source vintage whose real-time period began on
--                     the first day of our requested window; the true start
--                     may be earlier, so vintage_date is an upper bound on
--                     first availability (still correct for as-of queries
--                     on or after vintage_date)
CREATE TABLE IF NOT EXISTS observations (
    indicator_id     TEXT NOT NULL REFERENCES indicators(indicator_id),
    observation_date TEXT NOT NULL
                     CHECK (observation_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    vintage_date     TEXT NOT NULL
                     CHECK (vintage_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    vintage_basis    TEXT NOT NULL
                     CHECK (vintage_basis IN ('source_realtime', 'source_realtime_clipped',
                                             'retrieval_date', 'release_date')),
    value            REAL,
    value_status     TEXT NOT NULL CHECK (value_status IN ('ok', 'missing')),
    fetch_id         INTEGER NOT NULL REFERENCES fetch_log(fetch_id),
    PRIMARY KEY (indicator_id, observation_date, vintage_date),
    CHECK ((value_status = 'ok') = (value IS NOT NULL)),
    CHECK (vintage_date >= observation_date)
) STRICT;

-- ---------------------------------------------------------------------
-- 3. RELEASES, FORECASTS, SURPRISES, INTERPRETATION
--    Mathematical surprise, economic interpretation and market reaction
--    are stored separately on purpose.
-- ---------------------------------------------------------------------

-- One row per publication (e.g. the August 2026 CPI report).
CREATE TABLE IF NOT EXISTS release_events (
    event_id          INTEGER PRIMARY KEY,
    release_name      TEXT NOT NULL,   -- e.g. 'CPI', 'Employment Situation', 'ISM Manufacturing'
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    reference_period  TEXT NOT NULL
                      CHECK (reference_period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
                          OR reference_period GLOB '[0-9][0-9][0-9][0-9]-Q[1-4]'
                          OR reference_period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    release_time_utc  TEXT NOT NULL
                      CHECK (release_time_utc GLOB
                        '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z'),
    status            TEXT NOT NULL
                      CHECK (status IN ('scheduled', 'released', 'delayed', 'cancelled')),
    time_is_estimated INTEGER NOT NULL DEFAULT 0 CHECK (time_is_estimated IN (0, 1)),
    notes             TEXT,
    UNIQUE (release_name, reference_period)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_release_events_time ON release_events (release_time_utc);

-- One row per headline figure inside a publication
-- (CPI headline MoM, CPI headline YoY, core MoM, ... are separate rows).
-- This is the DEFINITION of the figure only. Its values (computed and
-- manually entered) live in release_value_candidates; the value used by
-- the dashboard is decided by validation (v_release_status).
CREATE TABLE IF NOT EXISTS release_values (
    value_id           INTEGER PRIMARY KEY,
    event_id           INTEGER NOT NULL REFERENCES release_events(event_id),
    indicator_id       TEXT NOT NULL REFERENCES indicators(indicator_id),
    measure            TEXT NOT NULL
                       CHECK (measure IN ('level', 'mom_pct', 'yoy_pct', 'change', 'rate_pct')),
    unit               TEXT NOT NULL,   -- e.g. 'percent', 'thousands of jobs', 'index'
    published_decimals INTEGER NOT NULL CHECK (published_decimals BETWEEN 0 AND 4),
    UNIQUE (event_id, indicator_id, measure)
) STRICT;

-- forecast_source is mandatory and must exist in sources.
-- The provider is NOT hard-coded: it is added to sources once coverage
-- and permitted usage are verified.
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id       INTEGER PRIMARY KEY,
    value_id          INTEGER NOT NULL REFERENCES release_values(value_id),
    forecast_source   TEXT NOT NULL REFERENCES sources(source_id),
    forecast_value    REAL NOT NULL,
    forecast_as_of    TEXT,             -- when the consensus snapshot was taken, if known
    collected_at      TEXT NOT NULL,
    collection_method TEXT NOT NULL
                      CHECK (collection_method IN ('manual_entry', 'api', 'file_import')),
    fetch_id          INTEGER REFERENCES fetch_log(fetch_id),
    notes             TEXT,
    UNIQUE (value_id, forecast_source)
) STRICT;

-- Economic interpretation is methodology, so it is versioned.
CREATE TABLE IF NOT EXISTS release_interpretations (
    value_id            INTEGER NOT NULL,
    forecast_source     TEXT NOT NULL,
    methodology_version TEXT NOT NULL REFERENCES methodology_versions(version),
    direction           TEXT NOT NULL CHECK (direction IN ('pillar_up', 'pillar_down', 'in_line')),
    rationale           TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (value_id, forecast_source, methodology_version),
    FOREIGN KEY (value_id, forecast_source) REFERENCES forecasts(value_id, forecast_source)
) STRICT;

-- ---------------------------------------------------------------------
-- 3b. RELEASE VALUE VALIDATION (Phase 5)
--
--   candidates   every value of a release figure, from every origin
--                (computed from first-release vintages, or entered manually).
--                Append-only: nothing is ever overwritten.
--   validations  the result of comparing one specific pair of candidates
--                under one rule version. Append-only.
--   overrides    documented human decisions, bound to one validation.
--                Append-only log; a decision is withdrawn by a 'revoke' row.
--   revisions    later values of the same figure (look-ahead safe: kept
--                apart from first prints).
-- ---------------------------------------------------------------------

-- config/release_methods.yaml versions. Same version + different file
-- hash is refused by the pipeline (no silent method changes).
CREATE TABLE IF NOT EXISTS release_method_versions (
    version       TEXT PRIMARY KEY,
    config_hash   TEXT NOT NULL,
    registered_at TEXT NOT NULL
) STRICT;

-- Which method / rule version is currently active. Views read this.
CREATE TABLE IF NOT EXISTS active_versions (
    kind    TEXT PRIMARY KEY CHECK (kind IN ('release_methods', 'validation_rule')),
    version TEXT NOT NULL,
    set_at  TEXT NOT NULL
) STRICT;

-- computed: value = computed value rounded to published_decimals;
--           bound_low/high = range of values consistent with the rounding
--           of the input levels; inputs = JSON list of the exact
--           observations (with vintage dates) used.
-- manual:   value_text = the exact string entered; fetch_id = file snapshot.
CREATE TABLE IF NOT EXISTS release_value_candidates (
    candidate_id    INTEGER PRIMARY KEY,
    value_id        INTEGER NOT NULL REFERENCES release_values(value_id),
    origin          TEXT NOT NULL CHECK (origin IN ('computed', 'manual')),
    value           REAL NOT NULL,
    value_text      TEXT,
    value_unrounded REAL,
    bound_low       REAL,
    bound_high      REAL,
    source_id       TEXT NOT NULL REFERENCES sources(source_id),
    method          TEXT,
    method_version  TEXT REFERENCES release_method_versions(version),
    as_of_date      TEXT,
    inputs          TEXT,
    fetch_id        INTEGER REFERENCES fetch_log(fetch_id),
    run_id          INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    recorded_at     TEXT NOT NULL,
    CHECK ((origin = 'computed') = (method_version IS NOT NULL)),
    CHECK (origin = 'manual' OR (
               value_unrounded IS NOT NULL AND bound_low IS NOT NULL AND bound_high IS NOT NULL
           AND method IS NOT NULL AND inputs IS NOT NULL
           AND as_of_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
           AND bound_low <= value_unrounded AND value_unrounded <= bound_high)),
    CHECK (origin = 'computed' OR (value_text IS NOT NULL AND fetch_id IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_candidate_manual
    ON release_value_candidates (value_id) WHERE origin = 'manual';
CREATE UNIQUE INDEX IF NOT EXISTS ux_candidate_computed
    ON release_value_candidates (value_id, method_version) WHERE origin = 'computed';

-- Every computation attempt, so "why is this not computed?" is answerable.
CREATE TABLE IF NOT EXISTS release_computation_log (
    log_id         INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    value_id       INTEGER NOT NULL REFERENCES release_values(value_id),
    method_version TEXT,
    outcome        TEXT NOT NULL
                   CHECK (outcome IN ('computed', 'unchanged', 'not_computable',
                                      'no_method', 'conflict')),
    reason         TEXT,
    logged_at      TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_computation_log_value ON release_computation_log (value_id, log_id);

CREATE TABLE IF NOT EXISTS release_validations (
    validation_id         INTEGER PRIMARY KEY,
    value_id              INTEGER NOT NULL REFERENCES release_values(value_id),
    computed_candidate_id INTEGER REFERENCES release_value_candidates(candidate_id),
    manual_candidate_id   INTEGER REFERENCES release_value_candidates(candidate_id),
    rule_version          TEXT NOT NULL,
    status                TEXT NOT NULL
                          CHECK (status IN ('match', 'within_rounding', 'discrepancy',
                                            'computed_only', 'manual_only')),
    published_decimals    INTEGER NOT NULL,
    computed_rounded      REAL,
    manual_value          REAL,
    difference            REAL,           -- manual - computed_rounded
    explanation           TEXT NOT NULL,
    run_id                INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    validated_at          TEXT NOT NULL,
    CHECK (computed_candidate_id IS NOT NULL OR manual_candidate_id IS NOT NULL),
    CHECK (status NOT IN ('match', 'within_rounding', 'discrepancy')
           OR (computed_candidate_id IS NOT NULL AND manual_candidate_id IS NOT NULL)),
    CHECK (status != 'computed_only'
           OR (computed_candidate_id IS NOT NULL AND manual_candidate_id IS NULL)),
    CHECK (status != 'manual_only'
           OR (manual_candidate_id IS NOT NULL AND computed_candidate_id IS NULL))
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_validation_pair ON release_validations
    (value_id, IFNULL(computed_candidate_id, 0), IFNULL(manual_candidate_id, 0), rule_version);

-- reason_code vocabulary is fixed so overrides can be counted and audited.
CREATE TABLE IF NOT EXISTS release_overrides (
    override_id         TEXT PRIMARY KEY,
    action              TEXT NOT NULL CHECK (action IN ('resolve', 'revoke')),
    validation_id       INTEGER REFERENCES release_validations(validation_id),
    resolution          TEXT CHECK (resolution IN ('use_computed', 'use_manual', 'use_value')),
    override_value      REAL,
    override_value_text TEXT,
    revokes_override_id TEXT REFERENCES release_overrides(override_id),
    -- the values the analyst actually reviewed (as displayed); NULL = absent.
    -- The loader refuses a resolve whose reviewed values are not the
    -- figure's current values, so a decision can never approve values
    -- nobody looked at.
    reviewed_computed_text TEXT,
    reviewed_manual_text   TEXT,
    reason_code         TEXT CHECK (reason_code IN (
                            'data_entry_error', 'computation_method_limitation',
                            'source_correction', 'verified_against_primary_source', 'other')),
    justification       TEXT NOT NULL CHECK (length(trim(justification)) > 0),
    evidence            TEXT,
    decided_by          TEXT NOT NULL CHECK (length(trim(decided_by)) > 0),
    decided_at          TEXT NOT NULL
                        CHECK (decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    run_id              INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    raw_path            TEXT NOT NULL,   -- snapshot of the overrides file that introduced it
    raw_sha256          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    CHECK (action != 'resolve' OR (validation_id IS NOT NULL AND resolution IS NOT NULL
                                   AND reason_code IS NOT NULL AND revokes_override_id IS NULL)),
    CHECK (action != 'revoke' OR (revokes_override_id IS NOT NULL AND validation_id IS NULL
                                  AND resolution IS NULL AND override_value IS NULL
                                  AND reviewed_computed_text IS NULL
                                  AND reviewed_manual_text IS NULL)),
    CHECK ((resolution IS 'use_value') = (override_value IS NOT NULL)),
    CHECK (resolution IS NOT 'use_value' OR length(trim(IFNULL(evidence, ''))) > 0)
) STRICT;

CREATE TABLE IF NOT EXISTS release_value_revisions (
    value_id        INTEGER NOT NULL REFERENCES release_values(value_id),
    as_of_date      TEXT NOT NULL
                    CHECK (as_of_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    method_version  TEXT NOT NULL REFERENCES release_method_versions(version),
    value_unrounded REAL NOT NULL,
    value_rounded   REAL NOT NULL,
    inputs          TEXT NOT NULL,
    run_id          INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (value_id, as_of_date, method_version)
) STRICT;

-- --- Integrity triggers -------------------------------------------------

-- Both candidates in a validation must belong to the validated figure,
-- and each must have the right origin.
CREATE TRIGGER IF NOT EXISTS trg_validation_candidates_match
BEFORE INSERT ON release_validations
WHEN (NEW.computed_candidate_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM release_value_candidates c
          WHERE c.candidate_id = NEW.computed_candidate_id
            AND c.value_id = NEW.value_id AND c.origin = 'computed'))
  OR (NEW.manual_candidate_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM release_value_candidates c
          WHERE c.candidate_id = NEW.manual_candidate_id
            AND c.value_id = NEW.value_id AND c.origin = 'manual'))
BEGIN
    SELECT RAISE(ABORT, 'validation candidates must belong to the same release value and origin');
END;

-- At most one active (un-revoked) resolve per validation.
CREATE TRIGGER IF NOT EXISTS trg_override_one_active
BEFORE INSERT ON release_overrides
WHEN NEW.action = 'resolve' AND EXISTS (
    SELECT 1 FROM release_overrides o
    WHERE o.action = 'resolve' AND o.validation_id = NEW.validation_id
      AND NOT EXISTS (SELECT 1 FROM release_overrides r
                      WHERE r.action = 'revoke' AND r.revokes_override_id = o.override_id))
BEGIN
    SELECT RAISE(ABORT, 'an active override already exists for this validation; revoke it first');
END;

-- A revoke must target an existing, not-yet-revoked resolve.
CREATE TRIGGER IF NOT EXISTS trg_override_revoke_target
BEFORE INSERT ON release_overrides
WHEN NEW.action = 'revoke' AND (
       NOT EXISTS (SELECT 1 FROM release_overrides o
                   WHERE o.override_id = NEW.revokes_override_id AND o.action = 'resolve')
    OR EXISTS (SELECT 1 FROM release_overrides r
               WHERE r.action = 'revoke' AND r.revokes_override_id = NEW.revokes_override_id))
BEGIN
    SELECT RAISE(ABORT, 'revoke must target an existing resolve that is not already revoked');
END;

-- Append-only: history tables can be added to, never edited or deleted.
CREATE TRIGGER IF NOT EXISTS trg_observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_candidates_no_update BEFORE UPDATE ON release_value_candidates
BEGIN SELECT RAISE(ABORT, 'release_value_candidates is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_candidates_no_delete BEFORE DELETE ON release_value_candidates
BEGIN SELECT RAISE(ABORT, 'release_value_candidates is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_validations_no_update BEFORE UPDATE ON release_validations
BEGIN SELECT RAISE(ABORT, 'release_validations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_validations_no_delete BEFORE DELETE ON release_validations
BEGIN SELECT RAISE(ABORT, 'release_validations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_overrides_no_update BEFORE UPDATE ON release_overrides
BEGIN SELECT RAISE(ABORT, 'release_overrides is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_overrides_no_delete BEFORE DELETE ON release_overrides
BEGIN SELECT RAISE(ABORT, 'release_overrides is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revisions_no_update BEFORE UPDATE ON release_value_revisions
BEGIN SELECT RAISE(ABORT, 'release_value_revisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revisions_no_delete BEFORE DELETE ON release_value_revisions
BEGIN SELECT RAISE(ABORT, 'release_value_revisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_method_versions_no_update BEFORE UPDATE ON release_method_versions
BEGIN SELECT RAISE(ABORT, 'release_method_versions is append-only'); END;

-- ---------------------------------------------------------------------
-- 4. ANALYTICS (trend -> pillar -> regime)
--    data_mode labels whether results used latest-revised data or
--    point-in-time vintages. MVP history is 'latest'.
-- ---------------------------------------------------------------------

-- Window lengths and the acceleration definition are set in Phase 6.
CREATE TABLE IF NOT EXISTS indicator_signals (
    indicator_id        TEXT NOT NULL REFERENCES indicators(indicator_id),
    as_of_date          TEXT NOT NULL
                        CHECK (as_of_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    methodology_version TEXT NOT NULL REFERENCES methodology_versions(version),
    data_mode           TEXT NOT NULL CHECK (data_mode IN ('latest', 'point_in_time')),
    latest_obs_date     TEXT,
    latest_value        REAL,
    short_window_change REAL,
    long_window_change  REAL,
    yoy_change          REAL,
    acceleration        REAL,
    change_unit         TEXT CHECK (change_unit IN ('pct_annualized', 'pct', 'bps', 'points')),
    direction           TEXT NOT NULL
                        CHECK (direction IN ('rising', 'stable', 'falling', 'insufficient_data')),
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (indicator_id, as_of_date, methodology_version, data_mode)
) STRICT;

CREATE TABLE IF NOT EXISTS pillar_states (
    as_of_date          TEXT NOT NULL,
    pillar              TEXT NOT NULL CHECK (pillar IN ('growth', 'inflation', 'labor', 'financial')),
    methodology_version TEXT NOT NULL REFERENCES methodology_versions(version),
    data_mode           TEXT NOT NULL CHECK (data_mode IN ('latest', 'point_in_time')),
    state               TEXT NOT NULL,
    score               REAL,
    inputs_used         INTEGER NOT NULL CHECK (inputs_used >= 0),
    inputs_missing      INTEGER NOT NULL CHECK (inputs_missing >= 0),
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (as_of_date, pillar, methodology_version, data_mode),
    CHECK (
           state = 'insufficient_data'
        OR (pillar = 'growth'    AND state IN ('accelerating', 'stable', 'slowing'))
        OR (pillar = 'inflation' AND state IN ('heating', 'stable', 'cooling'))
        OR (pillar = 'labor'     AND state IN ('strengthening', 'stable', 'weakening'))
        OR (pillar = 'financial' AND state IN ('tightening', 'neutral', 'easing'))
    )
) STRICT;

-- Evidence: which indicator signals produced each pillar state.
-- The UNIQUE constraint stops one indicator from voting in two pillars
-- in the same snapshot (no double-counting the same information).
CREATE TABLE IF NOT EXISTS pillar_state_inputs (
    as_of_date          TEXT NOT NULL,
    pillar              TEXT NOT NULL,
    methodology_version TEXT NOT NULL,
    data_mode           TEXT NOT NULL,
    indicator_id        TEXT NOT NULL,
    polarity            INTEGER NOT NULL CHECK (polarity IN (-1, 0, 1)),  -- snapshot at compute time
    weight              REAL NOT NULL CHECK (weight >= 0),
    contribution        REAL,            -- NULL when the signal is insufficient_data
    PRIMARY KEY (as_of_date, pillar, methodology_version, data_mode, indicator_id),
    UNIQUE (as_of_date, methodology_version, data_mode, indicator_id),
    FOREIGN KEY (as_of_date, pillar, methodology_version, data_mode)
        REFERENCES pillar_states (as_of_date, pillar, methodology_version, data_mode),
    FOREIGN KEY (indicator_id, as_of_date, methodology_version, data_mode)
        REFERENCES indicator_signals (indicator_id, as_of_date, methodology_version, data_mode)
) STRICT;

-- Pillar states for a snapshot are read from pillar_states (same keys),
-- not duplicated here. Label vocabulary is defined in Phase 7.
CREATE TABLE IF NOT EXISTS regime_snapshots (
    as_of_date          TEXT NOT NULL,
    methodology_version TEXT NOT NULL REFERENCES methodology_versions(version),
    data_mode           TEXT NOT NULL CHECK (data_mode IN ('latest', 'point_in_time')),
    regime_label        TEXT NOT NULL,
    conflicts           TEXT,            -- readable list of pillar conflicts; NULL if none
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (as_of_date, methodology_version, data_mode)
) STRICT;

-- ---------------------------------------------------------------------
-- 5. FEDERAL RESERVE
-- ---------------------------------------------------------------------

-- Future meetings are stored with decision = NULL.
CREATE TABLE IF NOT EXISTS fomc_meetings (
    meeting_end_date   TEXT PRIMARY KEY
                       CHECK (meeting_end_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    meeting_start_date TEXT NOT NULL,
    meeting_type       TEXT NOT NULL CHECK (meeting_type IN ('scheduled', 'unscheduled')),
    has_sep            INTEGER CHECK (has_sep IN (0, 1)),  -- Summary of Economic Projections
    decision           TEXT CHECK (decision IN ('hike', 'cut', 'hold')),
    target_lower       REAL,
    target_upper       REAL,
    change_bps         INTEGER,
    dissents           INTEGER CHECK (dissents >= 0),
    statement_url      TEXT,
    source_id          TEXT NOT NULL REFERENCES sources(source_id),
    CHECK ((decision IS NULL) = (target_lower IS NULL)),
    CHECK ((target_lower IS NULL) = (target_upper IS NULL)),
    CHECK (target_upper IS NULL OR target_upper > target_lower),
    CHECK (
           decision IS NULL
        OR (decision = 'hold' AND change_bps = 0)
        OR (decision = 'hike' AND change_bps > 0)
        OR (decision = 'cut'  AND change_bps < 0)
    )
) STRICT;

-- ---------------------------------------------------------------------
-- 6. MARKETS
--    Rule: FRED-backed daily series live in observations (single,
--    vintage-aware ingestion path) and are linked via assets.indicator_id.
--    Bar data from vendors / broker exports live in market_prices.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assets (
    asset_id     TEXT PRIMARY KEY,   -- e.g. 'SPX', 'NDX', 'VIX', 'UST2Y', 'GOLD'
    name         TEXT NOT NULL,
    asset_class  TEXT NOT NULL
                 CHECK (asset_class IN ('equity_index', 'rates', 'fx', 'commodity', 'volatility')),
    quote_type   TEXT NOT NULL CHECK (quote_type IN ('index_level', 'yield_pct', 'price')),
    indicator_id TEXT REFERENCES indicators(indicator_id),  -- set when daily data come from observations
    notes        TEXT
) STRICT;

-- bar_time: '1d' bars use the session date 'YYYY-MM-DD';
--           intraday bars use the UTC bar-open timestamp.
-- instrument_symbol: actual contract (e.g. 'GCZ6') or 'spot', so futures
--           rolls in a continuous series stay identifiable.
CREATE TABLE IF NOT EXISTS market_prices (
    asset_id          TEXT NOT NULL REFERENCES assets(asset_id),
    bar_interval      TEXT NOT NULL CHECK (bar_interval IN ('1d', '1h', '30m', '5m', '1m')),
    bar_time          TEXT NOT NULL,
    instrument_symbol TEXT NOT NULL,
    open              REAL,
    high              REAL,
    low               REAL,
    close             REAL NOT NULL,
    volume            REAL,
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    fetch_id          INTEGER NOT NULL REFERENCES fetch_log(fetch_id),
    PRIMARY KEY (asset_id, bar_interval, bar_time, source_id),
    CHECK (
           (bar_interval = '1d'  AND bar_time GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]')
        OR (bar_interval != '1d' AND bar_time GLOB
             '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z')
    )
) STRICT;

-- Populated from Phase 9 onward. Created now so the schema is complete.
CREATE TABLE IF NOT EXISTS event_reactions (
    event_id            INTEGER NOT NULL REFERENCES release_events(event_id),
    asset_id            TEXT NOT NULL REFERENCES assets(asset_id),
    reaction_window     TEXT NOT NULL CHECK (reaction_window IN ('5m', '30m', '1h', 'eod', '1d')),
    methodology_version TEXT NOT NULL REFERENCES methodology_versions(version),
    pre_time            TEXT NOT NULL,
    pre_value           REAL NOT NULL,
    post_time           TEXT NOT NULL,
    post_value          REAL NOT NULL,
    change_abs          REAL NOT NULL,
    change_unit         TEXT NOT NULL CHECK (change_unit IN ('points', 'bps', 'pct')),
    change_pct          REAL,
    data_origin         TEXT NOT NULL CHECK (data_origin IN ('observations', 'market_prices')),
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (event_id, asset_id, reaction_window, methodology_version)
) STRICT;

-- ---------------------------------------------------------------------
-- 7. VIEWS
-- ---------------------------------------------------------------------

-- Latest known vintage of every observation.
-- Point-in-time ("as of date D") queries take a parameter, so they live
-- in Python: same logic with the extra condition vintage_date <= D.
CREATE VIEW IF NOT EXISTS v_observations_latest AS
SELECT o.indicator_id,
       o.observation_date,
       o.vintage_date,
       o.vintage_basis,
       o.value,
       o.value_status,
       o.fetch_id
FROM observations o
WHERE o.vintage_date = (
    SELECT MAX(o2.vintage_date)
    FROM observations o2
    WHERE o2.indicator_id = o.indicator_id
      AND o2.observation_date = o.observation_date
);

-- Current state of every release figure.
--   current computed candidate = computed with the active method version
--   current validation         = validation of exactly the current
--                                candidate pair under the active rule
--   active override            = resolve bound to the current validation
--                                and not revoked. An override bound to an
--                                older validation is stale and ignored.
-- verified_value is non-NULL only for: match, within_rounding, overridden.
-- For match / within_rounding the published (manual) figure is used.
-- 'unreproducible' (the latest run could not reproduce the stored computed
-- value) takes precedence over everything, including overrides: nothing
-- built on a value that cannot be reproduced counts as verified.
CREATE VIEW IF NOT EXISTS v_release_status AS
WITH cur_comp AS (
    SELECT c.* FROM release_value_candidates c
    WHERE c.origin = 'computed'
      AND c.method_version = (SELECT version FROM active_versions WHERE kind = 'release_methods')
),
cur_man AS (
    SELECT * FROM release_value_candidates WHERE origin = 'manual'
),
cur_val AS (
    SELECT v.* FROM release_validations v
    WHERE v.rule_version = (SELECT version FROM active_versions WHERE kind = 'validation_rule')
      AND v.computed_candidate_id IS (SELECT cc.candidate_id FROM cur_comp cc
                                      WHERE cc.value_id = v.value_id)
      AND v.manual_candidate_id IS (SELECT cm.candidate_id FROM cur_man cm
                                    WHERE cm.value_id = v.value_id)
),
active_ovr AS (
    SELECT o.* FROM release_overrides o
    WHERE o.action = 'resolve'
      AND NOT EXISTS (SELECT 1 FROM release_overrides r
                      WHERE r.action = 'revoke' AND r.revokes_override_id = o.override_id)
),
latest_log AS (
    SELECT l.value_id, l.outcome, l.reason FROM release_computation_log l
    WHERE l.log_id = (SELECT MAX(l2.log_id) FROM release_computation_log l2
                      WHERE l2.value_id = l.value_id)
)
SELECT rv.value_id,
       re.event_id,
       re.release_name,
       re.reference_period,
       re.release_time_utc,
       rv.indicator_id,
       rv.measure,
       rv.unit,
       rv.published_decimals,
       cc.candidate_id    AS computed_candidate_id,
       cc.value           AS computed_value,
       cc.value_unrounded AS computed_unrounded,
       cc.method_version,
       cc.as_of_date      AS computed_as_of_date,
       cm.candidate_id    AS manual_candidate_id,
       cm.value           AS manual_value,
       cm.source_id       AS manual_source,
       cv.validation_id,
       cv.status          AS validation_status,
       cv.explanation     AS validation_explanation,
       ao.override_id,
       ao.resolution      AS override_resolution,
       ll.outcome         AS last_computation_outcome,
       ll.reason          AS last_computation_note,
       CASE
           WHEN ll.outcome = 'conflict' THEN 'unreproducible'
           WHEN ao.override_id IS NOT NULL THEN 'overridden'
           WHEN cv.status IS NOT NULL THEN cv.status
           WHEN re.release_time_utc > strftime('%Y-%m-%dT%H:%M:%SZ', 'now') THEN 'awaiting_release'
           ELSE 'missing'
       END AS final_status,
       CASE
           WHEN ll.outcome = 'conflict'         THEN NULL
           WHEN ao.resolution = 'use_computed' THEN cc.value
           WHEN ao.resolution = 'use_manual'   THEN cm.value
           WHEN ao.resolution = 'use_value'    THEN ao.override_value
           WHEN ao.override_id IS NULL AND cv.status IN ('match', 'within_rounding') THEN cm.value
       END AS verified_value,
       CASE
           WHEN ll.outcome = 'conflict' THEN NULL
           WHEN ao.override_id IS NOT NULL THEN 'override:' || ao.resolution
           WHEN cv.status IN ('match', 'within_rounding') THEN 'published, corroborated by computation'
       END AS verified_origin
FROM release_values rv
JOIN release_events re  ON re.event_id  = rv.event_id
LEFT JOIN cur_comp cc   ON cc.value_id  = rv.value_id
LEFT JOIN cur_man cm    ON cm.value_id  = rv.value_id
LEFT JOIN cur_val cv    ON cv.value_id  = rv.value_id
LEFT JOIN active_ovr ao ON ao.validation_id = cv.validation_id
LEFT JOIN latest_log ll ON ll.value_id  = rv.value_id;

-- Raw surprise = verified actual - forecast, rounded to published precision.
-- Only VERIFIED actuals appear: unresolved discrepancies and single-source
-- values are excluded from every calculation built on this view.
-- Math only: no interpretation, no market reaction.
CREATE VIEW IF NOT EXISTS v_surprises AS
SELECT s.value_id,
       s.event_id,
       s.release_name,
       s.reference_period,
       s.release_time_utc,
       s.indicator_id,
       s.measure,
       s.unit,
       s.published_decimals,
       s.verified_value AS actual,
       s.final_status   AS actual_status,
       f.forecast_value,
       f.forecast_source,
       ROUND(s.verified_value - f.forecast_value, s.published_decimals) AS raw_surprise
FROM v_release_status s
JOIN forecasts f ON f.value_id = s.value_id
WHERE s.verified_value IS NOT NULL;

-- Release monitor: every figure, its status, and both candidate values.
-- surprise is shown only when the actual is verified.
-- previous_as_published = prior period's verified first print.
-- previous_revised      = prior period's computed value as of this
--                         release's first-print date (revisions table).
CREATE VIEW IF NOT EXISTS v_release_monitor AS
SELECT s.value_id,
       s.release_name,
       s.reference_period,
       s.release_time_utc,
       s.indicator_id,
       s.measure,
       s.unit,
       s.final_status,
       s.verified_value AS actual,
       s.computed_value,
       s.manual_value,
       f.forecast_value,
       f.forecast_source,
       CASE WHEN s.verified_value IS NOT NULL AND f.forecast_value IS NOT NULL
            THEN ROUND(s.verified_value - f.forecast_value, s.published_decimals) END AS surprise,
       ps.verified_value AS previous_as_published,
       CASE WHEN s.computed_as_of_date IS NOT NULL AND ps.computed_value IS NOT NULL THEN
           COALESCE((SELECT r.value_rounded FROM release_value_revisions r
                     WHERE r.value_id = ps.value_id
                       AND r.method_version = ps.method_version
                       AND r.as_of_date <= s.computed_as_of_date
                     ORDER BY r.as_of_date DESC LIMIT 1),
                    ps.computed_value)
       END AS previous_revised,
       s.validation_explanation,
       s.last_computation_note
FROM v_release_status s
LEFT JOIN forecasts f ON f.value_id = s.value_id
LEFT JOIN release_events pe
       ON pe.release_name = s.release_name
      AND pe.reference_period = substr(date(s.reference_period || '-01', '-1 month'), 1, 7)
LEFT JOIN release_values pv
       ON pv.event_id = pe.event_id
      AND pv.indicator_id = s.indicator_id
      AND pv.measure = s.measure
LEFT JOIN v_release_status ps ON ps.value_id = pv.value_id;

-- Items needing human review, most serious first.
-- stale_overrides lists active overrides bound to an older validation of
-- this figure: the values they approved have since changed.
CREATE VIEW IF NOT EXISTS v_release_review_queue AS
SELECT s.*,
       CASE s.final_status
           WHEN 'unreproducible' THEN 1
           WHEN 'discrepancy'   THEN 1
           WHEN 'missing'       THEN 2
           WHEN 'manual_only'   THEN 3
           WHEN 'computed_only' THEN 3
       END AS priority,
       (SELECT group_concat(o.override_id, ', ')
        FROM release_overrides o
        JOIN release_validations v ON v.validation_id = o.validation_id
        WHERE o.action = 'resolve'
          AND v.value_id = s.value_id
          AND o.validation_id IS NOT s.validation_id
          AND NOT EXISTS (SELECT 1 FROM release_overrides r
                          WHERE r.action = 'revoke' AND r.revokes_override_id = o.override_id)
       ) AS stale_overrides
FROM v_release_status s
WHERE s.final_status IN ('unreproducible', 'discrepancy', 'missing',
                         'manual_only', 'computed_only');

-- Unified daily market panel with provenance on every row.
-- Assets linked to an indicator read from observations; all other assets
-- read daily bars from market_prices. An asset never appears from both.
CREATE VIEW IF NOT EXISTS v_market_daily AS
SELECT a.asset_id,
       o.observation_date AS session_date,
       o.value            AS close,
       i.source_id,
       s.source_tier,
       'observations'     AS data_origin
FROM assets a
JOIN indicators i            ON i.indicator_id = a.indicator_id
JOIN sources s               ON s.source_id    = i.source_id
JOIN v_observations_latest o ON o.indicator_id = a.indicator_id
WHERE o.value_status = 'ok'
UNION ALL
SELECT m.asset_id,
       m.bar_time         AS session_date,
       m.close,
       m.source_id,
       s.source_tier,
       'market_prices'    AS data_origin
FROM market_prices m
JOIN assets a  ON a.asset_id  = m.asset_id
JOIN sources s ON s.source_id = m.source_id
WHERE m.bar_interval = '1d'
  AND a.indicator_id IS NULL;
