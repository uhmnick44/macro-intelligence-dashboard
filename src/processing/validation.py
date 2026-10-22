"""
Release value validation: compare the computed first print with the
manually entered published figure.

Rule (RULE_VERSION = "rounding-v1")

  P = published figure (manual), with d decimals
  R = computed value rounded to d decimals
  [low, high) = computed range allowed by rounding of the input levels
  [P - u/2, P + u/2) = true values that publish as P (u = 10^-d)

  match            R == P
  within_rounding  R != P, but the two ranges share at least one value: the
                   difference is fully explained by rounding, so it is not
                   an error. Ranges that only TOUCH at an endpoint do not
                   overlap (a value that publishes as 4.3 can never publish
                   as 4.4). All arithmetic is exact decimal.
  discrepancy      the ranges do not overlap: rounding cannot explain it
  computed_only    no manual figure to compare against
  manual_only      no computed figure (see the computation log for why)

Verified (usable by dashboard calculations): match, within_rounding, or a
documented override. Single-source values are NOT verified: one value
cannot be cross-checked.

Changing this logic requires a new RULE_VERSION; old results are kept.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from src.processing.release_values import set_active
from src.processing.transforms import half_unit, round_published
from src.storage.db import utc_now

RULE_VERSION = "rounding-v1"
VERIFIED_STATUSES = ("match", "within_rounding", "overridden")


@dataclass(frozen=True)
class Comparison:
    status: str
    computed_rounded: Decimal | None
    manual: Decimal | None
    difference: Decimal | None
    explanation: str


def _q(x: float, d: int) -> Decimal:
    return round_published(x, d)


def _exact(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def compare(*, decimals: int, manual: float | None, computed_rounded: float | None,
            computed_unrounded: float | None = None, low: float | None = None,
            high: float | None = None) -> Comparison | None:
    """Pure comparison. Returns None when there is nothing to compare."""
    if manual is None and computed_rounded is None:
        return None
    if computed_rounded is None:
        p = _q(manual, decimals)
        return Comparison("manual_only", None, p, None,
                          f"Only a manually entered value ({p}) exists; it could not be "
                          "cross-checked by computation.")
    r = _q(computed_rounded, decimals)
    if manual is None:
        return Comparison("computed_only", r, None, None,
                          f"Only a computed value ({r}) exists; no published figure was "
                          "entered to cross-check it.")

    p = _q(manual, decimals)
    diff = p - r
    show = decimals + 3
    unr = f"{computed_unrounded:.{show}f}" if computed_unrounded is not None else str(r)
    if diff == 0:
        return Comparison("match", r, p, diff,
                          f"Computed {unr} rounds to {r}; published {p}. Match.")

    hu = half_unit(decimals)
    p_lo, p_hi = p - hu, p + hu
    c_lo = _exact(low) if low is not None else r
    c_hi = _exact(high) if high is not None else r
    rng = f"{c_lo:.{show}f} to {c_hi:.{show}f}"
    pub = f"{p_lo:.{show}f} to {p_hi:.{show}f}"
    if max(c_lo, p_lo) < min(c_hi, p_hi):     # strict: touching is not overlapping
        return Comparison("within_rounding", r, p, diff,
                          f"Computed {unr} rounds to {r}; published {p}. Rounding explains the "
                          f"difference: computed range {rng} overlaps the published figure's "
                          f"range {pub}.")
    return Comparison("discrepancy", r, p, diff,
                      f"Computed {unr} rounds to {r}; published {p} (difference {diff}). "
                      f"Computed range {rng} does not overlap the published figure's range "
                      f"{pub}: rounding cannot explain it.")


@dataclass
class ValidateReport:
    new: int = 0
    unchanged: int = 0
    by_status: dict = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def validate_all(conn: sqlite3.Connection, *, run_id: int, method_version: str) -> ValidateReport:
    report = ValidateReport()
    rows = conn.execute("""
        SELECT rv.value_id, rv.published_decimals,
               cc.candidate_id, cc.value, cc.value_unrounded, cc.bound_low, cc.bound_high,
               cm.candidate_id, cm.value
        FROM release_values rv
        LEFT JOIN release_value_candidates cc
               ON cc.value_id = rv.value_id AND cc.origin = 'computed' AND cc.method_version = ?
        LEFT JOIN release_value_candidates cm
               ON cm.value_id = rv.value_id AND cm.origin = 'manual'
    """, (method_version,)).fetchall()

    with conn:
        set_active(conn, "validation_rule", RULE_VERSION)
        for (vid, decimals, cc_id, cc_val, cc_unr, lo, hi, cm_id, cm_val) in rows:
            cmp = compare(decimals=decimals, manual=cm_val, computed_rounded=cc_val,
                          computed_unrounded=cc_unr, low=lo, high=hi)
            if cmp is None:
                continue
            report.by_status[cmp.status] = report.by_status.get(cmp.status, 0) + 1
            stored = conn.execute("""
                SELECT status FROM release_validations
                WHERE value_id = ? AND IFNULL(computed_candidate_id, 0) = IFNULL(?, 0)
                  AND IFNULL(manual_candidate_id, 0) = IFNULL(?, 0) AND rule_version = ?
            """, (vid, cc_id, cm_id, RULE_VERSION)).fetchone()
            if stored is not None:
                if stored[0] == cmp.status:
                    report.unchanged += 1
                else:
                    report.conflicts.append(
                        f"value_id {vid}: re-validation gives {cmp.status}, stored {stored[0]} "
                        f"under {RULE_VERSION} (kept stored)")
                continue
            conn.execute("""
                INSERT INTO release_validations
                    (value_id, computed_candidate_id, manual_candidate_id, rule_version, status,
                     published_decimals, computed_rounded, manual_value, difference, explanation,
                     run_id, validated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (vid, cc_id, cm_id, RULE_VERSION, cmp.status, decimals,
                  float(cmp.computed_rounded) if cmp.computed_rounded is not None else None,
                  float(cmp.manual) if cmp.manual is not None else None,
                  float(cmp.difference) if cmp.difference is not None else None,
                  cmp.explanation, run_id, utc_now()))
            report.new += 1
    return report
