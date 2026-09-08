from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx

from backend.adapters._utils import to_float as _to_float_base
from backend.adapters.official_fundamentals import (
    OfficialBalanceSheetRow,
    OfficialIncomeStatementRow,
    _gross_margin,
    _operating_margin,
)

MOPS_BASE_URL = "https://mopsov.twse.com.tw/mops/web"
MOPS_INCOME_ENDPOINT = f"{MOPS_BASE_URL}/ajax_t164sb04"
MOPS_BALANCE_ENDPOINT = f"{MOPS_BASE_URL}/ajax_t164sb03"
MOPS_API_BASE_URL = "https://mops.twse.com.tw/mops/api"
MOPS_API_INCOME_ENDPOINT = f"{MOPS_API_BASE_URL}/t164sb04"
MOPS_API_BALANCE_ENDPOINT = f"{MOPS_API_BASE_URL}/t164sb03"


def _to_float(value: Any) -> float | None:
    return _to_float_base(value, parenthesized_negative=True)


def _market_type(market: str) -> str:
    return "otc" if market.upper() == "TPEX" else "sii"


def _ad_to_roc_year(year: int) -> str:
    return str(year - 1911)


def _parse_income_period(text: str) -> tuple[int, int] | None:
    annual_match = re.search(r"(\d{2,3})年度", text)
    if annual_match:
        return int(annual_match.group(1)) + 1911, 4
    match = re.search(r"(\d{2,3})年\d{2}月\d{2}日至\1年(\d{2})月\d{2}日", text)
    if not match:
        return None
    year = int(match.group(1)) + 1911
    end_month = int(match.group(2))
    quarter = (end_month - 1) // 3 + 1
    return (year, quarter) if 1 <= quarter <= 4 else None


def _parse_balance_period(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{2,3})年(\d{2})月\d{2}日", text)
    if not match:
        return None
    year = int(match.group(1)) + 1911
    month = int(match.group(2))
    quarter = (month - 1) // 3 + 1
    return (year, quarter) if 1 <= quarter <= 4 else None


def _normalize_label(value: str) -> str:
    return " ".join(str(value or "").replace("\u3000", "").split()).strip()


def _parse_mops_income_title(text: str) -> tuple[int, int] | None:
    normalized = _normalize_label(text)
    annual = re.search(r"(\d{2,3})年度", normalized)
    if annual:
        return int(annual.group(1)) + 1911, 4
    quarter = re.search(r"(\d{2,3})年第([1-4])季", normalized)
    if quarter:
        return int(quarter.group(1)) + 1911, int(quarter.group(2))
    range_match = re.search(r"(\d{2,3})年\d{2}月\d{2}日至\1年(\d{2})月\d{2}日", normalized)
    if range_match:
        end_month = int(range_match.group(2))
        if end_month == 3:
            return int(range_match.group(1)) + 1911, 1
    return _parse_income_period(normalized)


def _is_single_quarter_income_title(text: str) -> bool:
    """MOPS Q2/Q3 statements carry both a 3-month "第N季" column and a year-to-date
    "MM月DD日至MM月DD日" column for the same period. The scanner's history (Q1 ranges,
    annual "年度" columns, and the TWSE OpenAPI current-quarter feed) is cumulative
    year-to-date, so single-quarter columns must yield to the cumulative ones."""
    normalized = _normalize_label(text)
    return re.search(r"(\d{2,3})年第([1-4])季", normalized) is not None and "年度" not in normalized


def _parse_mops_balance_title(text: str) -> tuple[int, int] | None:
    normalized = _normalize_label(text)
    date_match = re.search(r"(\d{2,3})年(\d{2})月\d{2}日", normalized)
    if date_match:
        year = int(date_match.group(1)) + 1911
        month = int(date_match.group(2))
        quarter = (month - 1) // 3 + 1
        return (year, quarter) if 1 <= quarter <= 4 else None
    return _parse_balance_period(normalized)


def _label_value(rows: list[list[str]], labels: tuple[str, ...], column: int) -> float | None:
    for expected_label in labels:
        normalized_expected = _normalize_label(expected_label)
        for row in rows:
            if not row or _normalize_label(row[0]) != normalized_expected or len(row) <= column:
                continue
            value = _to_float(row[column])
            if value is not None:
                return value
    return None


class _RowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_cell = False
        self.cell = ""
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized == "tr":
            self.row = []
        if normalized in {"td", "th"}:
            self.in_cell = True
            self.cell = ""

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell += data

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"td", "th"} and self.in_cell:
            self.row.append(" ".join(self.cell.split()))
            self.in_cell = False
        if normalized == "tr" and self.row:
            self.rows.append(self.row)


@dataclass(frozen=True)
class MopsHistoricalBundle:
    incomes: list[OfficialIncomeStatementRow] = field(default_factory=list)
    balances: list[OfficialBalanceSheetRow] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)


class OfficialMopsHistoricalFundamentalsAdapter:
    """Fetch historical financial rows from official MOPS JSON APIs with HTML fallback."""

    def __init__(self, timeout: float = 20) -> None:
        self.timeout = timeout

    def _fetch_income(self, stock_code: str, company_name: str, market: str, fiscal_year: int, quarter: int) -> tuple[list[OfficialIncomeStatementRow], dict[str, Any]]:
        payload = self._fetch_statement_payload(MOPS_API_INCOME_ENDPOINT, stock_code, fiscal_year, quarter)
        if payload:
            rows = self.parse_income_payload(payload, stock_code, company_name, market)
            return rows, {"ok": True, "rows": len(rows)}
        html = self._fetch_statement(MOPS_INCOME_ENDPOINT, stock_code, market, fiscal_year, quarter)
        if html:
            rows = self.parse_income_statement(html, stock_code, company_name, market)
            return rows, {"ok": True, "rows": len(rows), "fallback": "html"}
        return [], {"ok": False, "rows": 0}

    def _fetch_balance(self, stock_code: str, company_name: str, market: str, fiscal_year: int, quarter: int) -> tuple[list[OfficialBalanceSheetRow], dict[str, Any]]:
        payload = self._fetch_statement_payload(MOPS_API_BALANCE_ENDPOINT, stock_code, fiscal_year, quarter)
        if payload:
            rows = self.parse_balance_payload(payload, stock_code, company_name, market)
            return rows, {"ok": True, "rows": len(rows)}
        html = self._fetch_statement(MOPS_BALANCE_ENDPOINT, stock_code, market, fiscal_year, quarter)
        if html:
            rows = self.parse_balance_sheet(html, stock_code, company_name, market)
            return rows, {"ok": True, "rows": len(rows), "fallback": "html"}
        return [], {"ok": False, "rows": 0}

    def fetch_company_period(self, stock_code: str, company_name: str, market: str, fiscal_year: int, quarter: int) -> MopsHistoricalBundle:
        with ThreadPoolExecutor(max_workers=2) as executor:
            income_future = executor.submit(self._fetch_income, stock_code, company_name, market, fiscal_year, quarter)
            balance_future = executor.submit(self._fetch_balance, stock_code, company_name, market, fiscal_year, quarter)
            incomes, income_status = income_future.result()
            balances, balance_status = balance_future.result()
        return MopsHistoricalBundle(incomes=incomes, balances=balances, status={"income": income_status, "balance": balance_status})

    def _fetch_statement_payload(self, endpoint: str, stock_code: str, fiscal_year: int, quarter: int) -> dict[str, Any] | None:
        payload = {
            "companyId": stock_code,
            "dataType": "2",
            "year": _ad_to_roc_year(fiscal_year),
            "season": str(quarter),
            "subsidiaryCompanyId": "",
        }
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://mops.twse.com.tw",
            "Referer": "https://mops.twse.com.tw/mops/",
            "User-Agent": "Mozilla/5.0",
        }
        try:
            response = httpx.post(endpoint, json=payload, headers=headers, timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict) or body.get("code") != 200:
            return None
        result = body.get("result")
        return result if isinstance(result, dict) else None

    def _fetch_statement(self, endpoint: str, stock_code: str, market: str, fiscal_year: int, quarter: int) -> str | None:
        params = {
            "encodeURIComponent": "1",
            "step": "1",
            "firstin": "1",
            "off": "1",
            "TYPEK": _market_type(market),
            "isnew": "false",
            "co_id": stock_code,
            "year": _ad_to_roc_year(fiscal_year),
            "season": f"{quarter:02d}",
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": endpoint.replace("ajax_", ""),
        }
        try:
            response = httpx.get(endpoint, params=params, headers=headers, timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        response.encoding = "utf-8"
        text = response.text
        if "查無資料" in text or "THE PAGE CANNOT BE ACCESSED" in text or "頁面無法執行" in text:
            return None
        return text

    def _parse_rows(self, html: str) -> list[list[str]]:
        parser = _RowsParser()
        parser.feed(html)
        return parser.rows

    def parse_income_statement(self, html: str, stock_code: str, company_name: str, market: str) -> list[OfficialIncomeStatementRow]:
        rows = self._parse_rows(html)
        header = next((row for row in rows if row and row[0] == "會計項目"), None)
        if not header:
            return []
        periods = [_parse_income_period(value) for value in header[1:]]
        parsed: list[OfficialIncomeStatementRow] = []
        for offset, period in enumerate(periods):
            if period is None:
                continue
            column = 1 + offset * 2
            fiscal_year, quarter = period
            revenue = _label_value(rows, ("營業收入合計", "收益合計"), column)
            cost = _label_value(rows, ("營業成本合計", "支出及費用合計"), column)
            gross_profit = _label_value(rows, ("營業毛利（毛損）淨額", "營業毛利（毛損）"), column)
            operating_income = _label_value(rows, ("營業利益（損失）",), column)
            net_income = _label_value(rows, ("母公司業主（淨利∕損）", "本期淨利（淨損）"), column)
            eps = _label_value(rows, ("基本每股盈餘",), column)
            parsed.append(
                OfficialIncomeStatementRow(
                    stockCode=stock_code,
                    companyName=company_name,
                    market=market,
                    fiscalYear=fiscal_year,
                    quarter=quarter,
                    revenue=revenue,
                    costOfRevenue=cost,
                    grossProfit=gross_profit,
                    grossMargin=_gross_margin(revenue, gross_profit),
                    operatingIncome=operating_income,
                    operatingMargin=_operating_margin(revenue, operating_income),
                    netIncome=net_income,
                    eps=eps,
                    source="MOPS original ajax_t164sb04",
                )
            )
        return parsed

    def parse_income_payload(self, payload: dict[str, Any], stock_code: str, company_name: str, market: str) -> list[OfficialIncomeStatementRow]:
        rows = payload.get("reportList", [])
        titles = payload.get("titles", [])
        if not isinstance(rows, list) or not isinstance(titles, list):
            return []
        company_name = str(payload.get("companyAbbreviation") or company_name).strip() or company_name
        title_texts = [str(item.get("main", "")) for item in titles[1:] if isinstance(item, dict)]
        periods = [_parse_mops_income_title(text) for text in title_texts]
        cumulative_periods = {
            period for period, text in zip(periods, title_texts, strict=True)
            if period is not None and not _is_single_quarter_income_title(text)
        }
        parsed: list[OfficialIncomeStatementRow] = []
        seen: set[tuple[int, int]] = set()
        for offset, (period, text) in enumerate(zip(periods, title_texts, strict=True)):
            if period is None or period in seen:
                continue
            if _is_single_quarter_income_title(text) and period in cumulative_periods:
                # Prefer the year-to-date column for the same period (see _is_single_quarter_income_title).
                continue
            seen.add(period)
            column = 1 + offset * 2
            fiscal_year, quarter = period
            revenue = _label_value(rows, ("營業收入合計", "營業收入", "收益合計"), column)
            cost = _label_value(rows, ("營業成本合計", "營業成本"), column)
            gross_profit = _label_value(rows, ("營業毛利（毛損）淨額", "營業毛利（毛損）"), column)
            operating_income = _label_value(rows, ("營業利益（損失）", "營業利益"), column)
            net_income = _label_value(
                rows,
                (
                    "母公司業主（淨利∕損）",
                    "母公司業主（淨利／損）",
                    "淨利（損）歸屬於母公司業主",
                    "本期淨利（淨損）",
                    "繼續營業單位本期淨利（淨損）",
                ),
                column,
            )
            eps = _label_value(rows, ("基本每股盈餘", "繼續營業單位淨利（淨損）"), column)
            parsed.append(
                OfficialIncomeStatementRow(
                    stockCode=stock_code,
                    companyName=company_name,
                    market=market,
                    fiscalYear=fiscal_year,
                    quarter=quarter,
                    revenue=revenue,
                    costOfRevenue=cost,
                    grossProfit=gross_profit,
                    grossMargin=_gross_margin(revenue, gross_profit),
                    operatingIncome=operating_income,
                    operatingMargin=_operating_margin(revenue, operating_income),
                    netIncome=net_income,
                    eps=eps,
                    source="MOPS official API t164sb04",
                )
            )
        return parsed

    def parse_balance_sheet(self, html: str, stock_code: str, company_name: str, market: str) -> list[OfficialBalanceSheetRow]:
        rows = self._parse_rows(html)
        header = next((row for row in rows if row and row[0] == "會計項目"), None)
        if not header:
            return []
        periods = [_parse_balance_period(value) for value in header[1:]]
        parsed: list[OfficialBalanceSheetRow] = []
        for offset, period in enumerate(periods):
            if period is None:
                continue
            column = 1 + offset * 2
            fiscal_year, quarter = period
            parsed.append(
                OfficialBalanceSheetRow(
                    stockCode=stock_code,
                    companyName=company_name,
                    market=market,
                    fiscalYear=fiscal_year,
                    quarter=quarter,
                    inventory=_label_value(rows, ("存貨", "存貨合計", "存貨淨額"), column),
                    source="MOPS original ajax_t164sb03",
                )
            )
        return parsed

    def parse_balance_payload(self, payload: dict[str, Any], stock_code: str, company_name: str, market: str) -> list[OfficialBalanceSheetRow]:
        rows = payload.get("reportList", [])
        titles = payload.get("titles", [])
        if not isinstance(rows, list) or not isinstance(titles, list):
            return []
        company_name = str(payload.get("companyAbbreviation") or company_name).strip() or company_name
        periods = [_parse_mops_balance_title(str(item.get("main", ""))) for item in titles[1:] if isinstance(item, dict)]
        parsed: list[OfficialBalanceSheetRow] = []
        seen: set[tuple[int, int]] = set()
        for offset, period in enumerate(periods):
            if period is None or period in seen:
                continue
            seen.add(period)
            column = 1 + offset * 2
            fiscal_year, quarter = period
            parsed.append(
                OfficialBalanceSheetRow(
                    stockCode=stock_code,
                    companyName=company_name,
                    market=market,
                    fiscalYear=fiscal_year,
                    quarter=quarter,
                    inventory=_label_value(rows, ("存貨", "存貨合計"), column),
                    source="MOPS official API t164sb03",
                )
            )
        return parsed
