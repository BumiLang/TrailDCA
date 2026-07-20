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
        # max(peak*30%, rate/2) = max(0.03, 0.05) = 0.05
        assert threshold == D("0.05")

    def test_threshold_uses_peak_times_30pct_when_higher(self):
        # peak=60%, current rate dropped to 30% -> max(0.60*0.30, 0.30/2)=max(0.18,0.15)=0.18
        peak, threshold = strategy.update_peak_and_threshold(D("0.60"), D("0.30"), D("0.10"))
        assert peak == D("0.60")
        assert threshold == D("0.18")

    def test_threshold_uses_half_current_rate_when_higher(self):
        # peak=15%, current rate spikes to 50% -> max(0.50*0.30, 0.50/2)=max(0.15,0.25)=0.25
        peak, threshold = strategy.update_peak_and_threshold(D("0.15"), D("0.50"), D("0.05"))
        assert peak == D("0.50")
        assert threshold == D("0.25")

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


class TestFractionalDailyBuy:
    def test_buys_while_under_target_regardless_of_rate(self):
        assert strategy.fractional_daily_buy_amount_krw(D("0"), D("-0.50")) == D("5000")
        assert strategy.fractional_daily_buy_amount_krw(D("95000"), D("-0.90")) == D("5000")

    def test_pauses_at_target_when_unprofitable(self):
        assert strategy.fractional_daily_buy_amount_krw(D("100000"), D("0.05")) is None
        assert strategy.fractional_daily_buy_amount_krw(D("100000"), D("0.10")) is None  # not strictly > 10%

    def test_resumes_past_target_once_profitable(self):
        assert strategy.fractional_daily_buy_amount_krw(D("150000"), D("0.11")) == D("5000")


class TestNonfractionalBuy:
    def test_never_buys_from_zero(self):
        assert strategy.nonfractional_should_buy(D("0"), D("0.99")) is False

    def test_requires_10pct_for_first_add(self):
        assert strategy.nonfractional_should_buy(D("1"), D("0.09")) is False
        assert strategy.nonfractional_should_buy(D("1"), D("0.10")) is True

    def test_scales_5pct_per_additional_share(self):
        # held 2 shares -> requires 15%
        assert strategy.nonfractional_should_buy(D("2"), D("0.14")) is False
        assert strategy.nonfractional_should_buy(D("2"), D("0.15")) is True

    def test_peak_reassigned_after_buy(self):
        assert strategy.peak_after_nonfractional_buy(D("0.12")) == D("0.12")
