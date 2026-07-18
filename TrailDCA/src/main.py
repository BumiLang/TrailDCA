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
import signal
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from src import strategy
from src.config import DAILY_SNAPSHOT_HOUR_KST, KST, PROJECT_ROOT, STATE_FILE, TICK_SECONDS, Config
from src.models import FractionalStatus, Market, SheetRow
from src.sheets_client import SheetsClient, fraction_to_percent_str
from src.toss_client import TossApiError, TossClient

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
        self.daily_buys: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.last_snapshot_date = data.get("last_snapshot_date")
            self.daily_buys = data.get("daily_buys", {})

    def save(self) -> None:
        self._path.write_text(
            json.dumps(
                {"last_snapshot_date": self.last_snapshot_date, "daily_buys": self.daily_buys},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def bought_today(self, date_str: str, symbol: str) -> bool:
        return symbol in self.daily_buys.get(date_str, [])

    def mark_bought(self, date_str: str, symbol: str) -> None:
        self.daily_buys.setdefault(date_str, []).append(symbol)
        for old_date in [d for d in self.daily_buys if d < date_str]:
            if len(self.daily_buys) > 3:
                del self.daily_buys[old_date]
        self.save()


# ---------------------------------------------------------------------------
# Market sessions (regular-hours windows), refreshed once/day at/after 08:00
# KST. We keep both "today" and "previousBusinessDay" windows from the Toss
# calendar response -- the US regular session spans past midnight KST, so a
# process that (re)starts between 00:00 and the session's actual end must
# still recognize it as open even though the calendar's "today" has no
# session of its own (e.g. Saturday).
# ---------------------------------------------------------------------------


@dataclass
class MarketSessions:
    loaded_date: str
    kr_windows: list[tuple[dt.datetime, dt.datetime]]
    us_windows: list[tuple[dt.datetime, dt.datetime]]


def _parse_session(session: dict | None) -> tuple[dt.datetime, dt.datetime] | None:
    if not session:
        return None
    return dt.datetime.fromisoformat(session["startTime"]), dt.datetime.fromisoformat(session["endTime"])


def _regular_market_windows(calendar: dict, integrated: bool) -> list[tuple[dt.datetime, dt.datetime]]:
    windows = []
    for key in ("previousBusinessDay", "today"):
        day = calendar.get(key) or {}
        regular = (day.get("integrated") or {}).get("regularMarket") if integrated else day.get("regularMarket")
        window = _parse_session(regular)
        if window:
            windows.append(window)
    return windows


def load_market_sessions(toss: TossClient) -> MarketSessions:
    today = dt.datetime.now(KST).date().isoformat()
    kr = toss.get_market_calendar("KR")
    us = toss.get_market_calendar("US")

    kr_windows = _regular_market_windows(kr, integrated=True)
    us_windows = _regular_market_windows(us, integrated=False)

    return MarketSessions(today, kr_windows, us_windows)


def _active_session_day(sessions: MarketSessions | None, market: Market, now: dt.datetime) -> str | None:
    """isoformat date of the regular-market window covering `now`, or None if
    the market is closed. For the US session (spans past midnight KST) this
    is the window's *start* date, so a continuous overnight session keeps a
    stable "trading day" key even after the KST calendar date rolls over --
    used to dedupe the once-per-day buy trigger across that rollover.
    """
    if sessions is None:
        return None
    windows = sessions.kr_windows if market == Market.KR else sessions.us_windows
    for start, end in windows:
        if start <= now < end:
            return start.date().isoformat()
    return None


def is_market_open(sessions: MarketSessions | None, market: Market, now: dt.datetime) -> bool:
    return _active_session_day(sessions, market, now) is not None


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

    def _current_price(self, symbol: str) -> Decimal:
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
            self._toss.wait_for_terminal_status(self._account_seq, order["orderId"])
            holdings = self._toss.get_holdings(self._account_seq, symbol=symbol)
            items = holdings.get("items", [])
            if not items:
                raise RuntimeError(f"buy order for {symbol} settled but holdings lookup returned nothing")
            return items[0]

        # dry-run: never calls place_order. Simulated against the latest
        # real price so logs/sheet stay directionally meaningful.
        price = self._current_price(symbol)
        sim = self._sim.setdefault(symbol, {"quantity": Decimal(0), "purchase_amount": Decimal(0)})
        if order_amount is not None:
            bought_qty = order_amount / price
            cost = order_amount
        else:
            bought_qty = quantity
            cost = quantity * price
        sim["quantity"] += bought_qty
        sim["purchase_amount"] += cost
        rate = (sim["quantity"] * price - sim["purchase_amount"]) / sim["purchase_amount"]
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
            "marketValue": {"purchaseAmount": str(sim["purchase_amount"])},
            "profitLoss": {"rate": str(rate)},
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
            self._toss.wait_for_terminal_status(self._account_seq, order["orderId"])
        else:
            sim = self._sim.pop(symbol, None)
            logger.info("[DRY-RUN] SELL(all) %s qty=%s", symbol, sim["quantity"] if sim else "0")


# ---------------------------------------------------------------------------
# Daily snapshot (spec 1 + 2): pull real holdings, reconcile into the sheet.
# ---------------------------------------------------------------------------


def _purchase_amount_krw(item: dict, exchange_rate_usd_krw: Decimal) -> Decimal:
    amount = Decimal(item["marketValue"]["purchaseAmount"])
    if item.get("currency") == "KRW":
        return amount
    return amount * exchange_rate_usd_krw


def daily_snapshot(
    toss: TossClient, sheets: SheetsClient, account_seq: int | str, exchange_rate_usd_krw: Decimal
) -> None:
    holdings = toss.get_holdings(account_seq)
    items = holdings.get("items", [])
    existing = {r.symbol: r for r in sheets.read_rows()}

    updates: list[tuple[int, str, str]] = []
    now_iso = dt.datetime.now(KST).isoformat(timespec="seconds")

    for item in items:
        symbol = item["symbol"]
        quantity = Decimal(item["quantity"])
        rate = Decimal(item["profitLoss"]["rate"])
        purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)

        if symbol in existing:
            row = existing[symbol]
            updates.append((row.row_number, "보유수량", str(quantity)))
            updates.append((row.row_number, "매입금액_원화", str(purchase_krw.quantize(Decimal("1")))))
            updates.append((row.row_number, "수익률", fraction_to_percent_str(rate)))
            updates.append((row.row_number, "마지막갱신", now_iso))

            # Also refresh 최고수익률/익절기준 here (not just during the market-hours
            # tick loop) so they don't sit stale from yesterday's close until the
            # market reopens. Only the *values* are recomputed -- liquidation is
            # never executed outside the tick loop, since trading is only allowed
            # during that symbol's regular market hours.
            if row.strategy_enabled and not row.liquidated:
                new_peak, new_threshold = strategy.update_peak_and_threshold(
                    row.peak_rate, rate, row.take_profit_threshold
                )
                updates.append((row.row_number, "최고수익률", fraction_to_percent_str(new_peak)))
                updates.append((row.row_number, "익절기준", fraction_to_percent_str(new_threshold)))
        else:
            sheets.append_default_row(
                symbol,
                item.get("name", ""),
                item.get("marketCountry", "KR"),
                quantity=str(quantity),
                purchase_amount_krw=str(purchase_krw.quantize(Decimal("1"))),
                profit_rate_pct=fraction_to_percent_str(rate),
            )
            logger.info("new holding discovered, added to sheet: %s", symbol)

    sheets.batch_write(updates)
    logger.info("daily snapshot reconciled %d holdings", len(items))


# ---------------------------------------------------------------------------
# 1-second tick: peak/threshold/liquidation + once-per-day buy trigger.
# ---------------------------------------------------------------------------


def _apply_trade_result(row: SheetRow, item: dict, exchange_rate_usd_krw: Decimal, now: dt.datetime, updates: list) -> None:
    row.quantity = Decimal(item["quantity"])
    row.purchase_amount_krw = _purchase_amount_krw(item, exchange_rate_usd_krw)
    row.profit_rate = Decimal(item["profitLoss"]["rate"])
    updates.append((row.row_number, "보유수량", str(row.quantity)))
    updates.append((row.row_number, "매입금액_원화", str(row.purchase_amount_krw.quantize(Decimal("1")))))
    updates.append((row.row_number, "수익률", fraction_to_percent_str(row.profit_rate)))
    updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))


def _is_fractional_unsupported_error(err: TossApiError) -> bool:
    return err.code == "stock-restricted" and "소수" in (err.message or "")


def _attempt_daily_buy(
    row: SheetRow,
    item: dict | None,
    current_rate: Decimal,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    session_day: str,
    now: dt.datetime,
    updates: list,
) -> None:
    held_qty = Decimal(item["quantity"]) if item else Decimal(0)
    purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw) if item else row.purchase_amount_krw

    try_fractional = row.market == Market.US and row.fractional_status != FractionalStatus.NO
    logger.debug(
        "%s daily-buy check: held_qty=%s purchase_krw=%s rate=%.4f try_fractional=%s fractional_status=%s",
        row.symbol, held_qty, purchase_krw, current_rate, try_fractional, row.fractional_status,
    )

    if try_fractional:
        amount_krw = strategy.fractional_daily_buy_amount_krw(purchase_krw, current_rate)
        if amount_krw is None:
            logger.debug(
                "%s: no fractional buy today (target reached at %s KRW, rate %.4f not > 10%%)",
                row.symbol, purchase_krw, current_rate,
            )
            return  # target reached, profit <=10%: no buy today (rule 4.2)

        order_amount_usd = (amount_krw / exchange_rate_usd_krw).quantize(Decimal("0.01"))
        client_order_id = f"{session_day}-{row.symbol}-DCA"[:36]
        try:
            result = executor.buy(
                row.symbol, "USD", "MARKET", order_amount=order_amount_usd, client_order_id=client_order_id
            )
            if row.fractional_status == FractionalStatus.UNKNOWN:
                row.fractional_status = FractionalStatus.YES
                updates.append((row.row_number, "소수점가능여부", "TRUE"))
            _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
            logger.info("BUY(amount) %s target=%sKRW (%sUSD)", row.symbol, amount_krw, order_amount_usd)
            return
        except TossApiError as e:
            if _is_fractional_unsupported_error(e):
                row.fractional_status = FractionalStatus.NO
                updates.append((row.row_number, "소수점가능여부", "FALSE"))
                logger.info("%s: fractional not supported (%s) - falling back to whole-share rule today", row.symbol, e.message)
                # fall through to the non-fractional branch below, same day
            else:
                logger.warning("buy failed for %s: %s", row.symbol, e)
                return

    # non-fractional path: KR always, or US once confirmed non-fractional / same-day fallback
    if strategy.nonfractional_should_buy(held_qty, current_rate):
        client_order_id = f"{session_day}-{row.symbol}-ADD"[:36]
        currency = "KRW" if row.market == Market.KR else "USD"
        result = executor.buy(row.symbol, currency, "MARKET", quantity=Decimal(1), client_order_id=client_order_id)
        new_rate = Decimal(result["profitLoss"]["rate"])
        row.peak_rate = strategy.peak_after_nonfractional_buy(new_rate)
        updates.append((row.row_number, "최고수익률", fraction_to_percent_str(row.peak_rate)))
        _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
        logger.info("BUY(1 share) %s new_rate=%.4f", row.symbol, new_rate)
    else:
        required = strategy.nonfractional_required_rate(held_qty) if held_qty >= 1 else None
        logger.debug(
            "%s: no whole-share buy today (held_qty=%s rate=%.4f required=%s)",
            row.symbol, held_qty, current_rate, required,
        )


def process_symbol(
    row: SheetRow,
    item: dict | None,
    sessions: MarketSessions,
    now: dt.datetime,
    run_state: RunState,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    updates: list,
) -> None:
    if not row.strategy_enabled or row.liquidated:
        logger.debug("%s skipped: strategy_enabled=%s liquidated=%s", row.symbol, row.strategy_enabled, row.liquidated)
        return
    session_day = _active_session_day(sessions, row.market, now)
    if session_day is None:
        logger.debug("%s skipped: %s market closed", row.symbol, row.market.value)
        return

    held = item is not None
    current_rate = Decimal(item["profitLoss"]["rate"]) if held else Decimal(0)

    if held:
        new_peak, new_threshold = strategy.update_peak_and_threshold(row.peak_rate, current_rate, row.take_profit_threshold)
        row.peak_rate, row.take_profit_threshold, row.profit_rate = new_peak, new_threshold, current_rate
        updates.append((row.row_number, "수익률", fraction_to_percent_str(current_rate)))
        updates.append((row.row_number, "최고수익률", fraction_to_percent_str(new_peak)))
        updates.append((row.row_number, "익절기준", fraction_to_percent_str(new_threshold)))
        logger.debug(
            "%s tick: rate=%.4f peak=%.4f threshold=%.4f liquidate=%s",
            row.symbol, current_rate, new_peak, new_threshold,
            strategy.should_liquidate(new_peak, current_rate, new_threshold),
        )

        if strategy.should_liquidate(new_peak, current_rate, new_threshold):
            client_order_id = f"{session_day}-{row.symbol}-EXIT"[:36]
            executor.liquidate(row.symbol, client_order_id=client_order_id)
            row.quantity = Decimal(0)
            row.purchase_amount_krw = Decimal(0)
            row.liquidated = True
            updates.append((row.row_number, "보유수량", "0"))
            updates.append((row.row_number, "매입금액_원화", "0"))
            updates.append((row.row_number, "청산여부", "TRUE"))
            updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))
            logger.info("LIQUIDATED %s peak=%.4f rate=%.4f threshold=%.4f", row.symbol, new_peak, current_rate, new_threshold)
            return

    if run_state.bought_today(session_day, row.symbol):
        return
    run_state.mark_bought(session_day, row.symbol)  # mark attempted regardless of outcome; avoids retry storms on error
    _attempt_daily_buy(row, item, current_rate, executor, exchange_rate_usd_krw, session_day, now, updates)


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
            logging.FileHandler(PROJECT_ROOT / "traildca.log", encoding="utf-8"),
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
    try:
        exchange_rate_usd_krw = Decimal(toss.get_exchange_rate("USD", "KRW")["rate"])
    except Exception:
        exchange_rate_usd_krw = Decimal("1300")
        logger.exception("failed to fetch initial USD/KRW rate; using fallback %s", exchange_rate_usd_krw)
    last_fx_refresh = time.monotonic()

    # Startup refresh: sync real holdings and recompute 최고수익률/익절기준 right
    # away, independent of the 08:00 KST daily-snapshot gate below, so a
    # mid-day restart doesn't leave the sheet showing stale peak/threshold
    # values until the next tick.
    logger.info("running startup snapshot (holdings + 최고수익률/익절기준 refresh)")
    try:
        daily_snapshot(toss, sheets, account_seq, exchange_rate_usd_krw)
    except Exception:
        logger.exception("startup snapshot failed; will retry at the next daily snapshot window")
    active_rows: list[SheetRow] = sheets.read_rows()

    stop = {"flag": False}

    def _handle_signal(signum, frame):
        logger.info("shutdown signal received (%s); exiting after current tick", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop["flag"]:
        loop_start = time.monotonic()
        now = dt.datetime.now(KST)
        today_str = now.date().isoformat()

        if sessions is None or (sessions.loaded_date != today_str and now.hour >= DAILY_SNAPSHOT_HOUR_KST):
            try:
                rate_resp = toss.get_exchange_rate("USD", "KRW")
                exchange_rate_usd_krw = Decimal(rate_resp["rate"])
                last_fx_refresh = time.monotonic()
                sessions = load_market_sessions(toss)
                logger.info(
                    "sessions refreshed for %s: KR %s / US %s (USD/KRW=%s)",
                    today_str,
                    sessions.kr_windows,
                    sessions.us_windows,
                    exchange_rate_usd_krw,
                )
            except Exception:
                logger.exception("failed to refresh market sessions / exchange rate; will retry next tick")

        if run_state.last_snapshot_date != today_str and now.hour >= DAILY_SNAPSHOT_HOUR_KST:
            try:
                daily_snapshot(toss, sheets, account_seq, exchange_rate_usd_krw)
                run_state.last_snapshot_date = today_str
                run_state.save()
                active_rows = sheets.read_rows()
            except Exception:
                logger.exception("daily snapshot failed, will retry next tick")

        if not active_rows:
            active_rows = sheets.read_rows()

        if time.monotonic() - last_fx_refresh > 60:
            try:
                rate_resp = toss.get_exchange_rate("USD", "KRW")
                exchange_rate_usd_krw = Decimal(rate_resp["rate"])
            except Exception:
                logger.exception("periodic exchange rate refresh failed, keeping previous value")
            last_fx_refresh = time.monotonic()

        candidates = [r for r in active_rows if r.strategy_enabled and not r.liquidated]
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
                    try:
                        process_symbol(row, items.get(row.symbol), sessions, now, run_state, executor, exchange_rate_usd_krw, updates)
                    except Exception:
                        logger.exception("error processing %s; continuing with other symbols", row.symbol)
                if updates:
                    try:
                        sheets.batch_write(updates)
                    except Exception:
                        logger.exception("sheet batch_write failed")

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, TICK_SECONDS - elapsed))

    logger.info("stopped cleanly")


if __name__ == "__main__":
    main()
