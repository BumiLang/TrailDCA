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

# Columns whose values are already percentages in "human" form (10.77 means
# 10.77%, not 0.1077) -- displayed with a literal "%" suffix via cell number
# format only. The custom pattern's quoted "%" is deliberate: Sheets' built-in
# PERCENT type would additionally multiply the stored value by 100, which
# would be wrong here since it's already scaled.
PERCENT_COLUMNS = ["수익률", "최고수익률", "익절기준"]
# KRW amount columns -- displayed with thousands separators.
AMOUNT_COLUMNS = ["매입금액_원화", "평가금액_원화", "평가손익_원화"]
# Columns that must be written as USER_ENTERED so Sheets coerces the value to
# a real NUMBER-typed cell (RAW leaves numeric-looking strings as TEXT, which
# silently drops any numberFormat -- the "%"/"," suffix disappears even
# though the underlying value is unchanged).
NUMERIC_FORMAT_COLUMNS = PERCENT_COLUMNS + AMOUNT_COLUMNS
PERCENT_FORMAT_ROWS = 2000  # comfortably above any realistic symbol count


def fraction_to_percent_str(value: Decimal) -> str:
    """0.1077 -> '10.77'. Used for all rate columns, which are stored as
    human-readable percentages in the sheet (spec examples: '최고수익률 = 0%')."""
    pct = (value * 100).quantize(Decimal("0.0001"))
    text = format(pct.normalize(), "f")
    return text


def _user_entered_value(column_name: str, value: str) -> str:
    """Prepare a value for a single shared USER_ENTERED write.

    NUMERIC_FORMAT_COLUMNS are left as plain digit strings so Sheets parses
    them into real NUMBER-typed cells (required for the %/"," numberFormat
    to actually show). Every other column gets a leading "'" -- the same
    "force literal text" marker a user gets by typing '005930 into the UI --
    so KR symbol codes keep their leading zero and ISO timestamps don't get
    reinterpreted as dates, while still going through the same USER_ENTERED
    request as everything else (one call instead of splitting RAW/
    USER_ENTERED into two, which was blowing past the Sheets API's writes-
    per-minute-per-user quota at one tick/second)."""
    if column_name in NUMERIC_FORMAT_COLUMNS or value == "":
        return value
    return "'" + value


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
        self._ensure_percent_format()
        self._ensure_amount_format()

    def _ensure_percent_format(self) -> None:
        for name in PERCENT_COLUMNS:
            col = self._col(name)
            col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip("0123456789")
            a1_range = f"{col_letter}2:{col_letter}{PERCENT_FORMAT_ROWS}"
            self._ws.format(a1_range, {"numberFormat": {"type": "NUMBER", "pattern": '0.00"%"'}})

    def _ensure_amount_format(self) -> None:
        for name in AMOUNT_COLUMNS:
            col = self._col(name)
            col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip("0123456789")
            a1_range = f"{col_letter}2:{col_letter}{PERCENT_FORMAT_ROWS}"
            self._ws.format(a1_range, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})

    def _ensure_header(self) -> None:
        first_row = self._ws.row_values(1)
        if not first_row:
            # Brand-new/empty sheet: safe to write the header fresh.
            self._ws.update([SHEET_COLUMNS], "A1")
            return
        if first_row != SHEET_COLUMNS:
            # Never blindly overwrite an existing header. Data rows below
            # still have their OLD physical column layout; rewriting just
            # the header text (without touching the data) silently
            # mislabels every column after the change point. A SHEET_COLUMNS
            # change (column added/removed/reordered) needs an explicit,
            # deliberate migration that inserts/moves real columns via the
            # Sheets API -- not an unattended header rewrite.
            logger.warning(
                "sheet header does not match SHEET_COLUMNS (expected %s, found %s); "
                "leaving header as-is -- run an explicit column migration instead",
                SHEET_COLUMNS, first_row,
            )

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
        rows = []
        for i, record in enumerate(records):
            row_number = i + 2
            try:
                rows.append(SheetRow.from_record(row_number, record))
            except Exception:
                # One malformed row (bad market code, non-numeric cell after a
                # manual edit, ...) must not take every other symbol down with
                # it -- skip it and keep going.
                logger.exception("skipping malformed sheet row %d: %r", row_number, record)
        return rows

    def _default_row_values(
        self,
        symbol: str,
        name: str,
        market: str,
        quantity: str = "0",
        purchase_amount_krw: str = "0",
        valuation_amount_krw: str = "0",
        profit_amount_krw: str = "0",
        profit_rate_pct: str = "0",
        peak_rate_pct: str = "0",
        take_profit_threshold_pct: str = "-100",
    ) -> list[str]:
        """New symbol discovered in broker holdings but absent from the sheet.
        전략적용여부=TRUE, 청산여부=FALSE are hard defaults; 보유수량/매입금액/
        평가금액/평가손익/수익률/최고수익률/익절기준 reflect the real holdings
        just fetched in the same sync (최고수익률 seeded to the current profit
        rate, and 익절기준 derived from that peak via the normal
        trailing-stop formula, since this symbol has no tracked peak history
        yet)."""
        now = dt.datetime.now(KST).isoformat(timespec="seconds")
        row = {
            "종목코드": symbol,
            "종목명": name,
            "마켓구분": market,
            "보유수량": quantity,
            "매입금액_원화": purchase_amount_krw,
            "평가금액_원화": valuation_amount_krw,
            "평가손익_원화": profit_amount_krw,
            "수익률": profit_rate_pct,
            "전략적용여부": "TRUE",
            "최고수익률": peak_rate_pct,
            "익절기준": take_profit_threshold_pct,
            "청산여부": "FALSE",
            "마지막갱신": now,
        }
        return [row[c] for c in SHEET_COLUMNS]

    def append_default_row(self, symbol: str, name: str, market: str, **kwargs) -> None:
        # Single USER_ENTERED call: NUMERIC_FORMAT_COLUMNS need USER_ENTERED
        # to become real NUMBER-typed cells (so %/"," numberFormat shows),
        # while _user_entered_value forces every other column to literal
        # text (leading "'") so KR symbol codes keep their leading zero and
        # timestamps aren't reinterpreted as dates -- all in one API call.
        values = self._default_row_values(symbol, name, market, **kwargs)
        entered = [_user_entered_value(col, v) for col, v in zip(SHEET_COLUMNS, values)]
        self._ws.append_row(entered, value_input_option="USER_ENTERED")

    def append_default_rows(self, entries: list[dict]) -> None:
        """Batched form of append_default_row: entries is a list of kwargs
        dicts (symbol, name, market, quantity=..., ...). Emits a single
        append_rows API call regardless of how many new holdings were
        discovered, to stay well within Sheets API write-request quotas --
        a cold-start sync can easily discover 50+ new symbols at once."""
        if not entries:
            return
        rows = [self._default_row_values(**entry) for entry in entries]
        entered_rows = [[_user_entered_value(col, v) for col, v in zip(SHEET_COLUMNS, row)] for row in rows]
        self._ws.append_rows(entered_rows, value_input_option="USER_ENTERED")

    def batch_write(self, cell_updates: list[tuple[int, str, str]]) -> None:
        """cell_updates: list of (row_number, column_name, raw_value). Emits a
        single batch_update API call regardless of how many symbols/cells
        changed this tick, to stay well within Sheets API write-per-minute
        quotas (a second call here at one tick/second would double the
        write-request rate and trip the per-user quota).

        Everything goes through USER_ENTERED: NUMERIC_FORMAT_COLUMNS need it
        to become real NUMBER-typed cells (RAW leaves numeric-looking
        strings as TEXT, silently dropping their %/"," numberFormat), and
        _user_entered_value forces every other column to literal text so
        USER_ENTERED's UI-like parsing doesn't strip the leading zero off KR
        symbol codes or reinterpret timestamps as dates.
        """
        if not cell_updates:
            return
        data = []
        for row_number, column_name, value in cell_updates:
            a1 = gspread.utils.rowcol_to_a1(row_number, self._col(column_name))
            data.append({"range": a1, "values": [[_user_entered_value(column_name, value)]]})
        self._ws.batch_update(data, value_input_option="USER_ENTERED")

    def touch_last_updated(self, row_number: int) -> tuple[int, str, str]:
        now = dt.datetime.now(KST).isoformat(timespec="seconds")
        return (row_number, "마지막갱신", now)
