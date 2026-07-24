from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Market(str, Enum):
    KR = "KR"
    US = "US"


@dataclass
class HoldingSnapshot:
    """A single position as reported by GET /api/v1/holdings, converted to KRW."""

    symbol: str
    name: str
    market: Market
    quantity: Decimal
    purchase_amount_krw: Decimal
    profit_rate: Decimal  # fraction, e.g. 0.1077 = 10.77%


# Sheet column order. Keep in sync with sheets_client.HEADER.
SHEET_COLUMNS = [
    "종목코드",
    "종목명",
    "마켓구분",
    "보유수량",
    "매입금액_원화",
    "평가금액_원화",
    "평가손익_원화",
    "수익률",
    "최고수익률",
    "전략적용여부",
    "익절기준",
    "청산여부",
    "마지막갱신",
]


@dataclass
class SheetRow:
    row_number: int  # 1-indexed sheet row, including header (data starts at 2)
    symbol: str
    name: str
    market: Market
    quantity: Decimal
    purchase_amount_krw: Decimal
    valuation_amount_krw: Decimal
    profit_amount_krw: Decimal
    profit_rate: Decimal  # fraction
    strategy_enabled: bool
    peak_rate: Decimal  # fraction
    take_profit_threshold: Decimal  # fraction
    liquidated: bool
    last_updated: str

    @staticmethod
    def _dec(value, default: str = "0") -> Decimal:
        text = str(value).strip() if value is not None else ""
        # get_all_records() renders cells with their display format (percent
        # suffix, thousands separators, ...) rather than the raw stored
        # number -- strip both before parsing. The underlying stored value
        # itself is unaffected by how the cell happens to be formatted.
        text = text.rstrip("%").replace(",", "")
        if text == "":
            text = default
        return Decimal(text)

    @staticmethod
    def _bool(value) -> bool:
        if value is None:
            return False
        return str(value).strip().upper() in ("TRUE", "T")

    @classmethod
    def from_record(cls, row_number: int, record: dict) -> "SheetRow":
        return cls(
            row_number=row_number,
            symbol=str(record.get("종목코드", "")).strip(),
            name=str(record.get("종목명", "")).strip(),
            market=Market(str(record.get("마켓구분", "KR")).strip() or "KR"),
            quantity=cls._dec(record.get("보유수량", "0")),
            purchase_amount_krw=cls._dec(record.get("매입금액_원화", "0")),
            valuation_amount_krw=cls._dec(record.get("평가금액_원화", "0")),
            profit_amount_krw=cls._dec(record.get("평가손익_원화", "0")),
            # sheet stores rates as percentages (e.g. 10.77), internally we use fractions
            profit_rate=cls._dec(record.get("수익률", "0")) / Decimal(100),
            strategy_enabled=cls._bool(record.get("전략적용여부", "FALSE")),
            peak_rate=cls._dec(record.get("최고수익률", "0")) / Decimal(100),
            take_profit_threshold=cls._dec(record.get("익절기준", "-100")) / Decimal(100),
            liquidated=cls._bool(record.get("청산여부", "FALSE")),
            last_updated=str(record.get("마지막갱신", "")).strip(),
        )
