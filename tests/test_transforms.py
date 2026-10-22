"""
Pure transform and comparison tests (no database; synthetic numbers).
Run: pytest tests/test_transforms.py -v
"""
from decimal import Decimal

import pytest

from src.processing.transforms import (TransformError, decimals_of, difference, infer_precision,
                                       pct_change, round_published, shift_month)
from src.processing.validation import compare


# --- transforms ------------------------------------------------------------

def test_pct_change_bounds_contain_value():
    r = pct_change(320.960, 320.000, 0.0005)
    assert r.low < r.value < r.high
    assert r.value == pytest.approx(0.3)


def test_difference_bounds_are_two_half_units():
    r = difference(150.0, 0.0, 0.5)
    assert (r.low, r.value, r.high) == (149.0, 150.0, 151.0)


def test_pct_change_rejects_non_positive_base():
    with pytest.raises(TransformError):
        pct_change(1.0, 0.0, 0.0005)


def test_round_published_is_half_away_from_zero_and_no_negative_zero():
    assert round_published(0.25, 1) == Decimal("0.3")
    assert round_published(-0.25, 1) == Decimal("-0.3")
    assert str(round_published(-0.04, 1)) == "0.0"


def test_decimals_and_precision_inference():
    assert decimals_of(320.0) == 0 and decimals_of(314.796) == 3
    k, _ = infer_precision([300.1, 300.25, 300.125] * 5)
    assert k == 3
    k, note = infer_precision([300.1])          # too few values -> strictest
    assert k == 6 and "strictest" in note


def test_shift_month_crosses_years():
    assert shift_month("2026-01-01", -1) == "2025-12-01"
    assert shift_month("2026-08-01", -12) == "2025-08-01"


# --- comparison rule ---------------------------------------------------------

def _cmp(manual, unrounded, half_width=0.0003, decimals=1):
    rounded = float(round_published(unrounded, decimals))
    return compare(decimals=decimals, manual=manual, computed_rounded=rounded,
                   computed_unrounded=unrounded, low=unrounded - half_width,
                   high=unrounded + half_width)


def test_exact_match():
    assert _cmp(0.3, 0.2987).status == "match"


def test_mismatch_beyond_rounding_is_discrepancy():
    c = _cmp(0.5, 0.2987)
    assert c.status == "discrepancy"
    assert c.difference == Decimal("0.2")
    assert "cannot explain" in c.explanation


def test_one_unit_off_explained_by_rounding():
    # computed 0.24985 rounds to 0.2, but inputs' rounding allows up to 0.25015,
    # which publishes as 0.3: not an error
    assert _cmp(0.3, 0.24985).status == "within_rounding"


def test_one_unit_off_not_explained_by_rounding():
    # 0.2400 can never publish as 0.3 even with input rounding
    assert _cmp(0.3, 0.2400).status == "discrepancy"


def test_integer_change_off_by_one_with_integer_inputs():
    # payroll levels in whole thousands: the change is uncertain by +/-1
    c = compare(decimals=0, manual=150.0, computed_rounded=149.0, computed_unrounded=149.0,
                low=148.0, high=150.0)
    assert c.status == "within_rounding"
    c = compare(decimals=0, manual=150.0, computed_rounded=147.0, computed_unrounded=147.0,
                low=146.0, high=148.0)
    assert c.status == "discrepancy"


def test_missing_sides():
    assert compare(decimals=1, manual=None, computed_rounded=None) is None
    assert compare(decimals=1, manual=0.3, computed_rounded=None).status == "manual_only"
    assert compare(decimals=1, manual=None, computed_rounded=0.3).status == "computed_only"


def test_float_noise_does_not_cause_false_mismatch():
    # 0.1 + 0.2 = 0.30000000000000004 in floating point
    assert _cmp(0.3, 0.1 + 0.2).status == "match"


def test_level_ranges_that_only_touch_do_not_overlap():
    from src.processing.transforms import half_unit, level
    for manual, computed in [(4.4, 4.3), (4.2, 4.3), (3.8, 3.7), (0.2, 0.1)]:
        r = level(computed, half_unit(1))
        assert compare(decimals=1, manual=manual, computed_rounded=computed,
                       computed_unrounded=r.value, low=r.low, high=r.high).status == "discrepancy"
    # a finer input (3 decimals) at the boundary CAN publish either way
    r = level(4.350, half_unit(3))
    assert compare(decimals=1, manual=4.3, computed_rounded=4.4, computed_unrounded=r.value,
                   low=r.low, high=r.high).status == "within_rounding"
