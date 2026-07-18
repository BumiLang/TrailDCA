"""Google Sheets access via a service account (gspread + google-auth).

No Toss-specific libraries here on purpose -- this is a plain, standard
Python Sheets integration so it stays reusable outside this project.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import gspread
from google.oauth2.service_account import Credentials

from src.config import KST
from src.models import SHEET_COLUMNS, SheetRow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def fraction_to_percent_str(value: Decimal) -> str:
    """0.1077 -> '10.77'. Used for all rate columns, which are stored as
    human-readable percentages in the sheet (spec examples: '최고수익률 = 0%')."""
    pct = (value * 100).quantize(Decimal("0.0001"))
    text = format(pct.normalize(), "f")
    return text


class SheetsClient:
    def __init__(self, service_account_file: str, sheet_id: str, tab_name: str):
        creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)
        try:
            self._ws = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            self._ws = spreadsheet.add_worksheet(title=tab_name, rows=200, cols=len(SHEET_COLUMNS))
        self._ensure_header()

    def _ensure_header(self) -> None:
        first_row = self._ws.row_values(1)
        if first_row != SHEET_COLUMNS:
            self._ws.update([SHEET_COLUMNS], "A1")

    @staticmethod
    def _col(name: str) -> int:
        return SHEET_COLUMNS.index(name) + 1

    def read_rows(self) -> list[SheetRow]:
        # numericise_ignore=['all']: keep every cell as a raw string. Otherwise
        # gspread auto-converts numeric-looking cells to int/float, which would
        # both corrupt KR symbol codes like "005930" -> 5930 (leading zero lost)
        # and turn rate/amount fields into lossy binary floats before we ever
        # get a chance to parse them as Decimal.
        records = self._ws.get_all_records(expected_headers=SHEET_COLUMNS, numericise_ignore=["all"])
        return [SheetRow.from_record(i + 2, record) for i, record in enumerate(records)]

    def append_default_row(
        self,
        symbol: str,
        name: str,
        market: str,
        quantity: str = "0",
        purchase_amount_krw: str = "0",
        profit_rate_pct: str = "0",
    ) -> None:
        """New symbol discovered in broker holdings but absent from the sheet.
        Per spec 2.3, only the strategy-bookkeeping fields get hard defaults
        (전략적용여부/최고수익률/익절기준/청산여부); 보유수량/매입금액/수익률
        reflect the real holdings just fetched in the same daily snapshot (spec 2.2)."""
        now = dt.datetime.now(KST).isoformat(timespec="seconds")
        row = {
            "종목코드": symbol,
            "종목명": name,
            "마켓구분": market,
            "보유수량": quantity,
            "매입금액_원화": purchase_amount_krw,
            "수익률": profit_rate_pct,
            "전략적용여부": "TRUE",
            "최고수익률": "0",
            "익절기준": "-100",
            "청산여부": "FALSE",
            "소수점가능여부": "",
            "마지막갱신": now,
        }
        # value_input_option="RAW" is deliberate: "USER_ENTERED" makes Sheets
        # parse cells the way the UI would, which silently strips the leading
        # zero off KR symbol codes like "005930" -> 5930. Everything is parsed
        # back into Decimal/str on our side anyway, so plain text is safest.
        self._ws.append_row([row[c] for c in SHEET_COLUMNS], value_input_option="RAW")

    def batch_write(self, cell_updates: list[tuple[int, str, str]]) -> None:
        """cell_updates: list of (row_number, column_name, raw_value). Emits a
        single batch_update API call regardless of how many symbols/cells
        changed this tick, to stay well within Sheets API write quotas."""
        if not cell_updates:
            return
        data = []
        for row_number, column_name, value in cell_updates:
            a1 = gspread.utils.rowcol_to_a1(row_number, self._col(column_name))
            data.append({"range": a1, "values": [[value]]})
        self._ws.batch_update(data, value_input_option="RAW")

    def touch_last_updated(self, row_number: int) -> tuple[int, str, str]:
        now = dt.datetime.now(KST).isoformat(timespec="seconds")
        return (row_number, "마지막갱신", now)
