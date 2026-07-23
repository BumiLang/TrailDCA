from __future__ import annotations

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
PEAK_ACTIVATION_RATE = _Decimal("0.10")
# take-profit threshold once peak has activated:
#   peak < TAKE_PROFIT_BREAKPOINT: threshold = peak * TAKE_PROFIT_LOW_SLOPE + TAKE_PROFIT_LOW_BASE
#   peak >= TAKE_PROFIT_BREAKPOINT: threshold = peak * TAKE_PROFIT_HIGH_SLOPE
TAKE_PROFIT_BREAKPOINT = _Decimal("0.30")
TAKE_PROFIT_LOW_SLOPE = _Decimal("0.75")
TAKE_PROFIT_LOW_BASE = _Decimal("-0.035")
TAKE_PROFIT_HIGH_SLOPE = _Decimal("0.7")
INITIAL_TAKE_PROFIT_THRESHOLD = _Decimal("-1.00")  # -100%

TICK_SECONDS = 1
DAILY_SNAPSHOT_HOUR_KST = 8
