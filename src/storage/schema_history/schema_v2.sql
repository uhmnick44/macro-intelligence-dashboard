-- =====================================================================
-- Macro Intelligence Dashboard: database schema (v2)
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

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '2');

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
-- actual = value AS FIRST PUBLISHED, rounded as published. Never the revised value.
CREATE TABLE IF NOT EXISTS release_values (
    value_id           INTEGER PRIMARY KEY,
    event_id           INTEGER NOT NULL REFERENCES release_events(event_id),
    indicator_id       TEXT NOT NULL REFERENCES indicators(indicator_id),
    measure            TEXT NOT NULL
                       CHECK (measure IN ('level', 'mom_pct', 'yoy_pct', 'change', 'rate_pct')),
    unit               TEXT NOT NULL,   -- e.g. 'percent', 'thousands of jobs', 'index'
    published_decimals INTEGER NOT NULL CHECK (published_decimals BETWEEN 0 AND 4),
    actual             REAL,
    prior_as_published REAL,            -- prior period as it stood before this release
    prior_revised      REAL,            -- prior period as revised in this release
    actual_origin      TEXT NOT NULL
                       CHECK (actual_origin IN ('alfred_first_release', 'manual_entry', 'pending')),
    fetch_id           INTEGER REFERENCES fetch_log(fetch_id),
    UNIQUE (event_id, indicator_id, measure),
    CHECK ((actual IS NULL) = (actual_origin = 'pending'))
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

-- Raw surprise = actual - forecast, rounded to the published precision
-- (removes floating-point noise such as 0.3 - 0.2 = 0.0999...).
-- This is math only: no interpretation, no market reaction.
CREATE VIEW IF NOT EXISTS v_surprises AS
SELECT rv.value_id,
       re.event_id,
       re.release_name,
       re.reference_period,
       re.release_time_utc,
       rv.indicator_id,
       rv.measure,
       rv.unit,
       rv.published_decimals,
       rv.actual,
       f.forecast_value,
       f.forecast_source,
       ROUND(rv.actual - f.forecast_value, rv.published_decimals) AS raw_surprise
FROM release_values rv
JOIN release_events re ON re.event_id = rv.event_id
JOIN forecasts f       ON f.value_id  = rv.value_id
WHERE rv.actual IS NOT NULL;

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
