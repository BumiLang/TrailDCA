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
DAILY_BUY_RESUME_RATE = _Decimal("0.10")  # profit rate must reach this to keep buying past target
DAILY_BUY_RETRY_SECONDS = 60  # throttle interval between buy attempts until one actually fills
# Non-fractional (whole-share) buys skip the entry-rate gate entirely while
# still below DAILY_BUY_TARGET_KRW *and* this buy wouldn't push cumulative
# purchase amount past this ceiling -- same "keep DCAing regardless of rate"
# spirit as the fractional path, with headroom above DAILY_BUY_TARGET_KRW
# since a single whole-share buy can jump past it in one step.
NONFRACTIONAL_DCA_CEILING_KRW = _Decimal("130000")
PEAK_ACTIVATION_RATE = _Decimal("0.10")
# take-profit threshold once peak has activated:
#   peak < TAKE_PROFIT_BREAKPOINT: threshold = peak * TAKE_PROFIT_LOW_SLOPE + TAKE_PROFIT_LOW_BASE
#   peak >= TAKE_PROFIT_BREAKPOINT: threshold = peak * TAKE_PROFIT_HIGH_SLOPE
# LOW_SLOPE/LOW_BASE are chosen so the low-branch line passes through
# (peak=PEAK_ACTIVATION_RATE, threshold=0.05) -- i.e. threshold is exactly 5%
# right when the threshold activates at 10% peak -- and through
# (peak=TAKE_PROFIT_BREAKPOINT, threshold=TAKE_PROFIT_BREAKPOINT * TAKE_PROFIT_HIGH_SLOPE)
# so it meets the high-branch line exactly at the breakpoint with no jump.
TAKE_PROFIT_BREAKPOINT = _Decimal("0.30")
TAKE_PROFIT_LOW_SLOPE = _Decimal("0.8")
TAKE_PROFIT_LOW_BASE = _Decimal("-0.03")
TAKE_PROFIT_HIGH_SLOPE = _Decimal("0.7")
INITIAL_TAKE_PROFIT_THRESHOLD = _Decimal("-1.00")  # -100%

# Buy/sell orders (DCA buy, take-profit liquidation) only start once a
# session has been open this long -- skips the volatile open, when peak/
# threshold bookkeeping keeps running but no order is actually placed yet.
# Configurable independently per market and per side.
KR_BUY_DELAY_AFTER_OPEN = _dt.timedelta(hours=1)
KR_SELL_DELAY_AFTER_OPEN = _dt.timedelta(minutes=5)
US_BUY_DELAY_AFTER_OPEN = _dt.timedelta(0)
US_SELL_DELAY_AFTER_OPEN = _dt.timedelta(0)

TICK_SECONDS = 1
# Sheets API allows only 60 write requests/minute/user; batch_write() is one
# request regardless of how many cells it carries, so flushing on every 1s
# tick sits right at that limit and trips "Quota exceeded" under any jitter.
# Accumulate cell updates across ticks and flush at this cadence instead.
SHEET_FLUSH_INTERVAL_SECONDS = 1
DAILY_SNAPSHOT_HOUR_KST = 8
