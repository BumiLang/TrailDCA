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
        # peak*0.75 - 0.035 = 0.10*0.75-0.035 = 0.04
        assert threshold == D("0.04")

    def test_threshold_scales_linearly_below_breakpoint(self):
        # peak=25% -> 0.25*0.75-0.035 = 0.1525
        peak, threshold = strategy.update_peak_and_threshold(D("0.10"), D("0.25"), D("0.15"))
        assert peak == D("0.25")
        assert threshold == D("0.1525")

    def test_threshold_switches_formula_at_30pct_breakpoint(self):
        # peak=30% -> high-slope branch: 0.30*0.7 = 0.21
        peak, threshold = strategy.update_peak_and_threshold(D("0.25"), D("0.30"), D("0.1525"))
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


class TestShouldLiquidate:
    def test_no_liquidation_before_peak_activation(self):
        assert strategy.should_liquidate(D("0.05"), D("-0.50"), D("-1.00")) is False

    def test_liquidates_when_rate_drops_to_threshold(self):
        assert strategy.should_liquidate(D("0.60"), D("0.20"), D("0.20")) is True

    def test_no_liquidation_above_threshold(self):
        assert strategy.should_liquidate(D("0.60"), D("0.25"), D("0.20")) is False


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
