from decimal import Decimal

from src import strategy


def D(s: str) -> Decimal:
    return Decimal(s)


class TestNextLiquidationTriggerRate:
    def test_stage_0_uses_15pct_price_drawdown(self):
        # peak=40% -> price = cost*1.40 at peak; a 15% price drawdown means
        # price = cost*1.40*0.85 = cost*1.19 -> rate = 0.19
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 0) == D("0.19")

    def test_stage_1_uses_30pct_price_drawdown(self):
        # cost*1.40*0.70 = cost*0.98 -> rate = -0.02
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 1) == D("-0.02")

    def test_stage_2_uses_45pct_price_drawdown(self):
        # cost*1.40*0.55 = cost*0.77 -> rate = -0.23
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 2) == D("-0.23")

    def test_stage_3_and_beyond_returns_full_exit_floor(self):
        # once all three staged partial sells have fired, the only
        # remaining trigger is the absolute FULL_EXIT_PROFIT_RATE_FLOOR --
        # independent of peak
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 3) == D("0.03")
        assert strategy.next_liquidation_trigger_rate(D("0.90"), 5) == D("0.03")

    def test_low_peak_uses_the_same_formula_no_eligibility_gating(self):
        # unlike the old design, there's no per-stage minimum peak anymore
        # -- stage 0 uses the same formula even for a peak just above
        # PEAK_ACTIVATION_RATE: cost*1.16*0.85 = cost*0.986 -> rate = -0.014
        assert strategy.next_liquidation_trigger_rate(D("0.16"), 0) == D("-0.014")


class TestLiquidationSellFraction:
    def test_stage_0_sells_33pct_of_current_holding(self):
        assert strategy.liquidation_sell_fraction(0) == D("0.33")

    def test_stage_1_sells_77pct_of_current_holding(self):
        assert strategy.liquidation_sell_fraction(1) == D("0.77")

    def test_stage_2_sells_88pct_of_current_holding(self):
        assert strategy.liquidation_sell_fraction(2) == D("0.88")


class TestDisplayedLiquidationTriggerRate:
    def test_matches_raw_trigger_when_above_the_floor(self):
        # peak=40%, stage 0 -> raw trigger 19%, well above the 3% floor --
        # nothing to clamp
        assert strategy.displayed_liquidation_trigger_rate(D("0.40"), 0) == D("0.19")

    def test_clamped_up_to_the_floor_when_raw_trigger_is_below_it(self):
        # peak=15.56%, stage 0 -> raw trigger (1.1556)*(0.85)-1 = -1.77%,
        # below the 3% floor -- since the floor always fires first in
        # practice (see update_peak_threshold_and_sell_stage_gated), showing
        # -1.77% would misrepresent what's actually about to happen, so the
        # displayed value is clamped up to the floor instead
        assert strategy.displayed_liquidation_trigger_rate(D("0.1556"), 0) == D("0.03")

    def test_stage_3_and_beyond_is_already_the_floor_unaffected_by_clamping(self):
        assert strategy.displayed_liquidation_trigger_rate(D("0.40"), 3) == D("0.03")


class TestUpdatePeakThresholdAndSellStageGated:
    def test_below_target_no_action_and_stage_frozen_at_zero(self):
        # would otherwise be well past every trigger, but purchase amount
        # hasn't reached the 100k DCA target yet -- no action, stage stays
        # 0 (nothing to preserve yet), threshold pinned inert
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.20"), 0, D("99999"), just_reached_target=False
        )
        assert peak == D("0.40")
        assert threshold == D("-1.00")
        assert stage == 0
        assert next_stage == 0
        assert action is None

    def test_below_target_gate_no_longer_applies_once_a_stage_has_fired(self):
        # a prior partial sell (stage 2) shrank purchase_amount_krw back
        # below the 100k target (liquidate_partial reduces cost basis
        # roughly proportionally to quantity sold) -- unlike a never-sold
        # position, this one is evaluated normally regardless: no NEW high
        # here (0.30 < peak 0.40) so stage stays 2, and 0.30 clears neither
        # stage 2's own trigger (-0.23) nor the FULL_EXIT_PROFIT_RATE_FLOOR
        # (0.03) so no action, but the threshold is a REAL computed value,
        # not the inert -100% -- displayed clamped up to the 3% floor since
        # the raw -0.23 trigger is unreachable (the floor would fire first)
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 2, D("75000"), just_reached_target=False
        )
        assert peak == D("0.40")
        assert threshold == D("0.03")  # raw trigger (1.40)*(1-0.45)-1=-0.23, clamped to the 3% floor
        assert stage == 2
        assert next_stage == 2
        assert action is None

    def test_below_target_partial_sell_can_still_fire_once_a_stage_has_fired(self):
        # sell_stage=1 already fired once; purchase_amount_krw is back
        # below the 100k target after that partial sell, but stage-2
        # eligibility (30%-price-drawdown trigger) still gets evaluated and
        # fires normally instead of being gated off. current (4%) is above
        # FULL_EXIT_PROFIT_RATE_FLOOR (3%), so this is PARTIAL, not FULL.
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.50"), D("0.04"), 1, D("80000"), just_reached_target=False
        )
        assert peak == D("0.50")
        assert threshold == D("0.05")  # next trigger: (1.50)*(1-0.30)-1
        assert stage == 1
        assert next_stage == 2
        assert action == "PARTIAL"

    def test_crossing_target_no_longer_resets_peak_once_a_stage_has_fired(self):
        # sell_stage (2) is carried over, and -- unlike the never-sold
        # case -- peak is NOT reset to the current rate on this crossing
        # either, since peak tracking (max(peak, current_rate)) has been
        # running continuously on both sides of the target once a stage
        # has fired; next trigger is computed for stage 2, not stage 0
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 2, D("100000"), just_reached_target=True
        )
        assert peak == D("0.40")
        assert threshold == D("0.03")  # raw trigger -0.23, clamped to the floor
        assert stage == 2
        assert next_stage == 2
        assert action is None

    def test_crossing_target_full_exit_can_fire_once_a_stage_has_fired(self):
        # current (2%) is below FULL_EXIT_PROFIT_RATE_FLOOR (3%) on the
        # exact crossing tick -- with sell_stage already at 2, this is no
        # longer gated off or treated as an inert "just reached target"
        # reset; the FULL exit fires like any other tick
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.02"), 2, D("100000"), just_reached_target=True
        )
        assert peak == D("0.40")
        assert threshold == D("0.03")  # raw trigger -0.23, clamped to the floor
        assert (stage, next_stage, action) == (2, 3, "FULL")

    def test_no_action_above_stage_0_trigger(self):
        # peak=40%, current=25% -- above both stage 0's trigger (19%) and
        # the 3% floor
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.25"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("0.19")  # next trigger: (1.40)*(1-0.15)-1

    def test_stage_0_partial_sell_at_its_price_drawdown_trigger(self):
        # peak=40%, current exactly at stage 0's trigger (19%), still above
        # the 3% floor -> PARTIAL
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.19"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_stage_1_partial_sell_requires_a_high_enough_peak_to_clear_the_floor(self):
        # peak needs to be high enough that stage 1's trigger rate itself
        # is above FULL_EXIT_PROFIT_RATE_FLOOR, or the floor always wins
        # first (see the floor-priority tests below) -- at peak=50%, stage
        # 1's trigger is (1.50)*(1-0.30)-1 = 0.05, above the 3% floor
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.50"), D("0.05"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 2, "PARTIAL")
        assert threshold == D("0.05")

    def test_stage_2_partial_sell_requires_an_even_higher_peak(self):
        # stage 2's 45%-drawdown trigger needs a much higher peak than
        # stages 0/1 before it clears the 3% floor (roughly 87.3%, see
        # FULL_EXIT_PROFIT_RATE_FLOOR's comment in config.py) -- at
        # peak=90%, stage 2's trigger is (1.90)*(1-0.45)-1 = 0.045,
        # comfortably above the 3% floor
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.90"), D("0.045"), 2, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (2, 3, "PARTIAL")
        assert threshold == D("0.045")

    def test_stage_progression_only_advances_one_stage_per_tick(self):
        # peak=80% so none of the staged triggers are anywhere near the 3%
        # floor; current=20% clears stage 0's trigger (44%) AND stage 1's
        # trigger (26%), but stage 0 is still the earliest un-fired one --
        # only it fires this tick, not stage 1
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.80"), D("0.20"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_stage_progression_picks_up_the_next_stage_once_the_prior_one_fired(self):
        # same current_rate (20%) and peak (80%), but stage 0 already fired
        # on a previous tick -- this tick advances to stage 1 (trigger 26%)
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.80"), D("0.20"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 2, "PARTIAL")

    def test_stage_progression_does_not_skip_ahead_to_a_not_yet_current_stage(self):
        # same current_rate (20%) and peak (80%), stages 0 and 1 already
        # fired -- stage 2's own trigger is (1.80)*(1-0.45)-1 = -1%, which
        # 20% does NOT clear, so no action even though 20% cleared stages
        # 0/1's shallower bars long ago
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.80"), D("0.20"), 2, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (2, 2, None)

    def test_full_exit_floor_fires_even_at_a_gap_straight_past_every_stage(self):
        # peak=80%, current crashes straight to 2% (below the 3% floor) --
        # FULL fires immediately regardless of sell_stage, never PARTIAL
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.80"), D("0.02"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 3, "FULL")

    def test_full_exit_floor_overrides_a_would_be_partial_for_a_low_enough_peak(self):
        # peak=35%: stage 0's trigger is (1.35)*(1-0.15)-1 = 14.75%, above
        # the 3% floor. current=2% clears BOTH the floor (2% < 3%) and
        # stage 0's trigger (2% <= 14.75%) -- the floor check runs first
        # and wins, so this is FULL, not a stage-0 PARTIAL
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.35"), D("0.02"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 3, "FULL")

    def test_full_exit_floor_fires_right_at_activation_with_no_prior_partial(self):
        # peak just reached PEAK_ACTIVATION_RATE (15%) and current is
        # already below the 3% floor -- FULL fires even though sell_stage
        # is still 0 and no partial sell has ever happened for this symbol
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.15"), D("0.025"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 3, "FULL")

    def test_no_double_fire_once_stage_already_reached(self):
        # already at stage 1 (stage-0 partial already sold), current (32%)
        # clears neither stage 1's trigger (-2%) nor the 3% floor -> no
        # repeat action
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.32"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 1, None)

    def test_new_higher_peak_resets_stage_and_restarts_cycle(self):
        # stage was 1 (stage-0 partial already sold) at the old 40% peak;
        # this tick makes a brand-new higher peak (45%) -- the cycle
        # restarts from stage 0 relative to the new peak
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.45"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("0.2325")  # next trigger relative to the new peak: (1.45)*(1-0.15)-1

    def test_below_peak_activation_never_fires(self):
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.05"), D("0.01"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("-1.00")

    def test_allow_peak_update_false_freezes_peak_below_target(self):
        # current_rate (25%) is well above the old peak (10%), but with
        # allow_peak_update=False (the per-tick call) the peak must NOT
        # chase it -- only the once/day daily_snapshot call may raise it
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.10"), D("0.25"), 0, D("50000"), just_reached_target=False, allow_peak_update=False,
        )
        assert peak == D("0.10")
        assert threshold == D("-1.00")
        assert (stage, next_stage, action) == (0, 0, None)

    def test_allow_peak_update_false_freezes_peak_at_or_above_target(self):
        # same freeze in the normal (at/above target) branch -- peak stays
        # at the old value (20%) even though current_rate (35%) would
        # otherwise raise it; threshold is still computed from the frozen
        # peak: (1.20)*(1-0.15)-1 = 0.02, clamped up to the 3% floor
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.20"), D("0.35"), 0, D("150000"), just_reached_target=False, allow_peak_update=False,
        )
        assert peak == D("0.20")
        assert threshold == D("0.03")
        assert (stage, next_stage, action) == (0, 0, None)

    def test_allow_peak_update_false_still_evaluates_drawdown_against_frozen_peak(self):
        # peak stays frozen at 40% (allow_peak_update=False), but the
        # drawdown/action check still runs every tick against that frozen
        # peak -- a stage can still fire intraday without the peak itself
        # moving
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.10"), 0, D("150000"), just_reached_target=False, allow_peak_update=False,
        )
        assert peak == D("0.40")  # unchanged, current_rate (10%) is below peak anyway
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_allow_peak_update_false_does_not_block_hard_resets(self):
        # just_reached_target still resets peak to current_rate immediately
        # in real time, regardless of allow_peak_update -- only the routine
        # "chase current_rate upward" step is deferred to once/day
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 0, D("100000"), just_reached_target=True, allow_peak_update=False,
        )
        assert peak == D("0.30")
        assert threshold == D("0.105")  # next trigger: (1.30)*(1-0.15)-1
        assert (stage, next_stage, action) == (0, 0, None)

    def test_external_buy_detected_resets_peak_to_current_rate(self):
        # a manual top-up diluted the rate from a 40% peak down to 12% --
        # peak restarts fresh from the post-buy rate, no action fires on
        # this same tick (drawdown is 0 against the just-reset peak)
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.12"), 0, D("150000"), just_reached_target=False, external_buy_detected=True,
        )
        assert peak == D("0.12")
        assert (stage, next_stage, action) == (0, 0, None)

    def test_external_buy_detected_resets_sell_stage_regardless_of_prior_stage(self):
        # sell_stage was already 2 (two partial sells fired) -- an external
        # buy resets it to 0 unconditionally, unlike every other path in
        # this function which leaves an already-fired stage alone
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.12"), 2, D("150000"), just_reached_target=False, external_buy_detected=True,
        )
        assert (stage, next_stage, action) == (0, 0, None)

    def test_external_buy_detected_ignores_below_target_freeze(self):
        # purchase_amount_krw is still well below the 100k DCA target (which
        # would normally pin threshold at the inert -100% default) -- an
        # external buy overrides that gate too
        _, threshold, _, _, _ = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.05"), D("0.35"), 0, D("20000"), just_reached_target=False, external_buy_detected=True,
        )
        assert threshold == D("0.1475")  # next trigger: (1.35)*(1-0.15)-1, new_peak=0.35

    def test_external_buy_detected_preserves_active_status_despite_diluted_peak(self):
        # was already active (old peak 25% >= PEAK_ACTIVATION_RATE) before
        # this buy -- even though the diluted post-buy rate (8%) falls below
        # PEAK_ACTIVATION_RATE, take-profit protection must not silently
        # switch off; threshold is still a real computed value
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.25"), D("0.08"), 1, D("150000"), just_reached_target=False, external_buy_detected=True,
        )
        assert peak == D("0.08")
        assert threshold == D("0.03")  # raw trigger (1.08)*(1-0.15)-1=-0.082, clamped to the floor
        assert (stage, next_stage, action) == (0, 0, None)

    def test_external_buy_detected_stays_inert_when_never_active_and_still_below_activation(self):
        # neither the old peak (5%) nor the diluted new rate (3%) ever
        # reached PEAK_ACTIVATION_RATE -- threshold stays pinned inert
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.05"), D("0.03"), 0, D("150000"), just_reached_target=False, external_buy_detected=True,
        )
        assert peak == D("0.03")
        assert threshold == D("-1.00")
        assert (stage, next_stage, action) == (0, 0, None)


class TestDailyBuyAmount:
    def test_buys_while_under_target_regardless_of_rate(self):
        assert strategy.daily_buy_amount_krw(D("0"), D("-0.50")) == D("5000")
        assert strategy.daily_buy_amount_krw(D("95000"), D("-0.90")) == D("5000")

    def test_pauses_at_target_when_below_resume_rate(self):
        assert strategy.daily_buy_amount_krw(D("100000"), D("0.05")) is None
        assert strategy.daily_buy_amount_krw(D("100000"), D("0.09")) is None

    def test_resumes_at_target_once_rate_reaches_15pct(self):
        assert strategy.daily_buy_amount_krw(D("100000"), D("0.15")) == D("5000")

    def test_resumes_past_target_once_profitable(self):
        assert strategy.daily_buy_amount_krw(D("150000"), D("0.16")) == D("5000")


class TestFractionalEntryAllowed:
    def test_buys_while_under_target_regardless_of_rate_or_peak(self):
        assert strategy.fractional_entry_allowed(D("0"), D("-0.50"), D("0"), D("0")) is True
        assert strategy.fractional_entry_allowed(D("95000"), D("-0.90"), D("0.40"), D("-0.30")) is True

    def test_at_target_below_30pct_peak_never_bought_uses_flat_15pct(self):
        # peak below the 30% cutoff -> ratchet branch, but last_buy_rate
        # defaults to 0 -> floor = max(15%, 0%+3%=3%) = 15%
        assert strategy.fractional_entry_allowed(D("100000"), D("0.14"), D("0.15"), D("0")) is False
        assert strategy.fractional_entry_allowed(D("100000"), D("0.15"), D("0.15"), D("0")) is True

    def test_at_target_below_30pct_peak_ratchets_off_last_buy_rate(self):
        # last buy settled at 20% -> floor = max(15%, 20%+3%) = 23%
        assert strategy.fractional_entry_allowed(D("150000"), D("0.22"), D("0.15"), D("0.20")) is False
        assert strategy.fractional_entry_allowed(D("150000"), D("0.23"), D("0.15"), D("0.20")) is True

    def test_at_target_below_30pct_peak_mid_last_buy_rate_lands_between_flat_floor_and_ratchet(self):
        # last buy settled at 13% -> floor = max(15%, 13%+3%=16%) = 16%
        assert strategy.fractional_entry_allowed(D("150000"), D("0.15"), D("0.20"), D("0.13")) is False
        assert strategy.fractional_entry_allowed(D("150000"), D("0.16"), D("0.20"), D("0.13")) is True

    def test_at_target_below_30pct_peak_negative_last_buy_rate_still_uses_flat_15pct(self):
        # last buy settled at a loss (-30%) -- max(15%, -30%+3%=-27%) = 15%
        assert strategy.fractional_entry_allowed(D("150000"), D("0.14"), D("0.20"), D("-0.30")) is False
        assert strategy.fractional_entry_allowed(D("150000"), D("0.15"), D("0.20"), D("-0.30")) is True

    def test_at_target_at_or_above_30pct_peak_ignores_ratchet_uses_flat_15pct(self):
        # peak >= LIQUIDATION_STAGE_1_MIN_PEAK -> converges back to the same
        # flat 15% rule as daily_buy_amount_krw, regardless of how high
        # last_buy_rate is
        assert strategy.fractional_entry_allowed(D("150000"), D("0.14"), D("0.30"), D("0.50")) is False
        assert strategy.fractional_entry_allowed(D("150000"), D("0.15"), D("0.30"), D("0.50")) is True

    def test_peak_just_below_30pct_still_uses_ratchet(self):
        # boundary check: 29% peak is strictly below the 30% cutoff, so the
        # ratchet (off a 20% last buy rate -> floor 23%) still applies
        assert strategy.fractional_entry_allowed(D("150000"), D("0.22"), D("0.29"), D("0.20")) is False
        assert strategy.fractional_entry_allowed(D("150000"), D("0.23"), D("0.29"), D("0.20")) is True


class TestNonfractionalIsDcaGraceWindow:
    def test_true_below_target_and_below_ceiling(self):
        assert strategy.nonfractional_is_dca_grace_window(D("50000"), D("55000")) is True

    def test_false_once_projected_reaches_ceiling(self):
        assert strategy.nonfractional_is_dca_grace_window(D("95000"), D("135000")) is False

    def test_false_once_current_at_or_above_target(self):
        assert strategy.nonfractional_is_dca_grace_window(D("100000"), D("110000")) is False


class TestNonfractionalEntryAllowed:
    def test_buys_regardless_of_rate_within_dca_grace_window(self):
        # current 50,000 KRW (<100k), projected 55,000 KRW (<130k) -> allowed even at a loss
        assert strategy.nonfractional_entry_allowed(D("50000"), D("55000"), D("-0.50"), D("-1.00")) is True

    def test_below_target_at_or_above_ceiling_uses_flat_15pct_ignoring_ratchet(self):
        # current 95,000 KRW (<100k, so not yet at the "그 외" branch) but this buy pushes
        # projected to 135,000 (>=130k ceiling) -> flat 15% floor, last fallback
        # buy's projected_rate (20%) ignored
        assert strategy.nonfractional_entry_allowed(D("95000"), D("135000"), D("0.14"), D("0.20")) is False
        assert strategy.nonfractional_entry_allowed(D("95000"), D("135000"), D("0.15"), D("0.20")) is True

    def test_at_or_above_target_never_bought_uses_flat_15pct(self):
        # current purchase already >= 100k, no buy has ever fired for this
        # symbol (last_buy_rate defaults to 0) -> floor = max(15%, 0%+3%=3%)
        # = 15% (the +3% step only matters once last_buy_rate is already
        # at/above 12%)
        assert strategy.nonfractional_entry_allowed(D("100000"), D("110000"), D("0.14"), D("0")) is False
        assert strategy.nonfractional_entry_allowed(D("100000"), D("110000"), D("0.15"), D("0")) is True

    def test_at_or_above_target_ratchets_off_last_buy_rate(self):
        # last buy for this symbol settled at 20% -> floor = max(15%, 20%+3%) = 23%,
        # regardless of what the current take-profit threshold happens to be
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.22"), D("0.20")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.23"), D("0.20")) is True

    def test_at_or_above_target_mid_last_buy_rate_lands_between_flat_floor_and_ratchet(self):
        # last buy settled at 13% (below PEAK_ACTIVATION_RATE, but above the
        # 12% breakeven where +3% starts to matter) -> floor =
        # max(15%, 13%+3%=16%) = 16%, strictly above the flat 15% floor
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.15"), D("0.13")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.16"), D("0.13")) is True

    def test_at_or_above_target_negative_last_buy_rate_still_uses_flat_15pct(self):
        # last buy settled at a loss (-30%) -- max(15%, -30%+3%=-27%) = 15%,
        # the ratchet step never drops the floor below the flat PEAK_ACTIVATION_RATE
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.14"), D("-0.30")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.15"), D("-0.30")) is True
