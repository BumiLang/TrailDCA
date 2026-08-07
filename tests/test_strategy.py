from decimal import Decimal

from src import strategy


def D(s: str) -> Decimal:
    return Decimal(s)


class TestNextLiquidationTriggerRate:
    def test_stage_0_uses_20pct_drawdown(self):
        # peak=40% -> 0.40*(1-0.20) = 0.32
        assert strategy.next_liquidation_trigger_rate(D("0.40"), 0) == D("0.32")

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

    def test_20_to_30pct_peak_skips_20pct_drawdown_stage(self):
        # peak=25% is below LIQUIDATION_STAGE_1_MIN_PEAK (30%) but at/above
        # LIQUIDATION_STAGE_2_MIN_PEAK (20%) -- the 20%-drawdown stage is
        # skipped, next trigger is the 40%-drawdown level
        assert strategy.next_liquidation_trigger_rate(D("0.25"), 0) == D("0.25") * (D("1") - D("0.40"))

    def test_at_30pct_peak_all_stages_eligible(self):
        # peak=30% exactly meets LIQUIDATION_STAGE_1_MIN_PEAK -> back to the
        # normal 20%-drawdown next trigger
        assert strategy.next_liquidation_trigger_rate(D("0.30"), 0) == D("0.30") * (D("1") - D("0.20"))

    def test_low_peak_stage_1_already_done_still_skips_to_stage_3(self):
        # even if sell_stage somehow indicates stage 1 already fired (e.g.
        # peak dropped after an earlier high), a peak too low for stage 2
        # skips straight to the 50%-drawdown stop
        assert strategy.next_liquidation_trigger_rate(D("0.15"), 1) == D("0.15") * (D("1") - D("0.50"))


class TestUpdatePeakThresholdAndSellStageGated:
    def test_below_target_no_action_and_stage_frozen_at_zero(self):
        # would otherwise be a 25% drawdown from a 40% peak (past the 20%
        # stage-1 trigger), but purchase amount hasn't reached the 100k DCA
        # target yet -- no action, stage stays 0, threshold pinned inert
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 0, D("99999"), just_reached_target=False
        )
        assert peak == D("0.40")
        assert threshold == D("-1.00")
        assert stage == 0
        assert next_stage == 0
        assert action is None

    def test_crossing_target_resets_peak_stage_and_threshold(self):
        # peak/threshold restart fresh from the current rate (30%); stage 0
        # -> next trigger is the 20%-drawdown level: 0.30*0.8 = 0.24
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.30"), 2, D("100000"), just_reached_target=True
        )
        assert peak == D("0.30")
        assert threshold == D("0.24")
        assert stage == 0
        assert next_stage == 0
        assert action is None

    def test_crossing_target_below_activation_stays_inert(self):
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.03"), 2, D("100000"), just_reached_target=True
        )
        assert peak == D("0.03")
        assert threshold == D("-1.00")
        assert (stage, next_stage, action) == (0, 0, None)

    def test_no_action_below_20pct_drawdown(self):
        # peak=40%, current=35% -> drawdown 12.5%, below the 20% stage-1 bar
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.35"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("0.32")  # next trigger: 0.40*(1-0.20)

    def test_stage_1_partial_sell_at_20pct_drawdown(self):
        # peak=40%, current=32% -> drawdown exactly 20%
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.32"), 0, D("150000"), just_reached_target=False
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
        # un-fired one whose bar is cleared (stage 1, the 20% bar), not the
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
        # already at stage 1 (20% partial sold), current drawdown still only 20% -> no repeat action
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.32"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (1, 1, None)

    def test_new_higher_peak_resets_stage_and_restarts_cycle(self):
        # stage was 1 (20% partial already sold) at the old 40% peak; this
        # tick makes a brand-new higher peak (45%) -- the cycle restarts
        # from stage 0 relative to the new peak
        peak, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.40"), D("0.45"), 1, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("0.36")  # next trigger relative to the new peak: 0.45*(1-0.20)

    def test_below_peak_activation_never_fires(self):
        _, threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.05"), D("0.01"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)
        assert threshold == D("-1.00")

    def test_low_peak_below_20pct_only_50pct_drawdown_stage_fires(self):
        # peak=15%: 20%-drawdown level would be 15%*0.8=12%, cleared by
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
        # peak=25% (below the 30% bar for the 20%-drawdown stage, at/above
        # the 20% bar for the 40%-drawdown stage) -- current=15% is a 40%
        # drawdown, well past the (ineligible) 20% bar too, but stage 1
        # never fires here since it's not eligible at this peak
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.25"), D("0.15"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 2, "PARTIAL")

    def test_mid_peak_20_to_30pct_never_fires_stage_1_even_at_shallow_drawdown(self):
        # peak=25%, current=21% -> only a 16% drawdown, short of even the
        # (ineligible) 20% bar -- no action, same as the fully-eligible case
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.25"), D("0.21"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 0, None)

    def test_peak_at_30pct_stage_1_eligible_again(self):
        # peak=30% exactly meets LIQUIDATION_STAGE_1_MIN_PEAK -> the normal
        # (fully eligible) staging resumes
        _, _, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            D("0.30"), D("0.24"), 0, D("150000"), just_reached_target=False
        )
        assert (stage, next_stage, action) == (0, 1, "PARTIAL")


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


class TestPeakAfterShareBuy:
    def test_peak_reassigned_after_buy(self):
        assert strategy.peak_after_share_buy(D("0.12")) == D("0.12")


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

    def test_below_target_at_or_above_ceiling_uses_flat_10pct_ignoring_threshold(self):
        # current 95,000 KRW (<100k, so not yet at the "그 외" branch) but this buy pushes
        # projected to 135,000 (>=130k ceiling) -> flat 10% floor, threshold (17%) ignored
        assert strategy.nonfractional_entry_allowed(D("95000"), D("135000"), D("0.05"), D("0.17")) is False
        assert strategy.nonfractional_entry_allowed(D("95000"), D("135000"), D("0.10"), D("0.17")) is True

    def test_at_or_above_target_uses_threshold_regardless_of_projected_purchase(self):
        # current purchase already >= 100k -> always the max(10%, threshold) gate,
        # even though projected purchase (110k) hasn't reached the 130k ceiling
        assert strategy.nonfractional_entry_allowed(D("100000"), D("110000"), D("0.05"), D("-1.00")) is False
        assert strategy.nonfractional_entry_allowed(D("100000"), D("110000"), D("0.10"), D("-1.00")) is True

    def test_at_or_above_target_rate_gate_uses_threshold_when_higher_than_10pct(self):
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.16"), D("0.17")) is False
        assert strategy.nonfractional_entry_allowed(D("150000"), D("160000"), D("0.17"), D("0.17")) is True
