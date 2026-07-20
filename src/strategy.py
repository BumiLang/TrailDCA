"""Pure decision functions for the DCA / trailing-take-profit strategy.

All rates are Decimal fractions (0.10 == 10%). No I/O, no side effects —
this module is exercised directly by tests/test_strategy.py.
"""
from __future__ import annotations

from decimal import Decimal

from src.config import (
    FRACTIONAL_DAILY_BUY_KRW,
    FRACTIONAL_RESUME_RATE,
    FRACTIONAL_TARGET_KRW,
    NONFRACTIONAL_BASE_RATE,
    NONFRACTIONAL_STEP_RATE,
    PEAK_ACTIVATION_RATE,
    TRAILING_STOP_PEAK_RATIO,
)


def update_peak_and_threshold(
    peak: Decimal, current_rate: Decimal, current_threshold: Decimal
) -> tuple[Decimal, Decimal]:
    """Rule (every-second tick):
    1. peak = max(peak, current_rate)
    2. if peak >= 10%: threshold = max(peak * 30%, current_rate / 2)
       else: threshold unchanged
    """
    new_peak = max(peak, current_rate)
    if new_peak >= PEAK_ACTIVATION_RATE:
        new_threshold = max(new_peak * TRAILING_STOP_PEAK_RATIO, current_rate / 2)
    else:
        new_threshold = current_threshold
    return new_peak, new_threshold


def should_liquidate(peak: Decimal, current_rate: Decimal, threshold: Decimal) -> bool:
    """Rule 6: peak >= 10% and current_rate <= threshold -> liquidate everything."""
    return peak >= PEAK_ACTIVATION_RATE and current_rate <= threshold


def fractional_daily_buy_amount_krw(
    purchase_amount_krw: Decimal, current_rate: Decimal
) -> Decimal | None:
    """Rules 4.1-4.3 for fractional-tradable symbols.

    - Below the 100,000 KRW accumulation target: keep buying 5,000/day
      regardless of profit rate.
    - At/above target: only keep buying 5,000/day while current_rate > 10%.
      Once profitable, buying continues indefinitely (no re-cap).
    """
    if purchase_amount_krw < FRACTIONAL_TARGET_KRW:
        return Decimal(FRACTIONAL_DAILY_BUY_KRW)
    if current_rate > FRACTIONAL_RESUME_RATE:
        return Decimal(FRACTIONAL_DAILY_BUY_KRW)
    return None


def nonfractional_required_rate(held_qty: Decimal) -> Decimal:
    """10% + (held_qty - 1) * 5%"""
    return NONFRACTIONAL_BASE_RATE + (held_qty - 1) * NONFRACTIONAL_STEP_RATE


def nonfractional_should_buy(held_qty: Decimal, current_rate: Decimal) -> bool:
    """Rule 5.1: only add to an *existing* (held_qty >= 1) non-fractional
    position, and only if current profit rate already clears the
    quantity-scaled bar. A held_qty of 0 never buys (no manual seed position).
    """
    if held_qty < 1:
        return False
    return current_rate >= nonfractional_required_rate(held_qty)


def peak_after_nonfractional_buy(rate_after_buy: Decimal) -> Decimal:
    """Rule 5.2: on a non-fractional new buy, the peak is reassigned to the
    rate resulting from the buy (not maxed against the prior peak).
    """
    return rate_after_buy
