from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")
US_EASTERN = ZoneInfo("America/New_York")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "state.json"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    toss_client_id: str
    toss_client_secret: str
    toss_account_seq: str | None

    google_service_account_file: str
    google_sheet_id: str
    google_sheet_tab: str

    live_trading: bool
    log_level: str

    @staticmethod
    def load() -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")

        client_id = os.environ.get("TOSS_CLIENT_ID", "").strip()
        client_secret = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError(
                "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET must be set in .env"
            )

        sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID must be set in .env")

        return Config(
            toss_client_id=client_id,
            toss_client_secret=client_secret,
            toss_account_seq=(os.environ.get("TOSS_ACCOUNT_SEQ") or "").strip() or None,
            google_service_account_file=os.environ.get(
                "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
            ),
            google_sheet_id=sheet_id,
            google_sheet_tab=os.environ.get("GOOGLE_SHEET_TAB", "Sheet1"),
            live_trading=_bool(os.environ.get("LIVE_TRADING"), default=False),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )


# --- Strategy constants (from spec) ---
from decimal import Decimal as _Decimal

DAILY_BUY_KRW = _Decimal("5000")
DAILY_BUY_TARGET_KRW = _Decimal("100000")
DAILY_BUY_RESUME_RATE = _Decimal("0.15")  # profit rate must reach this to keep buying past target
DAILY_BUY_RETRY_SECONDS = 60  # throttle interval between buy attempts until one actually fills
# Non-fractional (whole-share) buys skip the entry-rate gate entirely while
# still below DAILY_BUY_TARGET_KRW *and* this buy wouldn't push cumulative
# purchase amount past this ceiling -- same "keep DCAing regardless of rate"
# spirit as the fractional path, with headroom above DAILY_BUY_TARGET_KRW
# since a single whole-share buy can jump past it in one step.
NONFRACTIONAL_DCA_CEILING_KRW = _Decimal("130000")
PEAK_ACTIVATION_RATE = _Decimal("0.15")
INITIAL_TAKE_PROFIT_THRESHOLD = _Decimal("-1.00")  # -100%, inert value before peak activates

# Once current_purchase_krw is already at/above DAILY_BUY_TARGET_KRW, each
# 1-share fallback buy (see nonfractional_entry_allowed) must project a rate
# at least this many points higher than the LAST fallback buy's projected
# rate did (or above PEAK_ACTIVATION_RATE if that's higher) -- a ratchet
# that only lets repeated 1-share buys through while the position is
# actually improving, not just standing still or drifting down.
NONFRACTIONAL_ENTRY_RATCHET_STEP = _Decimal("0.03")

# Staged trailing-stop liquidation, keyed off the drawdown of the *actual
# share price* from its peak (not a relative drawdown of the profit-rate
# number itself) -- since price = cost_basis * (1 + rate), a price drawdown
# of `d` off the peak price translates to a trigger profit-rate of
# (1 + peak) * (1 - d) - 1 (see strategy.next_liquidation_trigger_rate).
# All three stages are eligible as soon as PEAK_ACTIVATION_RATE is reached
# -- no separate per-stage minimum peak. Each stage sells its own
# LIQUIDATION_STAGE_*_SELL_FRACTION of the CURRENT holding (not the
# original position) -- 33% / 77% / 88% for stages 1/2/3 respectively, so
# all three firing in sequence leaves roughly 1.8% of the original position
# still held (0.67 * 0.23 * 0.12). A fresh (higher) peak restarts this
# staged cycle from scratch, so a partial sell doesn't block another one
# after a new high and pullback. Below PEAK_ACTIVATION_RATE nothing can
# fire.
LIQUIDATION_STAGE_1_DRAWDOWN = _Decimal("0.15")  # off peak PRICE; sell LIQUIDATION_STAGE_1_SELL_FRACTION of current holding
LIQUIDATION_STAGE_2_DRAWDOWN = _Decimal("0.30")  # off peak PRICE; sell LIQUIDATION_STAGE_2_SELL_FRACTION of current holding
LIQUIDATION_STAGE_3_DRAWDOWN = _Decimal("0.45")  # off peak PRICE; sell LIQUIDATION_STAGE_3_SELL_FRACTION of current holding
LIQUIDATION_STAGE_1_SELL_FRACTION = _Decimal("0.33")
LIQUIDATION_STAGE_2_SELL_FRACTION = _Decimal("0.77")
LIQUIDATION_STAGE_3_SELL_FRACTION = _Decimal("0.88")
# Absolute profit-rate stop-loss, independent of the staged drawdown ladder
# above and of sell_stage -- once activated (peak >= PEAK_ACTIVATION_RATE),
# the moment current_rate drops below this, sell everything regardless of
# which (if any) staged sell has fired. Because this is an absolute rate
# floor while the staged triggers are relative to peak PRICE, for a low
# peak this floor is often crossed before LIQUIDATION_STAGE_1_DRAWDOWN's
# trigger rate is -- e.g. at peak=15%, stage 1's trigger is
# 1.15*(1-0.15)-1 = -2.25%, well below this 3% floor, so current_rate
# crosses 3% (triggering FULL here) long before it would ever reach -2.25%
# (which would have triggered stage 1). This is expected, not a bug: it
# just means the staged ladder only meaningfully gates behavior once peak
# is high enough that its stage-1 trigger rate exceeds this floor (peak >=
# ~21.18% for the 15%/3% pairing above).
FULL_EXIT_PROFIT_RATE_FLOOR = _Decimal("0.03")

# Peak level above which the non-fractional (KR whole-share) and fractional
# (US amount-order) DCA buy ratchets fall back to the flat
# DAILY_BUY_RESUME_RATE floor instead of the last_buy_rate-based ratchet --
# unrelated to sell-stage eligibility (see strategy.fractional_entry_allowed
# / nonfractional_entry_allowed).
DCA_RATCHET_PEAK_CUTOFF = _Decimal("0.30")

# Buy/sell orders (DCA buy, take-profit liquidation) only start once a
# session has been open this long -- skips the volatile open, when peak/
# threshold bookkeeping keeps running but no order is actually placed yet.
# Configurable independently per market and per side.
KR_BUY_DELAY_AFTER_OPEN = _dt.timedelta(hours=1)
KR_SELL_DELAY_AFTER_OPEN = _dt.timedelta(minutes=35)
US_BUY_DELAY_AFTER_OPEN = _dt.timedelta(hours=1)
US_SELL_DELAY_AFTER_OPEN = _dt.timedelta(minutes=35)

TICK_SECONDS = 1
# Sheets API allows only 60 write requests/minute/user; batch_write() is one
# request regardless of how many cells it carries, so flushing on every 1s
# tick sits right at that limit and trips "Quota exceeded" under any jitter.
# Accumulate cell updates across ticks and flush at this cadence instead.
SHEET_FLUSH_INTERVAL_SECONDS = 1
DAILY_SNAPSHOT_HOUR_KST = 8
# 전략적용여부 is otherwise only refreshed from the sheet once/day (as part of
# the full daily_snapshot row reload) -- too slow to react to a manual
# toggle. Re-read just this column at this cadence instead, independent of
# the full row reload, so a manual edit takes effect within a minute.
STRATEGY_ENABLED_REFRESH_INTERVAL_SECONDS = 60
