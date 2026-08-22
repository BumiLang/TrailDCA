from decimal import Decimal

from src import strategy


def D(s: str) -> Decimal:
    return Decimal(s)


class TestNextLiquidationTriggerRate:
    def test_stage_0_uses_30pct_drawdown(self):
        # peak=40% -> 0.40*(1-0.30) = 0.28
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 0) == D("0.28")

    def test_stage_1_uses_40pct_drawdown(self):
        # peak=40% -> 0.40*(1-0.40) = 0.24
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 1) == D("0.24")

    def test_stage_2_uses_50pct_drawdown(self):
        # peak=40% -> 0.40*(1-0.50) = 0.20
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 2) == D("0.20")

    def test_below_20pct_peak_skips_straight_to_50pct_drawdown(self):
        # peak=15% is below LIQUIDATION_STAGE_2_MIN_PEAK (20%), so neither
        # the 20% nor 40% drawdown stage is eligible -- only the 50% stop
        assert strategy.next_liquidation_trigger_rate(D("0.15"), 0) == D("0.15") * (D("1") - D("0.50"))

    def test_20_to_30pct_peak_skips_30pct_drawdown_stage(self):
        # peak=25% is below LIQUIDATION_STAGE_1_MIN_PEAK (30%) but at/above
        # LIQUIDATION_STAGE_2_MIN_PEAK (20%) -- the 30%-drawdown stage is
        # skipped, next trigger is the 40%-drawdown level
        assert strategy.next_liquidation_trigger_rate(D("0.25"), 0) == D("0.25") * (D("1") - D("0.40"))

    def test_at_30pct_peak_all_stages_eligible(self):
        # peak=30% exactly meets LIQUIDATION_STAGE_1_MIN_PEAK -> back to the
        # normal 30%-drawdown next trigger
        assert strategy.next_liquidation_trigger_rate(D("0.30"), 0) == D("0.30") * (D("1") - D("0.30"))

    def test_low_peak_stage_1_already_done_still_skips_to_stage_3(self):
        # even if sell_stage somehow indicates stage 1 already fired (e.g.
        # peak dropped after an earlier high), a peak too low for stage 2
        # skips straight to the 50%-drawdown stop
        assert strategy.next_liquidation_trigger_rate(D("0.15"), 1) == D("0.15") * (D("1") - D("0.50"))


class TestUpdatePeakThresholdAndSellStageGated:
    def test_below_target_no_action_and_stage_frozen_at_zero(self):
        # would otherwise be a 50% drawdown from a 40% peak (past all three
        # stage triggers), but purchase amount hasn't reached the 100k DCA
        # target yet -- no action, stage stays 0 (nothing to preserve yet),
        # threshold pinned inert
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
        # here (0.30 < peak 0.40) so stage stays 2, and 0.30 doesn't clear
        # the stage-3 (50%-drawdown) bar (0.40*0.5=0.20) so no action, but
        # the threshold is a REAL computed value, not the inert -100%
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 2, D("75000"), just_reached_target=False
        )
        assert peak == D("0.40")
        assert threshold == D("0.20")  # next trigger: 0.40*(1-0.50), stage 2 -> only the 50% stop remains
        assert stage == 2
        assert next_stage == 2
        assert action is None

    def test_below_target_partial_sell_can_still_fire_once_a_stage_has_fired(self):
        # sell_stage=1 already fired once; purchase_amount_krw is back
        # below the 100k target after that partial sell, but stage-2
        # eligibility (40% drawdown from peak) still gets evaluated and
        # fires normally instead of being gated off
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.50"), D("0.29"), 1, D("80000"), just_reached_target=False
        )
        assert peak == D("0.50")
        assert threshold == D("0.30")  # next trigger: 0.50*(1-0.40)
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
        assert threshold == D("0.20")
        assert stage == 2
        assert next_stage == 2
        assert action is None

    def test_crossing_target_full_exit_can_fire_once_a_stage_has_fired(self):
        # deep drawdown (current 3% vs peak 40%, well past the 50% stop)
        # on the exact crossing tick -- with sell_stage already at 2, this
        # is no longer gated off or treated as an inert "just reached
        # target" reset; the FULL exit fires like any other tick
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.03"), 2, D("100000"), just_reached_target=True
        )
        assert peak == D("0.40")
        assert threshold == D("0.20")
        assert (stage, next_stage, action) == (2, 3, "FULL")

    def test_no_action_below_30pct_drawdown(self):
        # peak=40%, current=32% -> drawdown 20%, below the 30% stage-1 bar
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.32"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("0.28")  # next trigger: 0.40*(1-0.30)

    def test_stage_1_partial_sell_at_30pct_drawdown(self):
        # peak=40%, current=28% -> drawdown exactly 30%
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.28"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_stage_2_partial_sell_at_40pct_drawdown_when_stage_1_already_done(self):
        # peak=40%, current=24% -> drawdown exactly 40%; threshold (still
        # computed from the pre-action stage=1) is the 40%-drawdown level
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.24"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 2, "PARTIAL")
        assert threshold == D("0.24")  # next trigger: 0.40*(1-0.40)

    def test_gap_straight_to_45pct_drawdown_fires_stage_1_first_not_stage_2(self):
        # peak=40%, current=22% -> drawdown 45%, past the 40% stage-2 bar
        # and short of the 50% full-exit bar; stage still 0 (nothing fired
        # yet) -- only one stage fires per tick, and it's the earliest
        # un-fired one whose bar is cleared (stage 1, the 30% bar), not the
        # deepest one that's also cleared
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.22"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_gap_continues_to_stage_2_on_the_next_tick_if_still_cleared(self):
        # same 45% drawdown, but now stage 1 already fired on the previous
        # tick -- this tick picks up stage 2 (the 40% bar), still short of
        # the 50% full-exit bar
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.22"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 2, "PARTIAL")

    def test_gap_all_the_way_to_75pct_drawdown_still_only_fires_stage_1_first(self):
        # even a crash straight past all three bars only fires the
        # earliest un-fired stage this tick, never jumps to FULL directly
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.10"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_full_exit_at_50pct_drawdown(self):
        # peak=40%, current=20% -> drawdown exactly 50%
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.20"), 2, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (2, 3, "FULL")

    def test_no_double_fire_once_stage_already_reached(self):
        # already at stage 1 (30% partial sold), current drawdown still only 20% -> no repeat action
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.32"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 1, None)

    def test_new_higher_peak_resets_stage_and_restarts_cycle(self):
        # stage was 1 (30% partial already sold) at the old 40% peak; this
        # tick makes a brand-new higher peak (45%) -- the cycle restarts
        # from stage 0 relative to the new peak
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.45"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("0.315")  # next trigger relative to the new peak: 0.45*(1-0.30)

    def test_below_peak_activation_never_fires(self):
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.05"), D("0.01"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("-1.00")

    def test_low_peak_below_20pct_only_50pct_drawdown_stage_fires(self):
        # peak=15%: 30%-drawdown level would be 15%*0.7=10.5%, cleared by
        # current=10%, but the stage is ineligible below a 20% peak -- no
        # action fires here
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.15"), D("0.10"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)

    def test_low_peak_50pct_drawdown_still_fires_as_full_exit(self):
        # peak=15%, current=7.5% -> exactly 50% drawdown -- the 50% stop
        # has no peak minimum, so it still fires even though peak never
        # reached the 20%/30% eligibility bars for the earlier stages
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.15"), D("0.075"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 3, "FULL")

    def test_mid_peak_20_to_30pct_skips_stage_1_fires_stage_2(self):
        # peak=25% (below the 30% bar for the 30%-drawdown stage, at/above
        # the 20% bar for the 40%-drawdown stage) -- current=15% is a 40%
        # drawdown, well past the (ineligible) 30% bar too, but stage 1
        # never fires here since it's not eligible at this peak
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.25"), D("0.15"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 2, "PARTIAL")

    def test_mid_peak_20_to_30pct_never_fires_stage_1_even_at_shallow_drawdown(self):
        # peak=25%, current=21% -> only a 16% drawdown, short of even the
        # (ineligible) 30% bar -- no action, same as the fully-eligible case
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.25"), D("0.21"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)

    def test_peak_at_30pct_stage_1_eligible_again(self):
        # peak=30% exactly meets LIQUIDATION_STAGE_1_MIN_PEAK -> the normal
        # (fully eligible) staging resumes; drawdown exactly 30%
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.30"), D("0.21"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

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
        # peak (stage 0, peak>=20% -> only the 40%/50% stages remain
        # eligible): 0.20*(1-0.40) = 0.12
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.20"), D("0.35"), 0, D("150000"), just_reached_target=False, allow_peak_update=False,
        )
        assert peak == D("0.20")
        assert threshold == D("0.12")
        assert (stage, next_stage, action) == (0, 0, None)

    def test_allow_peak_update_false_still_evaluates_drawdown_against_frozen_peak(self):
        # peak stays frozen at 40% (allow_peak_update=False), but the
        # drawdown/action check still runs every tick against that frozen
        # peak -- a stage can still fire intraday without the peak itself
        # moving
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.28"), 0, D("150000"), just_reached_target=False, allow_peak_update=False,
        )
        assert peak == D("0.40")  # unchanged, current_rate (28%) is below peak anyway
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")

    def test_allow_peak_update_false_does_not_block_hard_resets(self):
        # just_reached_target still resets peak to current_rate immediately
        # in real time, regardless of allow_peak_update -- only the routine
        # "chase current_rate upward" step is deferred to once/day
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 0, D("100000"), just_reached_target=True, allow_peak_update=False,
        )
        assert peak == D("0.30")
        assert threshold == D("0.21")  # next trigger: 0.30*(1-0.30)
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
        assert threshold == D("0.245")  # next trigger: 0.35*(1-0.30), new_peak=0.35 clears the stage-1 min-peak bar

    def test_external_buy_detected_preserves_active_status_despite_diluted_peak(self):
        # was already active (old peak 25% >= PEAK_ACTIVATION_RATE) before
        # this buy -- even though the diluted post-buy rate (8%) falls below
        # PEAK_ACTIVATION_RATE, take-profit protection must not silently
        # switch off; threshold is still a real computed value
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.25"), D("0.08"), 1, D("150000"), just_reached_target=False, external_buy_detected=True,
        )
        assert peak == D("0.08")
        assert threshold == D("0.04")  # next trigger: 0.08*(1-0.50), new_peak below both stage-1/2 min-peak bars
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

    def test_resumes_at_target_once_rate_reaches_10pct(self):
        assert strategy.daily_buy_amount_krw(D("100000"), D("0.10")) == D("5000")

    def test_resumes_past_target_once_profitable(self):
        assert strategy.daily_buy_amount_krw(D("150000"), D("0.11")) == D("5000")


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

    def test_below_target_at_or_above_ceiling_uses_flat_10pct_ignoring_ratchet(self):
        # current 95,000 KRW (<100k, so not yet at the "그 외" branch) but this buy pushes
        # projected to 135,000 (>=130k ceiling) -> flat 10% floor, last fallback
        # buy's projected_rate (20%) ignored
        assert strategy.nonfractional_entry_allowed(D("95000"), D("135000"), D("0.05"), D("0.20")) is False
        assert strategy.nonfractional_entry_allowed(D("95000"), D("135000"), D("0.10"), D("0.20")) is True

    def test_at_or_above_target_never_bought_via_fallback_uses_flat_10pct(self):
        # current purchase already >= 100k, no fallback buy has ever fired
        # for this symbol (last_fallback_buy_rate defaults to 0)
        # -> floor = max(10%, 0%+3%=3%) = 10% (the +3% step only matters
        # once last_fallback_buy_rate is already at/above 7%)
        assert strategy.nonfractional_entry_allowed(D("100000"), D("110000"), D("0.09"), D("0")) is False
        assert strategy.nonfractional_entry_allowed(D("100000"), D("110000"), D("0.10"), D("0")) is True

    def test_at_or_above_target_ratchets_off_last_fallback_buy_rate(self):
        # last fallback buy for this symbol projected 20% -> floor = max(10%, 20%+3%) = 23%,
        # regardless of what the current take-profit threshold happens to be
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.22"), D("0.20")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.23"), D("0.20")) is True

    def test_at_or_above_target_mid_last_fallback_buy_rate_lands_between_flat_floor_and_ratchet(self):
        # last fallback buy projected 8% (below PEAK_ACTIVATION_RATE, but
        # above the 7% breakeven where +3% starts to matter) -> floor =
        # max(10%, 8%+3%=11%) = 11%, strictly above the flat 10% floor
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.10"), D("0.08")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.11"), D("0.08")) is True

    def test_at_or_above_target_negative_last_fallback_buy_rate_still_uses_flat_10pct(self):
        # last fallback buy projected a loss (-30%) -- max(10%, -30%+3%=-27%) = 10%,
        # the ratchet step never drops the floor below the flat PEAK_ACTIVATION_RATE
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.09"), D("-0.30")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.10"), D("-0.30")) is True
