"""
Pure numeric transforms used to compute release figures.

Every result comes with BOUNDS: the lowest and highest value the formula
could give if the input levels were anywhere inside their own rounding
(e.g. a CPI index published to 3 decimals could truly be +/- 0.0005).
Validation uses the bounds to tell a genuine discrepancy from a
rounding artefact.

No database access here; everything is unit-tested directly.
Trend transforms (3m/6m annualized) are added in Phase 6.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

MAX_DECIMALS = 6
MIN_SAMPLE_FOR_INFERENCE = 12


class TransformError(ValueError):
    pass


@dataclass(frozen=True)
class Result:
    value: float     # unrounded
    low: float       # bounds from input rounding
    high: float


def _d(x) -> Decimal:
    """Exact decimal of a stored value: 4.3 -> Decimal('4.3'), not 4.2999999..."""
    return x if isinstance(x, Decimal) else Decimal(repr(float(x)))


def _r(lo: Decimal, val: Decimal, hi: Decimal) -> Result:
    return Result(float(val), float(lo), float(hi))


def decimals_of(x: float) -> int:
    """Decimal places in the shortest representation of x (320.0 -> 0, 314.796 -> 3)."""
    exp = Decimal(repr(float(x))).normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def infer_precision(values: list[float]) -> tuple[int, str]:
    """
    Decimal precision of a published series, inferred from its stored values.

    Uses the MAXIMUM decimals seen. Over many observations it is very
    unlikely that every value ends in zero, so this recovers the published
    precision. If anything it errs strict (narrower bounds -> more items
    flagged for review, never fewer). With too few values to infer, the
    strictest precision is assumed.
    """
    usable = [v for v in values if v is not None]
    if len(usable) < MIN_SAMPLE_FOR_INFERENCE:
        return MAX_DECIMALS, (f"only {len(usable)} values; assumed {MAX_DECIMALS} decimals "
                              "(strictest)")
    k = min(MAX_DECIMALS, max(decimals_of(v) for v in usable))
    return k, f"inferred from {len(usable)} stored values"


def half_unit(decimals: int) -> Decimal:
    """Half of one unit in the last decimal: 1 decimal -> 0.05 (exact)."""
    return Decimal("0.5").scaleb(-decimals)


# Bounds are computed in exact decimal arithmetic, so an exact boundary
# (e.g. 4.3 + 0.05 = 4.35) is decided exactly, not by float noise.

def level(current, input_half_unit) -> Result:
    c, v = _d(current), _d(input_half_unit)
    return _r(c - v, c, c + v)


def difference(current, base, input_half_unit) -> Result:
    d, v = _d(current) - _d(base), _d(input_half_unit)
    return _r(d - 2 * v, d, d + 2 * v)


def pct_change(current, base, input_half_unit) -> Result:
    c, b, v = _d(current), _d(base), _d(input_half_unit)
    if b - v <= 0 or c - v < 0:
        raise TransformError("percent change needs positive levels")
    with localcontext() as ctx:
        ctx.prec = 34
        value = (c / b - 1) * 100
        # monotonic: rises with current, falls with base
        low = ((c - v) / (b + v) - 1) * 100
        high = ((c + v) / (b - v) - 1) * 100
    return _r(low, value, high)


def apply(formula: str, current, base, input_half_unit) -> Result:
    if formula == "level":
        return level(current, input_half_unit)
    if base is None:
        raise TransformError(f"{formula} needs a base value")
    if formula == "difference":
        return difference(current, base, input_half_unit)
    if formula == "pct_change":
        return pct_change(current, base, input_half_unit)
    raise TransformError(f"unknown formula {formula!r}")


def round_published(x: float, decimals: int) -> Decimal:
    """Round half away from zero to the published precision. -0.0 becomes 0.0."""
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return q.copy_abs() if q == 0 else q


def shift_month(observation_date: str, months: int) -> str:
    """'2026-08-01' shifted by -12 -> '2025-08-01'. Month-start dates only."""
    y, m, d = (int(p) for p in observation_date.split("-"))
    if d != 1:
        raise TransformError(f"monthly observation dates must be the 1st: {observation_date}")
    total = y * 12 + (m - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01"
