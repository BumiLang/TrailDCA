"""Thin wrapper around the Toss Securities OpenAPI (https://openapi.tossinvest.com).

Reference: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from decimal import Decimal
from typing import Any

import requests

import src  # noqa: F401  (ensures the package-level truststore injection has run)
from src.config import KST

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.tossinvest.com"

# Requests/second per rate-limit group. Groups not listed here fall back to
# DEFAULT_RATE. ORDER has a lower peak rate during the 09:00-09:10 KST rush.
GROUP_RATES = {
    "AUTH": 5,
    "MARKET_DATA": 10,
    "ORDER": 6,
    "ACCOUNT": 1,
    "ASSET": 5,
}
ORDER_PEAK_RATE = 3
DEFAULT_RATE = 5


def _dec_str(value: Decimal | str | int) -> str:
    """Render a Decimal as a plain (non-scientific) string for request bodies."""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


class TossApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, data: dict | None = None, request_id: str | None = None):
        super().__init__(f"[{status_code} {code}] {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data or {}
        self.request_id = request_id


class _RateLimiter:
    """Simple per-group leaky-bucket limiter: blocks the caller just long
    enough to keep calls within the group's requests/second budget."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def _rate_for(self, group: str) -> int:
        if group == "ORDER":
            now_kst = dt.datetime.now(KST).time()
            if dt.time(9, 0) <= now_kst < dt.time(9, 10):
                return ORDER_PEAK_RATE
        return GROUP_RATES.get(group, DEFAULT_RATE)

    def acquire(self, group: str) -> None:
        interval = 1.0 / self._rate_for(group)
        with self._lock:
            now = time.monotonic()
            next_allowed = self._next_allowed.get(group, 0.0)
            wait = max(0.0, next_allowed - now)
            self._next_allowed[group] = max(now, next_allowed) + interval
        if wait > 0:
            time.sleep(wait)


class TossClient:
    def __init__(self, client_id: str, client_secret: str, timeout: float = 10.0):
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._session = requests.Session()
        self._limiter = _RateLimiter()

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- auth ----

    def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return self._access_token

        self._limiter.acquire("AUTH")
        resp = self._session.post(
            f"{BASE_URL}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        return self._access_token

    @staticmethod
    def _raise_for_error(resp: requests.Response) -> None:
        try:
            body = resp.json()
            error = body.get("error", {})
        except ValueError:
            error = {}
        raise TossApiError(
            status_code=resp.status_code,
            code=error.get("code", "unknown"),
            message=error.get("message", resp.text[:500]),
            data=error.get("data"),
            request_id=error.get("requestId"),
        )

    # ---- transport ----

    def _request(
        self,
        method: str,
        path: str,
        group: str,
        account_seq: int | str | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)

        self._limiter.acquire(group)
        resp = self._session.request(
            method,
            f"{BASE_URL}{path}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        body = resp.json()
        return body.get("result")

    # ---- account / holdings ----

    def get_accounts(self) -> list[dict]:
        return self._request("GET", "/api/v1/accounts", group="ACCOUNT")

    def get_holdings(self, account_seq: int | str, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        return self._request(
            "GET", "/api/v1/holdings", group="ASSET", account_seq=account_seq, params=params
        )

    # ---- market info ----

    def get_market_calendar(self, market: str, date: str | None = None) -> dict:
        assert market in ("KR", "US")
        params = {"date": date} if date else None
        return self._request(
            "GET", f"/api/v1/market-calendar/{market}", group="MARKET_INFO", params=params
        )

    def get_exchange_rate(self, base_currency: str, quote_currency: str) -> dict:
        return self._request(
            "GET",
            "/api/v1/exchange-rate",
            group="MARKET_INFO",
            params={"baseCurrency": base_currency, "quoteCurrency": quote_currency},
        )

    def get_prices(self, symbols: list[str]) -> list[dict]:
        return self._request(
            "GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": ",".join(symbols)}
        )

    # ---- order info ----

    def get_buying_power(self, account_seq: int | str, currency: str) -> dict:
        return self._request(
            "GET",
            "/api/v1/buying-power",
            group="ORDER_INFO",
            account_seq=account_seq,
            params={"currency": currency},
        )

    def get_sellable_quantity(self, account_seq: int | str, symbol: str) -> dict:
        return self._request(
            "GET",
            "/api/v1/sellable-quantity",
            group="ORDER_INFO",
            account_seq=account_seq,
            params={"symbol": symbol},
        )

    # ---- orders ----

    def place_order(
        self,
        account_seq: int | str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | None = None,
        order_amount: Decimal | None = None,
        price: Decimal | None = None,
        client_order_id: str | None = None,
        confirm_high_value_order: bool = False,
    ) -> dict:
        assert (quantity is None) != (order_amount is None), "specify exactly one of quantity/order_amount"
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "confirmHighValueOrder": confirm_high_value_order,
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id
        if quantity is not None:
            body["quantity"] = _dec_str(quantity)
        if order_amount is not None:
            body["orderAmount"] = _dec_str(order_amount)
        if price is not None:
            body["price"] = _dec_str(price)

        return self._request(
            "POST", "/api/v1/orders", group="ORDER", account_seq=account_seq, json_body=body
        )

    def get_order(self, account_seq: int | str, order_id: str) -> dict:
        return self._request(
            "GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY", account_seq=account_seq
        )

    def list_open_orders(self, account_seq: int | str, symbol: str | None = None) -> dict:
        params: dict[str, Any] = {"status": "OPEN"}
        if symbol:
            params["symbol"] = symbol
        return self._request(
            "GET", "/api/v1/orders", group="ORDER_HISTORY", account_seq=account_seq, params=params
        )

    def cancel_order(self, account_seq: int | str, order_id: str) -> dict:
        return self._request(
            "POST",
            f"/api/v1/orders/{order_id}/cancel",
            group="ORDER",
            account_seq=account_seq,
            json_body={},
        )

    # ---- polling helper ----

    def wait_for_terminal_status(
        self, account_seq: int | str, order_id: str, timeout: float = 30.0, poll_interval: float = 1.0
    ) -> dict:
        """Poll GET /orders/{orderId} until a terminal status or timeout."""
        terminal = {"FILLED", "CANCELED", "REJECTED", "CANCEL_REJECTED", "REPLACE_REJECTED"}
        deadline = time.monotonic() + timeout
        order = self.get_order(account_seq, order_id)
        while order.get("status") not in terminal and time.monotonic() < deadline:
            time.sleep(poll_interval)
            order = self.get_order(account_seq, order_id)
        return order
