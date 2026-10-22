"""
Configuration loading and validation.

Reads config/sources.yaml, config/series.yaml and environment variables
(.env via python-dotenv). Validation happens here, before anything touches
the database, so a typo in YAML fails loudly instead of producing bad rows.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"

PILLARS = {"growth", "inflation", "labor", "financial", "none"}
FREQUENCIES = {"daily", "weekly", "monthly", "quarterly"}
SEASONAL = {"SA", "SAAR", "NSA", "NA"}
ROLES = {"input", "context", "display"}
PULLS = {"latest", "vintage_history", "manual"}
TIERS = {"official", "publisher", "third_party"}
METHODS = {"api", "manual_entry", "file_import", "reference_only"}
FORMULAS = {
    # formula -> {measure: required lag_months (None = any lag >= 1)}
    "level": {"level": 0, "rate_pct": 0},
    "difference": {"change": None},
    "pct_change": {"mom_pct": 1, "yoy_pct": 12},
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    fred_api_key: str | None
    db_path: Path
    raw_dir: Path
    manual_dir: Path


@dataclass(frozen=True)
class ReleaseMethods:
    version: str
    config_hash: str
    methods: dict          # (indicator_id, measure) -> method dict


@dataclass(frozen=True)
class ApprovalPolicy:
    version: str
    require_independent_approval: bool
    config_hash: str


@dataclass(frozen=True)
class Config:
    sources: list[dict]
    indicators: list[dict]
    assets: list[dict]
    defaults: dict = field(default_factory=dict)
    release_methods: ReleaseMethods | None = None
    approval_policy: ApprovalPolicy | None = None

    def indicator(self, indicator_id: str) -> dict:
        for ind in self.indicators:
            if ind["indicator_id"] == indicator_id:
                return ind
        raise KeyError(indicator_id)


def load_settings(env_file: Path | None = None) -> Settings:
    """Environment-driven settings. The API key is never read from YAML."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or PROJECT_ROOT / ".env")
    except ImportError:  # python-dotenv is optional; plain env vars still work
        pass

    def _path(var: str, default: str) -> Path:
        p = Path(os.environ.get(var, default))
        return p if p.is_absolute() else PROJECT_ROOT / p

    key = os.environ.get("FRED_API_KEY") or None
    return Settings(
        fred_api_key=key,
        db_path=_path("MACRO_DB_PATH", "db/macro.db"),
        raw_dir=_path("MACRO_RAW_DIR", "data/raw"),
        manual_dir=_path("MACRO_MANUAL_DIR", "data/manual"),
    )


def _require(d: dict, keys: list[str], where: str) -> None:
    missing = [k for k in keys if k not in d]
    if missing:
        raise ConfigError(f"{where}: missing keys {missing}")


def _check(value, allowed: set, field_name: str, where: str) -> None:
    if value not in allowed:
        raise ConfigError(f"{where}: {field_name}={value!r} not in {sorted(allowed)}")


def validate(cfg: Config) -> None:
    source_ids = set()
    for s in cfg.sources:
        where = f"source {s.get('source_id')!r}"
        _require(s, ["source_id", "name", "source_tier", "collection_method"], where)
        _check(s["source_tier"], TIERS, "source_tier", where)
        _check(s["collection_method"], METHODS, "collection_method", where)
        if s["source_id"] in source_ids:
            raise ConfigError(f"{where}: duplicate source_id")
        source_ids.add(s["source_id"])

    ids = set()
    for ind in cfg.indicators:
        where = f"indicator {ind.get('indicator_id')!r}"
        _require(ind, ["indicator_id", "name", "pillar", "source_id", "frequency", "units",
                       "seasonal_adjustment", "polarity", "regime_role", "pull"], where)
        _check(ind["pillar"], PILLARS, "pillar", where)
        _check(ind["frequency"], FREQUENCIES, "frequency", where)
        _check(ind["seasonal_adjustment"], SEASONAL, "seasonal_adjustment", where)
        _check(ind["regime_role"], ROLES, "regime_role", where)
        _check(ind["pull"], PULLS, "pull", where)
        _check(ind["polarity"], {-1, 0, 1}, "polarity", where)
        if ind["source_id"] not in source_ids:
            raise ConfigError(f"{where}: source_id {ind['source_id']!r} is not registered")
        if ind["pull"] in ("latest", "vintage_history") and not ind.get("source_series_id"):
            raise ConfigError(f"{where}: API pull requires source_series_id")
        if ind["pull"] == "manual" and ind.get("source_series_id"):
            raise ConfigError(f"{where}: manual series must not have source_series_id")
        if ind["regime_role"] == "input" and ind["polarity"] == 0:
            raise ConfigError(f"{where}: a regime input needs polarity +1 or -1")
        if ind["indicator_id"] in ids:
            raise ConfigError(f"{where}: duplicate indicator_id")
        ids.add(ind["indicator_id"])

    for a in cfg.assets:
        where = f"asset {a.get('asset_id')!r}"
        _require(a, ["asset_id", "name", "asset_class", "quote_type"], where)
        if a.get("indicator_id") and a["indicator_id"] not in ids:
            raise ConfigError(f"{where}: unknown indicator_id {a['indicator_id']!r}")


def load_release_methods(path: Path, indicators: list[dict]) -> ReleaseMethods:
    body = path.read_bytes()
    doc = yaml.safe_load(body)
    version = doc.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ConfigError(f"{path.name}: 'version' must be a non-empty string")
    by_id = {i["indicator_id"]: i for i in indicators}
    methods = {}
    for m in doc.get("methods") or []:
        where = f"release method {m.get('indicator_id')}/{m.get('measure')}"
        _require(m, ["indicator_id", "measure", "formula", "input", "lag_months"], where)
        _check(m["formula"], set(FORMULAS), "formula", where)
        allowed = FORMULAS[m["formula"]]
        if m["measure"] not in allowed:
            raise ConfigError(f"{where}: formula {m['formula']} cannot produce measure "
                              f"{m['measure']} (allowed: {sorted(allowed)})")
        lag, need = m["lag_months"], allowed[m["measure"]]
        if not isinstance(lag, int) or (need is not None and lag != need) or (need is None and lag < 1):
            raise ConfigError(f"{where}: lag_months={lag!r} is inconsistent with "
                              f"{m['formula']}/{m['measure']}")
        for key in ("indicator_id", "input"):
            if m[key] not in by_id:
                raise ConfigError(f"{where}: unknown indicator {m[key]!r}")
        inp = by_id[m["input"]]
        if inp["pull"] != "vintage_history":
            raise ConfigError(f"{where}: input {m['input']} must use pull: vintage_history "
                              "(a first print cannot be computed from latest-revised data)")
        if inp["frequency"] != "monthly":
            raise ConfigError(f"{where}: only monthly inputs are supported")
        k = (m["indicator_id"], m["measure"])
        if k in methods:
            raise ConfigError(f"{where}: defined twice")
        methods[k] = dict(m)
    return ReleaseMethods(version=version.strip(),
                          config_hash=hashlib.sha256(body).hexdigest(), methods=methods)


def load_approval_policy(path: Path) -> ApprovalPolicy:
    body = path.read_bytes()
    doc = yaml.safe_load(body) or {}
    version = doc.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ConfigError(f"{path.name}: 'version' must be a non-empty string")
    flag = doc.get("require_independent_approval")
    if not isinstance(flag, bool):
        raise ConfigError(f"{path.name}: require_independent_approval must be true or false")
    return ApprovalPolicy(version.strip(), flag, hashlib.sha256(body).hexdigest())


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    sources = yaml.safe_load((config_dir / "sources.yaml").read_text())["sources"]
    series = yaml.safe_load((config_dir / "series.yaml").read_text())
    methods_path = config_dir / "release_methods.yaml"
    cfg = Config(
        sources=sources,
        indicators=series["indicators"],
        assets=series.get("assets", []),
        defaults=series.get("defaults", {}),
        release_methods=(load_release_methods(methods_path, series["indicators"])
                         if methods_path.exists() else None),
        approval_policy=(load_approval_policy(config_dir / "approval_policy.yaml")
                         if (config_dir / "approval_policy.yaml").exists() else None),
    )
    validate(cfg)
    return cfg
