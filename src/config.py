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
INITIAL_TAKE_PROFIT_THRESHOLD = _Decimal("-1.00")  # -100%, inert value before peak activates

# Once current_purchase_krw is already at/above DAILY_BUY_TARGET_KRW, each
# 1-share fallback buy (see nonfractional_entry_allowed) must project a rate
# at least this many points higher than the LAST fallback buy's projected
# rate did (or above PEAK_ACTIVATION_RATE if that's higher) -- a ratchet
# that only lets repeated 1-share buys through while the position is
# actually improving, not just standing still or drifting down.
NONFRACTIONAL_ENTRY_RATCHET_STEP = _Decimal("0.03")

# Staged trailing-stop liquidation, keyed off the *relative* drawdown from
# peak profit rate (peak - current_rate) / peak. 익절기준 is now derived
# directly from this same peak/stage state (see
# strategy.next_liquidation_trigger_rate) -- it shows the rate at which the
# next not-yet-fired stage would sell, and doubles as the non-fractional
# fallback-buy entry floor. A fresh (higher) peak restarts this staged
# cycle from scratch, so a partial sell doesn't block another one after a
# new high and pullback. Below PEAK_ACTIVATION_RATE nothing can fire.
LIQUIDATION_STAGE_1_DRAWDOWN = _Decimal("0.30")  # sell LIQUIDATION_STAGE_SELL_FRACTION of current holding
LIQUIDATION_STAGE_2_DRAWDOWN = _Decimal("0.40")  # sell LIQUIDATION_STAGE_SELL_FRACTION of current holding
LIQUIDATION_STAGE_3_DRAWDOWN = _Decimal("0.50")  # sell everything remaining, liquidated=True
LIQUIDATION_STAGE_SELL_FRACTION = _Decimal("0.50")
# How high peak has to have gotten before each of the earlier (less severe)
# stages is even eligible to fire -- a position whose peak never really
# took off doesn't get the same gradual staging as one that ran up a lot.
# Stage 3 (the 50% full-exit stop-loss) has no extra minimum beyond
# PEAK_ACTIVATION_RATE -- it's always the last-resort stop once activated.
#   peak <  LIQUIDATION_STAGE_2_MIN_PEAK (20%): only stage 3 can fire.
#   LIQUIDATION_STAGE_2_MIN_PEAK <= peak < LIQUIDATION_STAGE_1_MIN_PEAK (30%): stages 2-3.
#   peak >= LIQUIDATION_STAGE_1_MIN_PEAK (30%): stages 1-3 (all of them).
LIQUIDATION_STAGE_1_MIN_PEAK = _Decimal("0.30")
LIQUIDATION_STAGE_2_MIN_PEAK = _Decimal("0.20")

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
