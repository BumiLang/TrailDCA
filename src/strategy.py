"""Pure decision functions for the DCA / trailing-take-profit strategy.

All rates are Decimal fractions (0.10 == 10%). No I/O, no side effects —
this module is exercised directly by tests/test_strategy.py.
"""
from __future__ import annotations

from decimal import Decimal

from src.config import (
    DAILY_BUY_KRW,
    DAILY_BUY_RESUME_RATE,
    DAILY_BUY_TARGET_KRW,
    NONFRACTIONAL_DCA_CEILING_KRW,
    PEAK_ACTIVATION_RATE,
    TAKE_PROFIT_BREAKPOINT,
    TAKE_PROFIT_HIGH_SLOPE,
    TAKE_PROFIT_LOW_BASE,
    TAKE_PROFIT_LOW_SLOPE,
)


def update_peak_and_threshold(
    peak: Decimal, current_rate: Decimal, current_threshold: Decimal
) -> tuple[Decimal, Decimal]:
    """Rule (every-second tick):
    1. peak = max(peak, current_rate)
    2. if peak >= 10%:
         threshold = peak * 0.8 - 3%   (peak < 30%)   -- threshold == 5% right at peak == 10%
         threshold = peak * 0.7        (peak >= 30%)  -- meets the low branch exactly at 30%
       else: threshold unchanged
    """
    new_peak = max(peak, current_rate)
    if new_peak >= PEAK_ACTIVATION_RATE:
        if new_peak < TAKE_PROFIT_BREAKPOINT:
            new_threshold = new_peak * TAKE_PROFIT_LOW_SLOPE + TAKE_PROFIT_LOW_BASE
        else:
            new_threshold = new_peak * TAKE_PROFIT_HIGH_SLOPE
    else:
        new_threshold = current_threshold
    return new_peak, new_threshold


def should_liquidate(peak: Decimal, current_rate: Decimal, threshold: Decimal, purchase_amount_krw: Decimal) -> bool:
    """Rule 6: peak >= 10% and current_rate <= threshold -> liquidate everything.

    Skipped entirely while purchase_amount_krw is still below
    DAILY_BUY_TARGET_KRW -- a position that hasn't finished its initial DCA
    accumulation shouldn't be fully exited on a dip; liquidation only
    becomes possible once the position is fully built out.
    """
    if purchase_amount_krw < DAILY_BUY_TARGET_KRW:
        return False
    return peak >= PEAK_ACTIVATION_RATE and current_rate <= threshold


def daily_buy_amount_krw(
    purchase_amount_krw: Decimal, current_rate: Decimal
) -> Decimal | None:
    """Rule 4: once/day DCA buy target, in KRW.

    - Below the 100,000 KRW accumulation target: keep buying 5,000/day
      regardless of profit rate.
    - At/above target: only keep buying 5,000/day while current_rate >= 10%.
      Once profitable, buying continues indefinitely (no re-cap).
    """
    if purchase_amount_krw < DAILY_BUY_TARGET_KRW:
        return Decimal(DAILY_BUY_KRW)
    if current_rate >= DAILY_BUY_RESUME_RATE:
        return Decimal(DAILY_BUY_KRW)
    return None


def peak_after_share_buy(rate_after_buy: Decimal) -> Decimal:
    """When the amount-based buy fails and a single whole share is bought as
    a fallback, the peak is reassigned to the rate resulting from that buy
    (not maxed against the prior peak) -- a whole-share purchase can shift
    the cost basis enough that the old peak/threshold no longer applies.
    """
    return rate_after_buy


def nonfractional_is_dca_grace_window(current_purchase_krw: Decimal, projected_purchase_krw: Decimal) -> bool:
    """True while current_purchase_krw is still below DAILY_BUY_TARGET_KRW
    (100,000) and adding this share would keep cumulative purchase amount
    below NONFRACTIONAL_DCA_CEILING_KRW (130,000) -- the unconditional
    "keep DCAing regardless of rate" window, with headroom past the
    100,000 target since a single whole-share buy can jump straight past
    it in one step."""
    return current_purchase_krw < DAILY_BUY_TARGET_KRW and projected_purchase_krw < NONFRACTIONAL_DCA_CEILING_KRW


def nonfractional_entry_allowed(
    current_purchase_krw: Decimal,
    projected_purchase_krw: Decimal,
    projected_rate: Decimal,
    take_profit_threshold: Decimal,
) -> bool:
    """Whether the whole-share fallback buy may fire.

    - DCA grace window (see nonfractional_is_dca_grace_window): buy
      regardless of profit rate, same spirit as the fractional path.
    - Still below DAILY_BUY_TARGET_KRW but this buy pushes
      projected_purchase_krw to/past NONFRACTIONAL_DCA_CEILING_KRW: only
      the flat PEAK_ACTIVATION_RATE (10%) floor applies -- not the
      (possibly higher) trailing take-profit threshold -- so leaving the
      grace window isn't blocked just because the trailing stop has
      climbed above 10%.
    - Otherwise (current_purchase_krw already at/above target): the rate
      that would RESULT from adding this share must clear
      max(PEAK_ACTIVATION_RATE, take_profit_threshold).
    """
    if nonfractional_is_dca_grace_window(current_purchase_krw, projected_purchase_krw):
        return True
    if current_purchase_krw < DAILY_BUY_TARGET_KRW:
        return projected_rate >= PEAK_ACTIVATION_RATE
    return projected_rate >= max(PEAK_ACTIVATION_RATE, take_profit_threshold)
