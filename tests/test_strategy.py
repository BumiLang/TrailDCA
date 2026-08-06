from decimal import Decimal

from src import strategy


def D(s: str) -> Decimal:
    return Decimal(s)


class TestUpdatePeakAndThreshold:
    def test_peak_rises_with_current_rate(self):
        peak, threshold = strategy.update_peak_and_threshold(D("0.05"), D("0.08"), D("-1.00"))
        assert peak == D("0.08")
        # peak still below 10% activation -> threshold untouched
        assert threshold == D("-1.00")

    def test_threshold_activates_at_10pct_peak(self):
        # peak hits exactly 10%, current_rate also 10%
        peak, threshold = strategy.update_peak_and_threshold(D("0.08"), D("0.10"), D("-1.00"))
        assert peak == D("0.10")
        # peak*0.8 - 0.03 = 0.10*0.8-0.03 = 0.05 (threshold activates at exactly 5%)
        assert threshold == D("0.05")

    def test_threshold_scales_linearly_below_breakpoint(self):
        # peak=25% -> 0.25*0.8-0.03 = 0.17
        peak, threshold = strategy.update_peak_and_threshold(D("0.10"), D("0.25"), D("0.15"))
        assert peak == D("0.25")
        assert threshold == D("0.17")

    def test_threshold_switches_formula_at_30pct_breakpoint(self):
        # peak=30% -> high-slope branch: 0.30*0.7 = 0.21, matching the low
        # branch's value at the same point (0.30*0.8-0.03 = 0.21) -- no jump
        peak, threshold = strategy.update_peak_and_threshold(D("0.25"), D("0.30"), D("0.17"))
        assert peak == D("0.30")
        assert threshold == D("0.21")

    def test_threshold_uses_high_slope_above_breakpoint(self):
        # peak=90% -> 0.90*0.7 = 0.63
        peak, threshold = strategy.update_peak_and_threshold(D("0.50"), D("0.90"), D("0.42"))
        assert peak == D("0.90")
        assert threshold == D("0.63")

    def test_peak_never_decreases(self):
        peak, _ = strategy.update_peak_and_threshold(D("0.30"), D("0.10"), D("-0.10"))
        assert peak == D("0.30")


class TestUpdatePeakAndThresholdGated:
    def test_below_target_tracks_peak_but_freezes_threshold(self):
        # peak would normally activate a real threshold at 20%, but purchase
        # amount hasn't reached the 100k DCA target yet -- threshold stays
        # pinned at -100% even though peak keeps climbing in the background
        peak, threshold = strategy.update_peak_and_threshold_gated(
            D("0.10"), D("0.20"), D("-1.00"), D("50000"), just_reached_target=False
        )
        assert peak == D("0.20")
        assert threshold == D("-1.00")

    def test_crossing_target_resets_peak_to_current_rate(self):
        # peak silently accumulated to 20% during the DCA phase; this tick
        # is the one where purchase_amount_krw first reaches the target and
        # current_rate has dropped back to 10% -- without a reset this would
        # hand back an already-active 13% threshold above the current rate
        peak, threshold = strategy.update_peak_and_threshold_gated(
            D("0.20"), D("0.10"), D("-1.00"), D("100000"), just_reached_target=True
        )
        assert peak == D("0.10")
        # peak=10% -> 0.10*0.8-0.03 = 0.05
        assert threshold == D("0.05")

    def test_crossing_target_below_activation_stays_inert(self):
        peak, threshold = strategy.update_peak_and_threshold_gated(
            D("0.05"), D("0.03"), D("-1.00"), D("100000"), just_reached_target=True
        )
        assert peak == D("0.03")
        assert threshold == D("-1.00")

    def test_above_target_tracks_normally(self):
        peak, threshold = strategy.update_peak_and_threshold_gated(
            D("0.10"), D("0.25"), D("0.05"), D("150000"), just_reached_target=False
        )
        assert peak == D("0.25")
        assert threshold == D("0.17")


class TestShouldLiquidate:
    def test_no_liquidation_before_peak_activation(self):
        assert strategy.should_liquidate(D("0.05"), D("-0.50"), D("-1.00"), D("150000")) is False

    def test_liquidates_when_rate_drops_to_threshold(self):
        assert strategy.should_liquidate(D("0.60"), D("0.20"), D("0.20"), D("150000")) is True

    def test_no_liquidation_above_threshold(self):
        assert strategy.should_liquidate(D("0.60"), D("0.25"), D("0.20"), D("150000")) is False

    def test_no_liquidation_while_purchase_amount_below_target(self):
        # would otherwise liquidate, but purchase amount hasn't reached the 100k DCA target yet
        assert strategy.should_liquidate(D("0.60"), D("0.20"), D("0.20"), D("99999")) is False

    def test_liquidates_once_purchase_amount_reaches_target(self):
        assert strategy.should_liquidate(D("0.60"), D("0.20"), D("0.20"), D("100000")) is True


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
