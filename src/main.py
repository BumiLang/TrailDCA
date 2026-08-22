"""Daily snapshot + 1-second tick orchestration for the DCA / trailing
take-profit strategy described in the project spec.

Run with: python -m src.main
Safety: LIVE_TRADING=false (the .env default) never calls place_order; it
only logs what would have been ordered and simulates the resulting position
in memory so the sheet/logs still show a plausible run.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src import strategy
from src.config import (
    DAILY_BUY_RETRY_SECONDS,
    DAILY_BUY_TARGET_KRW,
    DAILY_SNAPSHOT_HOUR_KST,
    INITIAL_TAKE_PROFIT_THRESHOLD,
    KR_BUY_DELAY_AFTER_OPEN,
    KR_SELL_DELAY_AFTER_OPEN,
    KST,
    LIQUIDATION_STAGE_SELL_FRACTION,
    PEAK_ACTIVATION_RATE,
    PROJECT_ROOT,
    SHEET_FLUSH_INTERVAL_SECONDS,
    STATE_FILE,
    STRATEGY_ENABLED_REFRESH_INTERVAL_SECONDS,
    TICK_SECONDS,
    US_BUY_DELAY_AFTER_OPEN,
    US_SELL_DELAY_AFTER_OPEN,
    Config,
)
from src.models import Market, SheetRow
from src.sheets_client import SheetsClient, fraction_to_percent_str
from src.toss_client import OrderNotFilledError, TossApiError, TossClient

logger = logging.getLogger("traildca")


# ---------------------------------------------------------------------------
# Local run-state: survives process restarts, tracks what's already been done
# today so we never double-buy. The Google Sheet remains the source of truth
# for strategy state (peak/threshold/liquidated/etc).
# ---------------------------------------------------------------------------


class RunState:
    def __init__(self, path: Path):
        self._path = path
        self.last_snapshot_date: str | None = None
        # tracks the daily DCA buy SUCCESS per (date, symbol) -- rule 4 keeps
        # retrying until this is set, not just until one attempt was made
        self.daily_buys: dict[str, list[str]] = {}
        # last buy-attempt timestamp per (date, symbol), used to throttle
        # retries after a failed/unfilled attempt to once every
        # DAILY_BUY_RETRY_SECONDS instead of hammering every tick
        self.daily_buy_attempts: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("state.json is corrupted or unreadable; starting from a fresh run-state")
            return
        self.last_snapshot_date = data.get("last_snapshot_date")
        self.daily_buys = data.get("daily_buys", {})
        self.daily_buy_attempts = data.get("daily_buy_attempts", {})

    def save(self) -> None:
        payload = json.dumps(
            {
                "last_snapshot_date": self.last_snapshot_date,
                "daily_buys": self.daily_buys,
                "daily_buy_attempts": self.daily_buy_attempts,
            },
            ensure_ascii=False,
            indent=2,
        )
        # write-then-rename so a crash mid-write can never leave a truncated/
        # corrupt state.json behind (os.replace is atomic on POSIX and Windows)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        # Retry the rename: on Windows, cloud-sync/antivirus can momentarily
        # hold a lock on the destination right after it's written, which
        # surfaces as a transient PermissionError rather than a real failure.
        for attempt in range(5):
            try:
                os.replace(tmp_path, self._path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))

    @staticmethod
    def _prune(buys: dict[str, object], keep: int = 3) -> None:
        for old_date in sorted(buys)[:-keep] if len(buys) > keep else []:
            del buys[old_date]

    def bought_today(self, date_str: str, symbol: str) -> bool:
        return symbol in self.daily_buys.get(date_str, [])

    def mark_bought(self, date_str: str, symbol: str) -> None:
        self.daily_buys.setdefault(date_str, []).append(symbol)
        self._prune(self.daily_buys)
        self.save()

    def seconds_since_last_buy_attempt(self, date_str: str, symbol: str, now: dt.datetime) -> float | None:
        iso = self.daily_buy_attempts.get(date_str, {}).get(symbol)
        if iso is None:
            return None
        return (now - dt.datetime.fromisoformat(iso)).total_seconds()

    def mark_buy_attempt(self, date_str: str, symbol: str, now: dt.datetime) -> None:
        self.daily_buy_attempts.setdefault(date_str, {})[symbol] = now.isoformat(timespec="seconds")
        self._prune(self.daily_buy_attempts)
        self.save()


# ---------------------------------------------------------------------------
# Market sessions (regular-hours windows), refreshed once/day at/after 08:00
# KST, and refreshed immediately on service startup at any hour. The US
# regular session can spill past midnight KST, so a startup between 00:00
# and market close still needs to recognize an overnight session that
# started the previous business day -- us_prev_start/us_prev_end carry that
# session forward (from the same market-calendar response, no extra call).
# ---------------------------------------------------------------------------


@dataclass
class MarketSessions:
    loaded_date: str
    kr_start: dt.datetime | None
    kr_end: dt.datetime | None
    us_start: dt.datetime | None
    us_end: dt.datetime | None
    us_prev_start: dt.datetime | None
    us_prev_end: dt.datetime | None


def _parse_session(session: dict | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    if not session:
        return None, None
    return dt.datetime.fromisoformat(session["startTime"]), dt.datetime.fromisoformat(session["endTime"])


def load_market_sessions(toss: TossClient) -> MarketSessions:
    today = toss.now().date().isoformat()
    kr = toss.get_market_calendar("KR")
    us = toss.get_market_calendar("US")

    kr_integrated = (kr.get("today") or {}).get("integrated") or {}
    kr_start, kr_end = _parse_session(kr_integrated.get("regularMarket"))
    us_start, us_end = _parse_session((us.get("today") or {}).get("regularMarket"))
    us_prev_start, us_prev_end = _parse_session((us.get("previousBusinessDay") or {}).get("regularMarket"))

    return MarketSessions(today, kr_start, kr_end, us_start, us_end, us_prev_start, us_prev_end)


def _current_session_start(sessions: MarketSessions | None, market: Market, now: dt.datetime) -> dt.datetime | None:
    """Start time of the session currently open for `market`, or None if closed.

    For US, checks both today's session and yesterday's overnight session
    (still relevant if it hasn't ended yet, e.g. a 00:00-05:00 KST startup
    during last night's run) and returns whichever one `now` actually falls
    inside -- this is the single source of truth `is_market_open` and
    `_trading_day_key` both build on, so they can never disagree about which
    session (and therefore which session start) is currently active.
    """
    if sessions is None:
        return None
    if market == Market.KR:
        candidates = [(sessions.kr_start, sessions.kr_end)]
    else:
        candidates = [(sessions.us_start, sessions.us_end), (sessions.us_prev_start, sessions.us_prev_end)]
    for start, end in candidates:
        if start is not None and end is not None and start <= now < end:
            return start
    return None


def is_market_open(sessions: MarketSessions | None, market: Market, now: dt.datetime) -> bool:
    return _current_session_start(sessions, market, now) is not None


def _trading_day_key(market: Market, sessions: MarketSessions, now: dt.datetime) -> str:
    """Calendar-date key for once/day gating (e.g. the daily DCA buy).

    KR sessions never cross midnight KST, so today's date is unambiguous.
    A US session that started ~22:30 KST can still be open past midnight;
    if we keyed once/day tracking by `now.date()` directly, the key would
    flip mid-session and the same continuous session would look like a new
    day, letting the daily buy fire twice (once before midnight, once
    after). Keep using the date the *currently open* session started on.
    """
    if market != Market.US:
        return now.date().isoformat()
    start = _current_session_start(sessions, Market.US, now)
    if start is not None:
        return start.date().isoformat()
    return now.date().isoformat()


# ---------------------------------------------------------------------------
# Order execution: uniform live / dry-run interface. Returns a dict shaped
# like a HoldingsItem for the traded symbol after the trade "settles".
# ---------------------------------------------------------------------------


class OrderExecutor:
    def __init__(self, toss: TossClient, account_seq: int | str, live: bool):
        self._toss = toss
        self._account_seq = account_seq
        self._live = live
        self._sim: dict[str, dict] = {}

    def current_price(self, symbol: str) -> Decimal:
        prices = self._toss.get_prices([symbol])
        return Decimal(prices[0]["lastPrice"])

    def buy(
        self,
        symbol: str,
        currency: str,
        order_type: str,
        quantity: Decimal | None = None,
        order_amount: Decimal | None = None,
        client_order_id: str = "",
        current_holding: dict | None = None,
    ) -> dict:
        if self._live:
            order = self._toss.place_order(
                self._account_seq,
                symbol,
                "BUY",
                order_type,
                quantity=quantity,
                order_amount=order_amount,
                client_order_id=client_order_id,
            )
            final = self._toss.wait_for_terminal_status(self._account_seq, order["orderId"])
            if final.get("status") != "FILLED":
                raise OrderNotFilledError(final)
            holdings = self._toss.get_holdings(self._account_seq, symbol=symbol)
            items = holdings.get("items", [])
            if not items:
                raise RuntimeError(f"buy order for {symbol} filled but holdings lookup returned nothing")
            return items[0]

        # dry-run: never calls place_order. Simulated against the latest
        # real price so logs/sheet stay directionally meaningful. The first
        # touch of a symbol seeds the running simulation from its real
        # current holding (if any) so a simulated buy layers on top of the
        # actual position instead of overwriting the sheet with a phantom
        # from-zero position.
        if symbol not in self._sim:
            if current_holding is not None:
                self._sim[symbol] = {
                    "quantity": Decimal(current_holding["quantity"]),
                    "purchase_amount": Decimal(current_holding["marketValue"]["purchaseAmount"]),
                }
            else:
                self._sim[symbol] = {"quantity": Decimal(0), "purchase_amount": Decimal(0)}
        price = self.current_price(symbol)
        sim = self._sim[symbol]
        if order_amount is not None:
            bought_qty = order_amount / price
            cost = order_amount
        else:
            bought_qty = quantity
            cost = quantity * price
        sim["quantity"] += bought_qty
        sim["purchase_amount"] += cost
        valuation = sim["quantity"] * price
        profit_amount = valuation - sim["purchase_amount"]
        rate = profit_amount / sim["purchase_amount"]
        logger.info(
            "[DRY-RUN] BUY %s type=%s qty=%s amount=%s price=%s -> sim_qty=%s sim_rate=%.4f",
            symbol,
            order_type,
            quantity,
            order_amount,
            price,
            sim["quantity"],
            rate,
        )
        return {
            "symbol": symbol,
            "quantity": str(sim["quantity"]),
            "currency": currency,
            "marketValue": {"purchaseAmount": str(sim["purchase_amount"]), "amount": str(valuation)},
            "profitLoss": {"rate": str(rate), "amount": str(profit_amount)},
        }

    def liquidate(self, symbol: str, client_order_id: str = "") -> None:
        if self._live:
            sellable = Decimal(self._toss.get_sellable_quantity(self._account_seq, symbol)["sellableQuantity"])
            order = self._toss.place_order(
                self._account_seq,
                symbol,
                "SELL",
                "MARKET",
                quantity=sellable,
                client_order_id=client_order_id,
            )
            final = self._toss.wait_for_terminal_status(self._account_seq, order["orderId"])
            if final.get("status") != "FILLED":
                raise OrderNotFilledError(final)
        else:
            sim = self._sim.pop(symbol, None)
            logger.info("[DRY-RUN] SELL(all) %s qty=%s", symbol, sim["quantity"] if sim else "0")

    def liquidate_partial(
        self, symbol: str, market: Market, currency: str, fraction: Decimal, client_order_id: str = ""
    ) -> dict | None:
        """Sell `fraction` of the current sellable quantity as a market
        order (e.g. fraction=0.5 sells half the position). KR quantities
        are floored to a whole share since KR doesn't support fractional
        order quantities (see _attempt_fallback_share_buy). Returns the
        post-trade holdings item (same shape as buy()/the dry-run
        simulation), or None if the computed quantity rounds down to zero
        -- too small a whole-share KR position to split at this fraction;
        the caller should leave sell_stage untouched and let a deeper
        stage fire instead."""
        if self._live:
            sellable = Decimal(self._toss.get_sellable_quantity(self._account_seq, symbol)["sellableQuantity"])
            qty = _partial_sell_quantity(sellable, fraction, market)
            if qty <= 0:
                return None
            order = self._toss.place_order(
                self._account_seq,
                symbol,
                "SELL",
                "MARKET",
                quantity=qty,
                client_order_id=client_order_id,
            )
            final = self._toss.wait_for_terminal_status(self._account_seq, order["orderId"])
            if final.get("status") != "FILLED":
                raise OrderNotFilledError(final)
            holdings = self._toss.get_holdings(self._account_seq, symbol=symbol)
            items = holdings.get("items", [])
            if not items:
                raise RuntimeError(f"partial sell for {symbol} filled but holdings lookup returned nothing")
            return items[0]

        sim = self._sim.get(symbol)
        if sim is None:
            return None
        qty = _partial_sell_quantity(sim["quantity"], fraction, market)
        if qty <= 0:
            return None
        price = self.current_price(symbol)
        cost_removed = sim["purchase_amount"] * (qty / sim["quantity"])
        sim["quantity"] -= qty
        sim["purchase_amount"] -= cost_removed
        valuation = sim["quantity"] * price
        profit_amount = valuation - sim["purchase_amount"]
        rate = profit_amount / sim["purchase_amount"]
        logger.info(
            "[DRY-RUN] SELL(partial x%s) %s qty=%s -> sim_qty=%s sim_rate=%.4f",
            fraction, symbol, qty, sim["quantity"], rate,
        )
        return {
            "symbol": symbol,
            "quantity": str(sim["quantity"]),
            "currency": currency,
            "marketValue": {"purchaseAmount": str(sim["purchase_amount"]), "amount": str(valuation)},
            "profitLoss": {"rate": str(rate), "amount": str(profit_amount)},
        }


PARTIAL_SELL_MAX_DECIMALS = Decimal("0.000001")  # Toss rejects order quantities with more than 6 decimal places


def _partial_sell_quantity(quantity: Decimal, fraction: Decimal, market: Market) -> Decimal:
    """KR doesn't support fractional order quantities (see the fallback
    whole-share buy path in _attempt_fallback_share_buy) -- floor a partial
    sell to a whole share there. Other markets keep fractional precision,
    but Toss still rejects a quantity with more than 6 decimal places
    ("소수점 수량은 소수점 6자리까지 지원합니다") -- quantity * fraction can
    easily produce more than that (e.g. a sellable quantity that already
    carries several decimal places), so round down to 6 places rather than
    risk selling slightly more than intended."""
    raw = quantity * fraction
    if market == Market.KR:
        return raw.to_integral_value(rounding=ROUND_DOWN)
    return raw.quantize(PARTIAL_SELL_MAX_DECIMALS, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# Holdings sync (on service startup, and once/day thereafter): pull real
# holdings and reconcile into the sheet. Existing rows only get their live
# fields (quantity/purchase/rate/timestamp) refreshed -- symbols in the
# sheet but no longer held are left untouched. New holdings not yet in the
# sheet are appended with default strategy state.
# ---------------------------------------------------------------------------


def _purchase_amount_krw(item: dict, exchange_rate_usd_krw: Decimal) -> Decimal:
    amount = Decimal(item["marketValue"]["purchaseAmount"])
    if item.get("currency") == "KRW":
        return amount
    return amount * exchange_rate_usd_krw


def _valuation_amount_krw(item: dict, exchange_rate_usd_krw: Decimal) -> Decimal:
    amount = Decimal(item["marketValue"]["amount"])
    if item.get("currency") == "KRW":
        return amount
    return amount * exchange_rate_usd_krw


def _profit_amount_krw(item: dict, exchange_rate_usd_krw: Decimal) -> Decimal:
    amount = Decimal(item["profitLoss"]["amount"])
    if item.get("currency") == "KRW":
        return amount
    return amount * exchange_rate_usd_krw


def _closing_rate(
    toss: TossClient, item: dict, market: Market, sessions: MarketSessions | None, now: dt.datetime
) -> Decimal | None:
    """The profit rate this holding would show at the most recent *fully
    closed* trading day's close, for the once/day 최고수익률 update (see
    allow_peak_update in strategy.update_peak_threshold_and_sell_stage_gated).
    get_candles()'s newest daily bar is a still-forming, not-yet-closed
    candle whenever `market` happens to be open right now (e.g. this ran
    from a mid-session service restart, not the intended ~08:00 sync) --
    in that case use the second-newest bar (the last day that actually
    closed) instead. Returns None if the candle lookup fails or the
    holding is empty, so the caller can skip today's peak update rather
    than commit a bad value.
    """
    try:
        candles = toss.get_candles(item["symbol"], interval="1d", count=2).get("candles", [])
    except Exception:
        logger.exception("%s: failed to fetch daily candle for peak update; skipping today's update", item["symbol"])
        return None
    if not candles:
        return None
    market_open_right_now = sessions is not None and is_market_open(sessions, market, now)
    idx = 1 if market_open_right_now and len(candles) > 1 else 0
    close_price = Decimal(str(candles[idx]["closePrice"]))
    quantity = Decimal(item["quantity"])
    purchase_amount = Decimal(item["marketValue"]["purchaseAmount"])
    if purchase_amount == 0:
        return None
    return (quantity * close_price - purchase_amount) / purchase_amount


def daily_snapshot(
    toss: TossClient,
    sheets: SheetsClient,
    account_seq: int | str,
    exchange_rate_usd_krw: Decimal,
    sessions: MarketSessions | None,
    now: dt.datetime,
) -> None:
    holdings = toss.get_holdings(account_seq)
    items = holdings.get("items", [])
    existing = {r.symbol: r for r in sheets.read_rows()}

    updates: list[tuple[int, str, str]] = []
    new_rows: list[dict] = []
    now_iso = dt.datetime.now(KST).isoformat(timespec="seconds")

    for item in items:
        symbol = item["symbol"]
        quantity = Decimal(item["quantity"])
        rate = Decimal(item["profitLoss"]["rate"])
        purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)
        valuation_krw = _valuation_amount_krw(item, exchange_rate_usd_krw)
        profit_krw = _profit_amount_krw(item, exchange_rate_usd_krw)

        if symbol in existing:
            row = existing[symbol]
            updates.append((row.row_number, "보유수량", str(quantity)))
            updates.append((row.row_number, "매입금액_원화", str(purchase_krw.quantize(Decimal("1")))))
            updates.append((row.row_number, "평가금액_원화", str(valuation_krw.quantize(Decimal("1")))))
            updates.append((row.row_number, "평가손익_원화", str(profit_krw.quantize(Decimal("1")))))
            updates.append((row.row_number, "수익률", fraction_to_percent_str(rate)))
            updates.append((row.row_number, "마지막갱신", now_iso))
            # 최고수익률's only once/day update happens right here, using the
            # most recent *fully closed* trading day's close (see
            # _closing_rate) rather than this snapshot's live rate -- a
            # mid-session service restart would otherwise let peak jump off
            # an intraday, not-yet-closed price (see the allow_peak_update
            # docstring on strategy.update_peak_threshold_and_sell_stage_gated).
            # 최고수익률 updates regardless of 전략적용여부 (same as 수익률
            # above); 익절기준/매도단계 only commit while strategy_enabled,
            # mirroring process_symbol's own gating. If the candle lookup
            # fails, closing_rate is None and today's peak update is simply
            # skipped -- retried on the next daily snapshot.
            closing_rate = _closing_rate(toss, item, Market(item.get("marketCountry", "KR")), sessions, now)
            if closing_rate is not None:
                new_peak, new_threshold, stage, _, _ = strategy.update_peak_threshold_and_sell_stage_gated(
                    row.peak_rate, closing_rate, row.sell_stage, purchase_krw, just_reached_target=False, allow_peak_update=True,
                )
                updates.append((row.row_number, "최고수익률", fraction_to_percent_str(new_peak)))
                if row.strategy_enabled:
                    updates.append((row.row_number, "익절기준", fraction_to_percent_str(new_threshold)))
                    updates.append((row.row_number, "매도단계", str(stage)))
        else:
            # 최고수익률 seeds to the current rate (no tracked history yet);
            # 익절기준 is the next staged-sell trigger rate for that peak
            # (see strategy.next_liquidation_trigger_rate), not hardcoded,
            # so a newly-synced holding that's already well above the 10%
            # activation bar gets a real value instead of the inert -100%
            # default.
            threshold = strategy.next_liquidation_trigger_rate(rate, 0) if rate >= PEAK_ACTIVATION_RATE else INITIAL_TAKE_PROFIT_THRESHOLD
            new_rows.append(dict(
                symbol=symbol,
                name=item.get("name", ""),
                market=item.get("marketCountry", "KR"),
                quantity=str(quantity),
                purchase_amount_krw=str(purchase_krw.quantize(Decimal("1"))),
                valuation_amount_krw=str(valuation_krw.quantize(Decimal("1"))),
                profit_amount_krw=str(profit_krw.quantize(Decimal("1"))),
                profit_rate_pct=fraction_to_percent_str(rate),
                peak_rate_pct=fraction_to_percent_str(rate),
                take_profit_threshold_pct=fraction_to_percent_str(threshold),
            ))
            logger.info("new holding discovered, added to sheet: %s", symbol)

    sheets.batch_write(updates)
    sheets.append_default_rows(new_rows)
    logger.info("daily snapshot reconciled %d holdings", len(items))


def _delete_liquidated_rows(sheets: SheetsClient) -> None:
    """Weekly cleanup: on the Monday run of the daily snapshot, drop
    청산여부=TRUE rows from the sheet entirely so it doesn't grow unbounded
    with old, fully-exited positions."""
    rows = sheets.read_rows()
    liquidated_row_numbers = [r.row_number for r in rows if r.liquidated]
    if liquidated_row_numbers:
        sheets.delete_rows(liquidated_row_numbers)
        logger.info("weekly cleanup: deleted %d liquidated row(s) from sheet", len(liquidated_row_numbers))


# ---------------------------------------------------------------------------
# 1-second tick: peak/threshold/liquidation + once-per-day buy trigger.
# ---------------------------------------------------------------------------


def _apply_trade_result(row: SheetRow, item: dict, exchange_rate_usd_krw: Decimal, now: dt.datetime, updates: list) -> None:
    row.quantity = Decimal(item["quantity"])
    row.purchase_amount_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)
    row.valuation_amount_krw = _valuation_amount_krw(item, exchange_rate_usd_krw)
    row.profit_amount_krw = _profit_amount_krw(item, exchange_rate_usd_krw)
    row.profit_rate = Decimal(item["profitLoss"]["rate"])
    updates.append((row.row_number, "보유수량", str(row.quantity)))
    updates.append((row.row_number, "매입금액_원화", str(row.purchase_amount_krw.quantize(Decimal("1")))))
    updates.append((row.row_number, "평가금액_원화", str(row.valuation_amount_krw.quantize(Decimal("1")))))
    updates.append((row.row_number, "평가손익_원화", str(row.profit_amount_krw.quantize(Decimal("1")))))
    updates.append((row.row_number, "수익률", fraction_to_percent_str(row.profit_rate)))
    updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))


def _reset_peak_if_target_just_crossed(row: SheetRow, purchase_krw_before_buy: Decimal, updates: list) -> None:
    """Call right after _apply_trade_result() for a buy that may have pushed
    purchase_amount_krw from below DAILY_BUY_TARGET_KRW to at/above it.

    process_symbol's own crossing detection (just_reached_target) only
    catches a crossing that already happened *before* the current tick --
    it's computed from the peak/threshold block, which runs before this
    tick's own buy. A crossing caused by THIS buy is invisible to it: by
    the next tick, row.purchase_amount_krw already reflects the post-buy
    value, so the "was it below target last tick" comparison no longer
    sees a transition. Left alone, the staged-sell gate flips open on the
    very next tick using whatever peak silently accumulated while still
    below target (when sell actions were fully suppressed) -- handing back
    a stale, possibly much higher peak that can trigger an immediate
    stage sell against a position that's barely been held.
    """
    if purchase_krw_before_buy >= DAILY_BUY_TARGET_KRW or row.purchase_amount_krw < DAILY_BUY_TARGET_KRW:
        return
    new_peak, new_threshold, stage, _, _ = strategy.update_peak_threshold_and_sell_stage_gated(
        row.peak_rate, row.profit_rate, row.sell_stage, row.purchase_amount_krw, just_reached_target=True, allow_peak_update=False,
    )
    row.peak_rate, row.take_profit_threshold, row.sell_stage = new_peak, new_threshold, stage
    updates.append((row.row_number, "최고수익률", fraction_to_percent_str(new_peak)))
    updates.append((row.row_number, "익절기준", fraction_to_percent_str(new_threshold)))
    updates.append((row.row_number, "매도단계", str(stage)))
    logger.info(
        "%s: DCA target just crossed by this buy -- resetting peak/threshold/stage fresh from current rate %.4f",
        row.symbol, new_peak,
    )


def _attempt_daily_buy(
    row: SheetRow,
    item: dict | None,
    current_rate: Decimal,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    today_str: str,
    now: dt.datetime,
    updates: list,
) -> bool:
    """Rule 4 (unified, market-agnostic): DCA buy, retried until it fills.

    - purchase_amount_krw < 100,000: buy 5,000 KRW worth.
    - purchase_amount_krw >= 100,000 and current_rate >= 10%: buy 5,000 KRW worth.
    - otherwise: no buy right now.

    The 5,000 KRW order is placed as an amount order first (this only works
    for symbols/brokers that support fractional shares). If that order
    errors out for any reason, fall back to buying a single whole share --
    the amount-buy's eligibility condition already holds, so the fallback
    doesn't need to re-check it, though it applies its own extra entry-rate
    gate (see _attempt_fallback_share_buy) since a whole-share buy resets
    peak/threshold on every fill, not just the first one.

    Returns True iff an order actually filled -- the caller only stops
    retrying (throttled to DAILY_BUY_RETRY_SECONDS) once this is True.
    """
    purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw) if item else row.purchase_amount_krw
    amount_krw = strategy.daily_buy_amount_krw(purchase_krw, current_rate)
    if amount_krw is None:
        logger.debug(
            "%s: no buy right now (purchase=%s KRW, rate %.4f below 10%% resume bar)",
            row.symbol, purchase_krw, current_rate,
        )
        return False

    currency = "KRW" if row.market == Market.KR else "USD"
    order_amount = amount_krw if currency == "KRW" else (amount_krw / exchange_rate_usd_krw).quantize(Decimal("0.01"))
    client_order_id = f"{today_str}-{row.symbol}-DCA"[:36]
    try:
        result = executor.buy(
            row.symbol,
            currency,
            "MARKET",
            order_amount=order_amount,
            client_order_id=client_order_id,
            current_holding=item,
        )
    except (TossApiError, OrderNotFilledError):
        # Silently fall back -- KR symbols currently fail every amount-buy
        # by design (Toss restricts amount orders to US-market symbols), so
        # logging this every single DCA-buy day is pure noise. Any failure
        # from the fallback path itself is still logged there.
        return _attempt_fallback_share_buy(row, item, executor, exchange_rate_usd_krw, today_str, now, updates)

    _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
    # Record the actual settled rate as the ratchet floor the next 1-share
    # fallback buy for this symbol must clear (see
    # strategy.nonfractional_entry_allowed) -- a fractional amount buy isn't
    # itself rate-gated, but it still moves the average cost basis, so the
    # ratchet needs to reflect where the position actually landed rather
    # than staying anchored to whatever a prior fallback buy left behind.
    row.last_buy_rate = row.profit_rate
    updates.append((row.row_number, "직전매수수익률", fraction_to_percent_str(row.last_buy_rate)))
    _reset_peak_if_target_just_crossed(row, purchase_krw, updates)
    logger.info("BUY(amount) %s target=%sKRW (%s%s)", row.symbol, amount_krw, order_amount, currency)
    return True


def _attempt_fallback_share_buy(
    row: SheetRow,
    item: dict | None,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    today_str: str,
    now: dt.datetime,
    updates: list,
) -> bool:
    """1-share fallback for symbols that don't support fractional-amount
    orders. Every DCA-buy day for such a symbol goes through this path (the
    amount order fails every time). Entry eligibility is decided by
    strategy.nonfractional_entry_allowed (DCA grace window / flat 10% floor
    once past the ceiling / rate-vs-threshold gate once past the target --
    see that function's docstring). Assumes the symbol is already held by
    the time this strategy manages it (first entry into a symbol is done
    manually, outside this bot).
    """
    price = executor.current_price(row.symbol)
    current_purchase_amount = Decimal(item["marketValue"]["purchaseAmount"])
    projected_quantity = Decimal(item["quantity"]) + Decimal(1)
    projected_purchase_amount = current_purchase_amount + price
    projected_valuation = projected_quantity * price
    projected_rate = (projected_valuation - projected_purchase_amount) / projected_purchase_amount

    current_purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)
    projected_purchase_krw = (
        projected_purchase_amount if item.get("currency") == "KRW"
        else projected_purchase_amount * exchange_rate_usd_krw
    )

    is_grace_window = strategy.nonfractional_is_dca_grace_window(current_purchase_krw, projected_purchase_krw)

    if not strategy.nonfractional_entry_allowed(
        current_purchase_krw, projected_purchase_krw, projected_rate, row.last_buy_rate
    ):
        logger.debug(
            "%s: skipping 1-share fallback buy, post-buy rate %.4f below entry floor "
            "(last buy rate=%.4f) (current_purchase=%s KRW, projected_purchase=%s KRW)",
            row.symbol, projected_rate, row.last_buy_rate, current_purchase_krw, projected_purchase_krw,
        )
        return False

    client_order_id = f"{today_str}-{row.symbol}-DCA1"[:36]
    currency = "KRW" if row.market == Market.KR else "USD"
    try:
        result = executor.buy(
            row.symbol,
            currency,
            "MARKET",
            quantity=Decimal(1),
            client_order_id=client_order_id,
            current_holding=item,
        )
    except (TossApiError, OrderNotFilledError) as e:
        logger.warning("fallback 1-share buy also failed for %s; will retry in %ds: %s", row.symbol, DAILY_BUY_RETRY_SECONDS, e)
        return False

    _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
    # Record this fill's ACTUAL settled rate (not the pre-buy projected_rate
    # used for the entry decision above) as the ratchet floor the NEXT
    # fallback buy for this symbol must clear (see
    # strategy.nonfractional_entry_allowed) -- every successful fill
    # updates it, grace-window or not, so the ratchet always reflects the
    # most recent purchase's real outcome.
    row.last_buy_rate = row.profit_rate
    updates.append((row.row_number, "직전매수수익률", fraction_to_percent_str(row.last_buy_rate)))
    # Outside the DCA grace window, a fill unconditionally resets peak_rate
    # to the projected post-buy rate used for the entry decision (not maxed
    # against the prior peak) -- buying 1 more share at the current price
    # pulls the average cost basis toward that price, which pulls the
    # profit rate down, so the peak has to reflect that dip rather than
    # staying anchored to a pre-buy high. 익절기준 is recomputed from that
    # new peak at the row's current sell_stage (not forced back to 0 --
    # this reset doesn't imply a stage has been cleared). Inside the grace
    # window, peak_rate is left alone: these buys aren't rate-gated at all,
    # so force-resetting peak to whatever (possibly poor) rate results each
    # day would clobber the trailing-stop tracking while the position is
    # still being built out.
    if not is_grace_window:
        row.peak_rate = projected_rate
        row.take_profit_threshold = (
            strategy.next_liquidation_trigger_rate(projected_rate, row.sell_stage)
            if projected_rate >= PEAK_ACTIVATION_RATE
            else INITIAL_TAKE_PROFIT_THRESHOLD
        )
        updates.append((row.row_number, "최고수익률", fraction_to_percent_str(row.peak_rate)))
        updates.append((row.row_number, "익절기준", fraction_to_percent_str(row.take_profit_threshold)))
    # Grace-window buys are allowed to jump purchase_amount_krw straight
    # past DAILY_BUY_TARGET_KRW in one step (see
    # nonfractional_is_dca_grace_window) without the `if not is_grace_window`
    # peak reset above ever running -- so a crossing here still needs this
    # dedicated reset, using the REAL settled rate and taking precedence
    # over the projected_rate reset above if both apply on this tick.
    _reset_peak_if_target_just_crossed(row, current_purchase_krw, updates)
    logger.info(
        "BUY(1 share fallback) %s projected_rate=%.4f actual_rate=%s grace_window=%s",
        row.symbol, projected_rate, result["profitLoss"]["rate"], is_grace_window,
    )
    return True


def process_symbol(
    row: SheetRow,
    item: dict | None,
    sessions: MarketSessions,
    now: dt.datetime,
    run_state: RunState,
    today_str: str,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    updates: list,
    was_strategy_enabled: bool,
) -> None:
    if row.liquidated:
        if item is None:
            logger.debug("%s skipped: liquidated", row.symbol)
            return
        # Real holdings show a position again even though the sheet still
        # marks this row liquidated -- e.g. manually re-bought via MTS
        # after a take-profit exit or after being reconciled as externally
        # sold. Revive the row: clear liquidated, mirror the live position,
        # and reseed peak/threshold/sell_stage from the current rate, same
        # gating as any other fresh position
        # (update_peak_threshold_and_sell_stage_gated with
        # just_reached_target=True) -- frozen at -100% if this re-entry is
        # still below DAILY_BUY_TARGET_KRW, or activated immediately from
        # the current rate if it already reached/passed it in one buy.
        # Also mark today's DCA buy as already done for this symbol, so
        # Rule 4 doesn't stack its own buy on top of the manual one on the
        # same trading day. Return without running the rest of this tick's
        # logic -- the revived row gets its normal mirror/peak/threshold/buy
        # handling starting next tick, on a clean pass.
        current_rate = Decimal(item["profitLoss"]["rate"])
        purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)
        valuation_krw = _valuation_amount_krw(item, exchange_rate_usd_krw)
        profit_krw = _profit_amount_krw(item, exchange_rate_usd_krw)
        new_peak, new_threshold, new_stage, _, _ = strategy.update_peak_threshold_and_sell_stage_gated(
            current_rate, current_rate, 0, purchase_krw, just_reached_target=True, allow_peak_update=False,
        )
        row.liquidated = False
        row.quantity = Decimal(item["quantity"])
        row.purchase_amount_krw = purchase_krw
        row.valuation_amount_krw = valuation_krw
        row.profit_amount_krw = profit_krw
        row.profit_rate = current_rate
        row.peak_rate = new_peak
        row.take_profit_threshold = new_threshold
        row.sell_stage = new_stage
        updates.append((row.row_number, "청산여부", "FALSE"))
        updates.append((row.row_number, "보유수량", str(row.quantity)))
        updates.append((row.row_number, "매입금액_원화", str(purchase_krw.quantize(Decimal("1")))))
        updates.append((row.row_number, "평가금액_원화", str(valuation_krw.quantize(Decimal("1")))))
        updates.append((row.row_number, "평가손익_원화", str(profit_krw.quantize(Decimal("1")))))
        updates.append((row.row_number, "수익률", fraction_to_percent_str(current_rate)))
        updates.append((row.row_number, "최고수익률", fraction_to_percent_str(new_peak)))
        updates.append((row.row_number, "익절기준", fraction_to_percent_str(new_threshold)))
        updates.append((row.row_number, "매도단계", str(new_stage)))
        updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))
        buy_day_key = _trading_day_key(row.market, sessions, now)
        if not run_state.bought_today(buy_day_key, row.symbol):
            run_state.mark_bought(buy_day_key, row.symbol)
        logger.info(
            "%s: liquidated row shows a live position again -- reviving (peak=%.4f threshold=%.4f from current rate %.4f, purchase=%s KRW), skipping today's DCA buy",
            row.symbol, new_peak, new_threshold, current_rate, purchase_krw,
        )
        return
    session_start = _current_session_start(sessions, row.market, now)
    if session_start is None:
        logger.debug("%s skipped: %s market closed", row.symbol, row.market.value)
        return
    # Buy/sell orders only start once the session has been open this long --
    # peak/threshold bookkeeping below still runs from the open so the
    # threshold is already accurate once orders are allowed to fire. Delay
    # is configurable independently per market and per side.
    if row.market == Market.KR:
        buy_delay, sell_delay = KR_BUY_DELAY_AFTER_OPEN, KR_SELL_DELAY_AFTER_OPEN
    else:
        buy_delay, sell_delay = US_BUY_DELAY_AFTER_OPEN, US_SELL_DELAY_AFTER_OPEN
    since_open = now - session_start
    buy_allowed = since_open >= buy_delay
    sell_allowed = since_open >= sell_delay

    held = item is not None
    current_rate = Decimal(item["profitLoss"]["rate"]) if held else Decimal(0)

    if held:
        # Live mirror of the real position, refreshed every tick regardless
        # of 전략적용여부 -- the sheet should always reflect the actual
        # account even for symbols the strategy isn't actively managing.
        # Captured before row.purchase_amount_krw is overwritten below, so
        # the peak/threshold block further down can tell whether this tick
        # is the one where the DCA target was just crossed.
        just_reached_target = row.purchase_amount_krw < DAILY_BUY_TARGET_KRW
        # Our own buys and sells always update row.quantity immediately via
        # _apply_trade_result, so any quantity increase visible here (vs.
        # what we last recorded) reflects a buy this bot didn't place itself
        # -- e.g. a manual top-up via the MTS app. Captured before
        # row.quantity is overwritten below; see external_buy_detected's use
        # further down (update_peak_threshold_and_sell_stage_gated).
        new_quantity = Decimal(item["quantity"])
        external_buy_detected = new_quantity > row.quantity
        purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)
        valuation_krw = _valuation_amount_krw(item, exchange_rate_usd_krw)
        profit_krw = _profit_amount_krw(item, exchange_rate_usd_krw)
        row.quantity = new_quantity
        row.purchase_amount_krw = purchase_krw
        row.valuation_amount_krw = valuation_krw
        row.profit_amount_krw = profit_krw
        updates.append((row.row_number, "보유수량", str(row.quantity)))
        updates.append((row.row_number, "매입금액_원화", str(purchase_krw.quantize(Decimal("1")))))
        updates.append((row.row_number, "평가금액_원화", str(valuation_krw.quantize(Decimal("1")))))
        updates.append((row.row_number, "평가손익_원화", str(profit_krw.quantize(Decimal("1")))))
        updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))
    elif row.quantity > 0:
        # Real holdings no longer include this symbol, but the sheet still
        # shows a live quantity and it was never marked liquidated by this
        # bot (row.liquidated == False is guaranteed here, checked at the
        # top of this function) -- the position was almost certainly closed
        # out manually (e.g. via the MTS app) outside this process.
        # Reconcile the sheet to reality and permanently retire the row so
        # a stale purchase_amount_krw can't make Rule 4 try to re-buy a
        # position the user just exited on purpose. (row.quantity == 0 here
        # instead means the row was only ever manually pre-added and never
        # actually bought yet -- leave those alone so a first manual buy
        # can still be picked up normally.)
        row.quantity = Decimal(0)
        row.purchase_amount_krw = Decimal(0)
        row.valuation_amount_krw = Decimal(0)
        row.profit_amount_krw = Decimal(0)
        row.liquidated = True
        row.sell_stage = 0
        updates.append((row.row_number, "보유수량", "0"))
        updates.append((row.row_number, "매입금액_원화", "0"))
        updates.append((row.row_number, "평가금액_원화", "0"))
        updates.append((row.row_number, "평가손익_원화", "0"))
        updates.append((row.row_number, "매도단계", "0"))
        updates.append((row.row_number, "청산여부", "TRUE"))
        updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))
        logger.info("%s: no longer held but never marked liquidated -- reconciling as externally liquidated", row.symbol)
        return

    if held:
        # 수익률 keeps mirroring the live rate every tick regardless of
        # 전략적용여부. 최고수익률 itself, though, no longer chases current_rate
        # tick by tick (allow_peak_update=False here) -- it's only allowed to
        # rise once/day, from daily_snapshot()'s ~08:00 sync (effectively
        # 전일종가, since neither market is open at that hour). The hard
        # resets below (DCA target just crossed, or 전략적용여부 flipping
        # FALSE -> TRUE) still happen immediately in real time regardless --
        # discarding whatever peak/threshold/stage accumulated while
        # unmanaged/still building out can't wait for the next snapshot,
        # since it directly gates whether a sell can even fire right now.
        # Drawdown/action checks against whatever peak is on record still run
        # every tick, so a stage can still fire intraday.
        just_enabled = row.strategy_enabled and not was_strategy_enabled
        new_peak, new_threshold, stage, next_stage, action = strategy.update_peak_threshold_and_sell_stage_gated(
            row.peak_rate, current_rate, row.sell_stage, row.purchase_amount_krw, just_reached_target or just_enabled,
            allow_peak_update=False, external_buy_detected=external_buy_detected,
        )
        row.peak_rate, row.profit_rate = new_peak, current_rate
        updates.append((row.row_number, "수익률", fraction_to_percent_str(current_rate)))
        updates.append((row.row_number, "최고수익률", fraction_to_percent_str(new_peak)))

        if external_buy_detected:
            # 직전매수수익률 is otherwise only ever written by this bot's own
            # buys (fractional amount buys and 1-share fallback buys alike)
            # -- an externally-bought quantity increase resets it too, to
            # the same real current_rate used for the peak reset above, so
            # a stale pre-existing ratchet floor from before this outside
            # buy doesn't linger and block/misjudge the next fallback buy.
            row.last_buy_rate = current_rate
            updates.append((row.row_number, "직전매수수익률", fraction_to_percent_str(current_rate)))
            logger.info(
                "%s: external buy detected (quantity increased outside this bot) -- resetting peak/threshold/stage/직전매수수익률 from current rate %.4f",
                row.symbol, new_peak,
            )

        if not row.strategy_enabled:
            logger.debug("%s: strategy_enabled=False, only 수익률/최고수익률 updated", row.symbol)
        else:
            # 익절기준/매도단계 and the sell decision itself only apply while
            # the strategy is actively managing this row. stage is committed
            # unconditionally here -- it's pure peak-driven bookkeeping (a
            # fresh high restarts the staged cycle), independent of whether
            # the order below actually fires this tick.
            row.take_profit_threshold, row.sell_stage = new_threshold, stage
            updates.append((row.row_number, "익절기준", fraction_to_percent_str(new_threshold)))
            updates.append((row.row_number, "매도단계", str(stage)))
            logger.debug(
                "%s tick: rate=%.4f peak=%.4f threshold=%.4f purchase=%s KRW stage=%d action=%s",
                row.symbol, current_rate, new_peak, new_threshold, row.purchase_amount_krw, stage, action,
            )

            if sell_allowed and action == "FULL":
                client_order_id = f"{today_str}-{row.symbol}-EXIT"[:36]
                try:
                    executor.liquidate(row.symbol, client_order_id=client_order_id)
                except (TossApiError, OrderNotFilledError) as e:
                    # Do NOT mark liquidated on a failed/rejected sell -- the
                    # position is still real. Leave state untouched
                    # (sell_stage stays at `stage`, not `next_stage`) so the
                    # next tick's check retries the sell.
                    logger.warning("liquidation failed for %s, will retry next tick: %s", row.symbol, e)
                    return
                row.quantity = Decimal(0)
                row.purchase_amount_krw = Decimal(0)
                row.valuation_amount_krw = Decimal(0)
                row.profit_amount_krw = Decimal(0)
                row.liquidated = True
                row.sell_stage = next_stage
                updates.append((row.row_number, "보유수량", "0"))
                updates.append((row.row_number, "매입금액_원화", "0"))
                updates.append((row.row_number, "평가금액_원화", "0"))
                updates.append((row.row_number, "평가손익_원화", "0"))
                updates.append((row.row_number, "매도단계", str(next_stage)))
                updates.append((row.row_number, "청산여부", "TRUE"))
                updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))
                logger.info("LIQUIDATED(FULL) %s peak=%.4f rate=%.4f", row.symbol, new_peak, current_rate)
                return

            if sell_allowed and action == "PARTIAL":
                currency = "KRW" if row.market == Market.KR else "USD"
                client_order_id = f"{today_str}-{row.symbol}-STAGE{next_stage}"[:36]
                try:
                    result = executor.liquidate_partial(
                        row.symbol, row.market, currency, LIQUIDATION_STAGE_SELL_FRACTION, client_order_id=client_order_id
                    )
                except (TossApiError, OrderNotFilledError) as e:
                    # Same retry-next-tick contract as the FULL branch above.
                    logger.warning("partial liquidation failed for %s, will retry next tick: %s", row.symbol, e)
                    return
                if result is None:
                    # Computed sell quantity rounded down to zero (a
                    # whole-share KR position too small to split at this
                    # fraction) -- leave sell_stage as-is so a deeper stage
                    # can still fire once the drawdown gets there.
                    logger.debug("%s: partial sell at stage %d skipped, computed quantity rounded to 0", row.symbol, next_stage)
                else:
                    _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
                    row.sell_stage = next_stage
                    updates.append((row.row_number, "매도단계", str(next_stage)))
                    logger.info("PARTIAL SELL (stage %d) %s peak=%.4f rate=%.4f", next_stage, row.symbol, new_peak, current_rate)

    if not row.strategy_enabled:
        logger.debug("%s skipped: strategy_enabled=False", row.symbol)
        return

    if not buy_allowed:
        logger.debug("%s skipped: within buy delay of session open", row.symbol)
        return

    # Rule 4 (daily DCA buy): judged by success, not by attempt -- keeps
    # retrying (throttled to DAILY_BUY_RETRY_SECONDS) until an order actually
    # fills, rather than giving up for the day after a single failure.
    # Keyed by the session's trading day (not the raw KST calendar date) so
    # an overnight US session isn't double-counted across the midnight
    # boundary.
    buy_day_key = _trading_day_key(row.market, sessions, now)
    if not run_state.bought_today(buy_day_key, row.symbol):
        elapsed = run_state.seconds_since_last_buy_attempt(buy_day_key, row.symbol, now)
        if elapsed is None or elapsed >= DAILY_BUY_RETRY_SECONDS:
            run_state.mark_buy_attempt(buy_day_key, row.symbol, now)
            if _attempt_daily_buy(row, item, current_rate, executor, exchange_rate_usd_krw, buy_day_key, now, updates):
                run_state.mark_bought(buy_day_key, row.symbol)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _setup_logging(level: str) -> None:
    # Root stays at WARNING so LOG_LEVEL=DEBUG doesn't flood the log with
    # urllib3/google-auth connection-pool chatter; only our own "traildca"
    # logger follows the configured level.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                PROJECT_ROOT / "traildca.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
            ),
        ],
    )
    logger.setLevel(level)


def main() -> None:
    config = Config.load()
    _setup_logging(config.log_level)
    logger.info("starting TrailDCA (LIVE_TRADING=%s)", config.live_trading)
    if not config.live_trading:
        logger.warning("DRY-RUN mode: no real orders will be placed. Set LIVE_TRADING=true in .env to go live.")

    toss = TossClient(config.toss_client_id, config.toss_client_secret)
    accounts = toss.get_accounts()
    if config.toss_account_seq:
        account_seq: int | str = config.toss_account_seq
    else:
        if not accounts:
            raise RuntimeError("no brokerage accounts found for these credentials")
        account_seq = accounts[0]["accountSeq"]
    logger.info("using account_seq=%s", account_seq)

    sheets = SheetsClient(config.google_service_account_file, config.google_sheet_id, config.google_sheet_tab)
    run_state = RunState(STATE_FILE)
    executor = OrderExecutor(toss, account_seq, live=config.live_trading)

    sessions: MarketSessions | None = None
    exchange_rate_usd_krw = Decimal("1300")  # seed; refreshed before first real use below
    active_rows: list[SheetRow] = []
    last_fx_refresh = 0.0
    last_strategy_enabled_refresh = 0.0
    startup_synced = False
    # (row_number, column_name) -> latest value, accumulated across ticks and
    # flushed to Sheets every SHEET_FLUSH_INTERVAL_SECONDS instead of every
    # tick (see comment on SHEET_FLUSH_INTERVAL_SECONDS in config.py).
    pending_sheet_updates: dict[tuple[int, str], str] = {}
    last_sheet_flush = 0.0
    # Per-symbol 전략적용여부 as of the last tick it was observed -- lets
    # process_symbol detect the exact tick a row flips FALSE -> TRUE. Even
    # though 전략적용여부 itself is now refreshed every
    # STRATEGY_ENABLED_REFRESH_INTERVAL_SECONDS (see below), this still can't
    # be inferred from the SheetRow object alone: row.strategy_enabled is
    # mutated in place between ticks, so without this separate "last seen"
    # snapshot there'd be nothing to diff against.
    strategy_enabled_seen: dict[str, bool] = {}

    stop = {"flag": False}

    def _handle_signal(signum, frame):
        logger.info("shutdown signal received (%s); exiting after current tick", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop["flag"]:
        loop_start = time.monotonic()
        try:
            # Clock-skew-corrected time (see TossClient.now()) -- everything
            # that decides "is the market open" / "how long has it been open"
            # (is_market_open, _trading_day_key, the buy/sell open-delay in
            # process_symbol) flows from this one value.
            now = toss.now()
            today_str = now.date().isoformat()

            if sessions is None or (sessions.loaded_date != today_str and now.hour >= DAILY_SNAPSHOT_HOUR_KST):
                try:
                    rate_resp = toss.get_exchange_rate("USD", "KRW")
                    exchange_rate_usd_krw = Decimal(rate_resp["rate"])
                    last_fx_refresh = time.monotonic()
                    sessions = load_market_sessions(toss)
                    logger.info(
                        "sessions refreshed for %s: KR %s-%s / US %s-%s (USD/KRW=%s)",
                        today_str,
                        sessions.kr_start,
                        sessions.kr_end,
                        sessions.us_start,
                        sessions.us_end,
                        exchange_rate_usd_krw,
                    )
                except Exception:
                    logger.exception("failed to refresh market sessions / exchange rate; will retry next tick")

            # Sync on service startup (regardless of time) and once/day at/after
            # 08:00 KST thereafter -- startup_synced short-circuits the second
            # check on the same day a startup sync already covered it.
            if not startup_synced or (run_state.last_snapshot_date != today_str and now.hour >= DAILY_SNAPSHOT_HOUR_KST):
                try:
                    daily_snapshot(toss, sheets, account_seq, exchange_rate_usd_krw, sessions, now)
                    if now.weekday() == 0:  # Monday
                        _delete_liquidated_rows(sheets)
                    run_state.last_snapshot_date = today_str
                    run_state.save()
                    active_rows = sheets.read_rows()
                    startup_synced = True
                    last_strategy_enabled_refresh = time.monotonic()
                except Exception:
                    logger.exception("daily snapshot failed, will retry next tick")

            if not active_rows:
                try:
                    active_rows = sheets.read_rows()
                    last_strategy_enabled_refresh = time.monotonic()
                except Exception:
                    logger.exception("failed to read sheet rows; will retry next tick")

            # 전략적용여부 alone is re-read on this faster cadence (independent
            # of the once/day full row reload above) so a manual sheet toggle
            # takes effect within a minute. Only this one field is merged in
            # -- everything else (quantity/peak/threshold/stage/...) stays
            # whatever this process has been tracking live in memory, since
            # that's the source of truth intra-day and re-reading it here
            # could race with a not-yet-flushed pending_sheet_updates write.
            if active_rows and time.monotonic() - last_strategy_enabled_refresh >= STRATEGY_ENABLED_REFRESH_INTERVAL_SECONDS:
                try:
                    fresh_enabled = {r.symbol: r.strategy_enabled for r in sheets.read_rows()}
                    for row in active_rows:
                        if row.symbol in fresh_enabled:
                            row.strategy_enabled = fresh_enabled[row.symbol]
                except Exception:
                    logger.exception("periodic 전략적용여부 refresh failed, keeping previous values")
                last_strategy_enabled_refresh = time.monotonic()

            if time.monotonic() - last_fx_refresh > 60:
                try:
                    rate_resp = toss.get_exchange_rate("USD", "KRW")
                    exchange_rate_usd_krw = Decimal(rate_resp["rate"])
                except Exception:
                    logger.exception("periodic exchange rate refresh failed, keeping previous value")
                last_fx_refresh = time.monotonic()

            # Includes strategy_enabled=False rows too -- process_symbol still
            # mirrors 보유수량/매입금액_원화/마지막갱신 for those every tick,
            # it just skips the peak/threshold/buy logic for them.
            candidates = [r for r in active_rows if not r.liquidated]
            market_open_now = candidates and sessions and (
                is_market_open(sessions, Market.KR, now) or is_market_open(sessions, Market.US, now)
            )

            if market_open_now:
                items: dict[str, dict] = {}
                holdings_ok = True
                try:
                    holdings = toss.get_holdings(account_seq)
                    items = {i["symbol"]: i for i in holdings.get("items", [])}
                except Exception:
                    logger.exception("get_holdings failed this tick; skipping strategy processing")
                    holdings_ok = False

                if holdings_ok:
                    updates: list[tuple[int, str, str]] = []
                    for row in candidates:
                        was_strategy_enabled = strategy_enabled_seen.get(row.symbol, row.strategy_enabled)
                        try:
                            process_symbol(
                                row, items.get(row.symbol), sessions, now, run_state, today_str,
                                executor, exchange_rate_usd_krw, updates, was_strategy_enabled,
                            )
                        except Exception:
                            logger.exception("error processing %s; continuing with other symbols", row.symbol)
                        strategy_enabled_seen[row.symbol] = row.strategy_enabled
                    for row_number, column_name, value in updates:
                        pending_sheet_updates[(row_number, column_name)] = value

            # Flush accumulated cell updates on a slower cadence than the 1s
            # strategy tick to stay well under the Sheets API's 60
            # writes/minute/user quota. On failure, leave pending_sheet_updates
            # in place (freshest value per cell wins next round) and still
            # reset the flush clock so a persistent quota error can't cause a
            # tight retry loop -- the next scheduled flush picks it up.
            if pending_sheet_updates and time.monotonic() - last_sheet_flush >= SHEET_FLUSH_INTERVAL_SECONDS:
                try:
                    sheets.batch_write([(rn, col, val) for (rn, col), val in pending_sheet_updates.items()])
                    pending_sheet_updates.clear()
                except Exception:
                    logger.exception("sheet batch_write failed")
                last_sheet_flush = time.monotonic()
        except Exception:
            # Last-resort safety net: nothing above should reach here (each
            # step already has its own try/except), but a genuinely
            # unexpected error here must not kill the 24/7 process.
            logger.exception("unhandled error in main loop tick; continuing")

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, TICK_SECONDS - elapsed))

    if pending_sheet_updates:
        try:
            sheets.batch_write([(rn, col, val) for (rn, col), val in pending_sheet_updates.items()])
        except Exception:
            logger.exception("final sheet batch_write failed on shutdown")

    logger.info("stopped cleanly")


if __name__ == "__main__":
    main()
