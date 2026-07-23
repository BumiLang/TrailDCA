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
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src import strategy
from src.config import DAILY_SNAPSHOT_HOUR_KST, KST, PROJECT_ROOT, STATE_FILE, TICK_SECONDS, Config
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
        # tracks the once/day DCA buy attempt (rule 4)
        self.daily_buys: dict[str, list[str]] = {}
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

    def save(self) -> None:
        payload = json.dumps(
            {
                "last_snapshot_date": self.last_snapshot_date,
                "daily_buys": self.daily_buys,
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
    def _prune(buys: dict[str, list[str]], keep: int = 3) -> None:
        for old_date in sorted(buys)[:-keep] if len(buys) > keep else []:
            del buys[old_date]

    def bought_today(self, date_str: str, symbol: str) -> bool:
        return symbol in self.daily_buys.get(date_str, [])

    def mark_bought(self, date_str: str, symbol: str) -> None:
        self.daily_buys.setdefault(date_str, []).append(symbol)
        self._prune(self.daily_buys)
        self.save()


# ---------------------------------------------------------------------------
# Market sessions (regular-hours windows), refreshed once/day at/after 08:00
# KST -- late enough that the previous US regular session (which can spill
# past midnight KST) has already ended.
# ---------------------------------------------------------------------------


@dataclass
class MarketSessions:
    loaded_date: str
    kr_start: dt.datetime | None
    kr_end: dt.datetime | None
    us_start: dt.datetime | None
    us_end: dt.datetime | None


def _parse_session(session: dict | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    if not session:
        return None, None
    return dt.datetime.fromisoformat(session["startTime"]), dt.datetime.fromisoformat(session["endTime"])


def load_market_sessions(toss: TossClient) -> MarketSessions:
    today = dt.datetime.now(KST).date().isoformat()
    kr = toss.get_market_calendar("KR")
    us = toss.get_market_calendar("US")

    kr_integrated = (kr.get("today") or {}).get("integrated") or {}
    kr_start, kr_end = _parse_session(kr_integrated.get("regularMarket"))
    us_start, us_end = _parse_session((us.get("today") or {}).get("regularMarket"))

    return MarketSessions(today, kr_start, kr_end, us_start, us_end)


def is_market_open(sessions: MarketSessions | None, market: Market, now: dt.datetime) -> bool:
    if sessions is None:
        return False
    start, end = (sessions.kr_start, sessions.kr_end) if market == Market.KR else (sessions.us_start, sessions.us_end)
    if start is None or end is None:
        return False
    return start <= now < end


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
        price = self._current_price(symbol)
        sim = self._sim[symbol]
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
            final = self._toss.wait_for_terminal_status(self._account_seq, order["orderId"])
            if final.get("status") != "FILLED":
                raise OrderNotFilledError(final)
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


def _attempt_daily_buy(
    row: SheetRow,
    item: dict | None,
    current_rate: Decimal,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    today_str: str,
    now: dt.datetime,
    updates: list,
) -> None:
    """Rule 4 (unified, market-agnostic): once/day DCA buy.

    - purchase_amount_krw < 100,000: buy 5,000 KRW worth.
    - purchase_amount_krw >= 100,000 and current_rate >= 10%: buy 5,000 KRW worth.
    - otherwise: no buy today.

    The 5,000 KRW order is placed as an amount order first (this only works
    for symbols/brokers that support fractional shares). If that order
    errors out for any reason, fall back to buying a single whole share --
    the amount-buy's eligibility condition already holds, so the fallback
    doesn't need to re-check it.
    """
    purchase_krw = _purchase_amount_krw(item, exchange_rate_usd_krw) if item else row.purchase_amount_krw
    amount_krw = strategy.daily_buy_amount_krw(purchase_krw, current_rate)
    if amount_krw is None:
        logger.debug(
            "%s: no buy today (purchase=%s KRW, rate %.4f below 10%% resume bar)",
            row.symbol, purchase_krw, current_rate,
        )
        return

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
    except (TossApiError, OrderNotFilledError) as e:
        logger.info("%s: amount buy failed (%s); falling back to 1-share buy", row.symbol, e)
        _attempt_fallback_share_buy(row, item, executor, exchange_rate_usd_krw, today_str, now, updates)
        return

    _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
    logger.info("BUY(amount) %s target=%sKRW (%s%s)", row.symbol, amount_krw, order_amount, currency)


def _attempt_fallback_share_buy(
    row: SheetRow,
    item: dict | None,
    executor: OrderExecutor,
    exchange_rate_usd_krw: Decimal,
    today_str: str,
    now: dt.datetime,
    updates: list,
) -> None:
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
        logger.warning("fallback 1-share buy also failed for %s; will retry tomorrow: %s", row.symbol, e)
        return

    new_rate = Decimal(result["profitLoss"]["rate"])
    row.peak_rate = strategy.peak_after_share_buy(new_rate)
    updates.append((row.row_number, "최고수익률", fraction_to_percent_str(row.peak_rate)))
    _apply_trade_result(row, result, exchange_rate_usd_krw, now, updates)
    logger.info("BUY(1 share fallback) %s new_rate=%.4f", row.symbol, new_rate)


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
) -> None:
    if not row.strategy_enabled or row.liquidated:
        logger.debug("%s skipped: strategy_enabled=%s liquidated=%s", row.symbol, row.strategy_enabled, row.liquidated)
        return
    if not is_market_open(sessions, row.market, now):
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
            client_order_id = f"{today_str}-{row.symbol}-EXIT"[:36]
            try:
                executor.liquidate(row.symbol, client_order_id=client_order_id)
            except (TossApiError, OrderNotFilledError) as e:
                # Do NOT mark liquidated on a failed/rejected sell -- the
                # position is still real. Leave state untouched so the next
                # tick's should_liquidate check retries the sell.
                logger.warning("liquidation failed for %s, will retry next tick: %s", row.symbol, e)
                return
            row.quantity = Decimal(0)
            row.purchase_amount_krw = Decimal(0)
            row.liquidated = True
            updates.append((row.row_number, "보유수량", "0"))
            updates.append((row.row_number, "매입금액_원화", "0"))
            updates.append((row.row_number, "청산여부", "TRUE"))
            updates.append((row.row_number, "마지막갱신", now.isoformat(timespec="seconds")))
            logger.info("LIQUIDATED %s peak=%.4f rate=%.4f threshold=%.4f", row.symbol, new_peak, current_rate, new_threshold)
            return

    # Rule 4 (daily DCA buy): fixed target amount, doesn't depend on intraday
    # rate movement beyond the once/day snapshot, so a once/day attempt is
    # sufficient. Marked attempted regardless of outcome to avoid retry storms.
    if not run_state.bought_today(today_str, row.symbol):
        run_state.mark_bought(today_str, row.symbol)
        _attempt_daily_buy(row, item, current_rate, executor, exchange_rate_usd_krw, today_str, now, updates)


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

    stop = {"flag": False}

    def _handle_signal(signum, frame):
        logger.info("shutdown signal received (%s); exiting after current tick", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop["flag"]:
        loop_start = time.monotonic()
        try:
            now = dt.datetime.now(KST)
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

            if run_state.last_snapshot_date != today_str and now.hour >= DAILY_SNAPSHOT_HOUR_KST:
                try:
                    daily_snapshot(toss, sheets, account_seq, exchange_rate_usd_krw)
                    run_state.last_snapshot_date = today_str
                    run_state.save()
                    active_rows = sheets.read_rows()
                except Exception:
                    logger.exception("daily snapshot failed, will retry next tick")

            if not active_rows:
                try:
                    active_rows = sheets.read_rows()
                except Exception:
                    logger.exception("failed to read sheet rows; will retry next tick")

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
                            process_symbol(row, items.get(row.symbol), sessions, now, run_state, today_str, executor, exchange_rate_usd_krw, updates)
                        except Exception:
                            logger.exception("error processing %s; continuing with other symbols", row.symbol)
                    if updates:
                        try:
                            sheets.batch_write(updates)
                        except Exception:
                            logger.exception("sheet batch_write failed")
        except Exception:
            # Last-resort safety net: nothing above should reach here (each
            # step already has its own try/except), but a genuinely
            # unexpected error here must not kill the 24/7 process.
            logger.exception("unhandled error in main loop tick; continuing")

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, TICK_SECONDS - elapsed))

    logger.info("stopped cleanly")


if __name__ == "__main__":
    main()
