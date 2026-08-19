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
    INITIAL_TAKE_PROFIT_THRESHOLD,
    LIQUIDATION_STAGE_1_DRAWDOWN,
    LIQUIDATION_STAGE_1_MIN_PEAK,
    LIQUIDATION_STAGE_2_DRAWDOWN,
    LIQUIDATION_STAGE_2_MIN_PEAK,
    LIQUIDATION_STAGE_3_DRAWDOWN,
    NONFRACTIONAL_DCA_CEILING_KRW,
    PEAK_ACTIVATION_RATE,
)


def next_liquidation_trigger_rate(peak: Decimal, sell_stage: int) -> Decimal:
    """The profit-rate level at which the NEXT not-yet-fired, currently
    ELIGIBLE staged sell would trigger (see
    update_peak_threshold_and_sell_stage_gated) -- e.g. sell_stage=0 with
    peak >= LIQUIDATION_STAGE_1_MIN_PEAK means the 30%-drawdown partial
    sell hasn't fired yet and is eligible, so the next trigger is
    peak * (1 - LIQUIDATION_STAGE_1_DRAWDOWN). If peak hasn't reached the
    minimum for a given stage, that stage is skipped even if sell_stage
    hasn't reached it yet -- a position whose peak never really took off
    only ever gets the deepest (50%-drawdown) stop-loss. This is this
    row's 익절기준: both the non-fractional fallback-buy entry gate
    (nonfractional_entry_allowed) and the sheet's informational value use
    it, now that liquidation itself is fully staged rather than driven by
    a separate formula.
    """
    if sell_stage < 1 and peak >= LIQUIDATION_STAGE_1_MIN_PEAK:
        return peak * (1 - LIQUIDATION_STAGE_1_DRAWDOWN)
    if sell_stage < 2 and peak >= LIQUIDATION_STAGE_2_MIN_PEAK:
        return peak * (1 - LIQUIDATION_STAGE_2_DRAWDOWN)
    return peak * (1 - LIQUIDATION_STAGE_3_DRAWDOWN)


def update_peak_threshold_and_sell_stage_gated(
    peak: Decimal,
    current_rate: Decimal,
    sell_stage: int,
    purchase_amount_krw: Decimal,
    just_reached_target: bool,
    allow_peak_update: bool = True,
) -> tuple[Decimal, Decimal, int, int, str | None]:
    """Staged trailing-stop liquidation. 익절기준(threshold) is derived
    directly from the same peak/stage state via next_liquidation_trigger_rate
    -- it shows the rate at which the next not-yet-fired stage would sell.

    allow_peak_update gates ONLY the routine "raise peak toward current_rate"
    step (every max(peak, current_rate) below) -- main.py calls this once/tick
    with allow_peak_update=False during regular market-hours ticks, and once a
    day with allow_peak_update=True from daily_snapshot() (around the 08:00
    KST sync, using that tick's holdings rate -- KR hasn't opened and US has
    already closed by then, so it's effectively 전일종가). This does NOT gate
    the hard resets below (just_reached_target, or a brand-new all-time-high
    peak resetting sell_stage to 0) -- those still fire immediately, in real
    time, on whichever tick actually triggers them; only the day-to-day
    "chase the current rate upward" tracking is deferred to once/day. The
    drawdown/action checks further down still run every tick in real time
    against whatever peak is currently on record, so a stage can still fire
    intraday against a peak that was last raised at that morning's snapshot.

    - Below DAILY_BUY_TARGET_KRW while sell_stage is still 0 (never sold
      before): peak tracks in the background (so it's accurate the moment
      the target is crossed), threshold pinned at its inert -100% default,
      no sell action possible -- liquidation only becomes possible once
      the position is fully built out for the first time.
    - The tick the target is first crossed while sell_stage is still 0
      (just_reached_target=True): peak/threshold restart fresh from the
      current rate, discarding whatever peak silently accumulated during
      the DCA phase. Callers also pass True here for any other "start
      fresh now" event that should reset the same way -- e.g. main.py ORs
      in the tick 전략적용여부 just flipped FALSE -> TRUE, discarding
      whatever peak quietly accumulated while unmanaged.
    - Once sell_stage > 0 (a stage has fired at least once), BOTH of the
      above gates are bypassed for good, even if purchase_amount_krw later
      drops back below DAILY_BUY_TARGET_KRW -- which a partial sell
      routinely causes, since liquidate_partial reduces cost basis roughly
      proportionally to the quantity sold. A position that already cleared
      a stage keeps being evaluated normally (peak tracking, drawdown
      checks, further PARTIAL/FULL sells) regardless of purchase_amount_krw
      or just_reached_target -- it no longer gets the "still building out"
      treatment, and sell_stage itself is never silently reset back to 0
      by dropping below target again. Only a brand-new (never-sold)
      position gets the frozen/reset treatment above; peak tracking is
      identical (max(peak, current_rate)) on both sides of the target
      once sell_stage > 0, so there's no discontinuity left to guard
      against at the crossing point either.
    - At/above target on later ticks: normal peak tracking (never
      decreases). A fresh (higher) peak restarts the staged cycle from
      scratch (stage reset to 0) -- a partial sell at one high doesn't
      block another after the position makes a new, higher high and pulls
      back again. At most ONE stage fires per tick -- checked from the
      smallest drawdown up, so a gap straight past an earlier stage (e.g.
      a crash straight to 45% drawdown while stage 0 hasn't fired yet)
      fires the earliest un-fired ELIGIBLE stage first, not the deepest
      one; a later, still-un-fired stage whose bar is still cleared then
      fires on a subsequent tick, working through 30% -> 40% -> 50% in
      order rather than jumping ahead:
        - stage < 1 and peak >= LIQUIDATION_STAGE_1_MIN_PEAK (30%) and
          drawdown >= 30% (LIQUIDATION_STAGE_1_DRAWDOWN): PARTIAL (sell
          LIQUIDATION_STAGE_SELL_FRACTION of current holding).
        - stage < 2 and peak >= LIQUIDATION_STAGE_2_MIN_PEAK (20%) and
          drawdown >= 40% (LIQUIDATION_STAGE_2_DRAWDOWN): same, PARTIAL.
        - stage < 3 and drawdown >= 50% (LIQUIDATION_STAGE_3_DRAWDOWN):
          FULL exit -- no extra peak minimum beyond PEAK_ACTIVATION_RATE,
          it's always the last-resort stop.
      A position whose peak never reached LIQUIDATION_STAGE_1_MIN_PEAK
      skips the 30%-drawdown stage entirely (its bar never becomes
      eligible even if drawdown clears it); below
      LIQUIDATION_STAGE_2_MIN_PEAK the 40%-drawdown stage is skipped too,
      leaving only the 50%-drawdown full exit as a safety net. Nothing can
      fire below PEAK_ACTIVATION_RATE.

    Returns (new_peak, new_threshold, stage_after_peak_bookkeeping,
    next_stage_if_action_fires, action). `stage_after_peak_bookkeeping`
    should always be committed (pure bookkeeping, no I/O) regardless of
    whether the sell order actually executes; `next_stage_if_action_fires`
    should only be committed once the corresponding order actually
    succeeds -- mirrors how a liquidated row is only marked so after a
    successful sell, so a failed/delayed order gets retried next tick
    instead of being silently treated as done.
    """
    if purchase_amount_krw < DAILY_BUY_TARGET_KRW and sell_stage == 0:
        new_peak = max(peak, current_rate) if allow_peak_update else peak
        return new_peak, INITIAL_TAKE_PROFIT_THRESHOLD, 0, 0, None
    if just_reached_target and sell_stage == 0:
        new_peak = current_rate
        threshold = (
            next_liquidation_trigger_rate(new_peak, 0) if new_peak >= PEAK_ACTIVATION_RATE else INITIAL_TAKE_PROFIT_THRESHOLD
        )
        return new_peak, threshold, 0, 0, None

    new_peak = max(peak, current_rate) if allow_peak_update else peak
    stage = 0 if new_peak > peak else sell_stage
    next_stage, action = stage, None
    if new_peak >= PEAK_ACTIVATION_RATE:
        if stage < 1 and new_peak >= LIQUIDATION_STAGE_1_MIN_PEAK and current_rate <= new_peak * (1 - LIQUIDATION_STAGE_1_DRAWDOWN):
            next_stage, action = 1, "PARTIAL"
        elif stage < 2 and new_peak >= LIQUIDATION_STAGE_2_MIN_PEAK and current_rate <= new_peak * (1 - LIQUIDATION_STAGE_2_DRAWDOWN):
            next_stage, action = 2, "PARTIAL"
        elif stage < 3 and current_rate <= new_peak * (1 - LIQUIDATION_STAGE_3_DRAWDOWN):
            next_stage, action = 3, "FULL"
        new_threshold = next_liquidation_trigger_rate(new_peak, stage)
    else:
        new_threshold = INITIAL_TAKE_PROFIT_THRESHOLD
    return new_peak, new_threshold, stage, next_stage, action


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
