from __future__ import annotations

import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from backend.adapters._utils import to_float as _to_float

_FETCH_RETRIES = 2
_FETCH_RETRY_DELAY = 0.5


TWSE_INCOME_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_bd",
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_fh",
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ins",
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_mim",
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_basi",
]
TWSE_BALANCE_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci",
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_bd",
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_fh",
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ins",
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_mim",
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_basi",
]
TPEX_INCOME_URLS = [
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ciA",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_bdA",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_fhA",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_insA",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_mimA",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_basiA",
]
TPEX_BALANCE_URLS = [
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_bd",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_fh",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ins",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_mim",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_basi",
]
TWSE_VALUATION_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
TPEX_VALUATION_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"




def _roc_year(value: Any) -> int | None:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not text:
        return None
    try:
        return int(text) + 1911
    except ValueError:
        return None


def _quarter(value: Any) -> int | None:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not text:
        return None
    try:
        quarter = int(text)
    except ValueError:
        return None
    return quarter if 1 <= quarter <= 4 else None


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _gross_margin(revenue: float | None, gross_profit: float | None) -> float | None:
    if revenue in (None, 0) or gross_profit is None:
        return None
    return (gross_profit / revenue) * 100


def _operating_margin(revenue: float | None, operating_income: float | None) -> float | None:
    if revenue in (None, 0) or operating_income is None:
        return None
    return (operating_income / revenue) * 100


@dataclass(frozen=True)
class OfficialIncomeStatementRow:
    stockCode: str
    companyName: str
    market: str
    fiscalYear: int | None
    quarter: int | None
    revenue: float | None
    costOfRevenue: float | None
    grossProfit: float | None
    grossMargin: float | None
    operatingIncome: float | None
    operatingMargin: float | None
    netIncome: float | None
    eps: float | None
    source: str


@dataclass(frozen=True)
class OfficialBalanceSheetRow:
    stockCode: str
    companyName: str
    market: str
    fiscalYear: int | None
    quarter: int | None
    inventory: float | None
    source: str


@dataclass(frozen=True)
class OfficialValuationRow:
    stockCode: str
    companyName: str
    market: str
    date: str
    per: float | None
    priceBookRatio: float | None
    dividendYield: float | None
    fiscalQuarter: str
    source: str


@dataclass(frozen=True)
class OfficialFundamentalBundle:
    incomes: dict[str, OfficialIncomeStatementRow]
    balances: dict[str, OfficialBalanceSheetRow]
    valuations: dict[str, OfficialValuationRow]
    status: dict[str, Any]


class OfficialFundamentalsAdapter:
    def __init__(self, timeout: float = 20) -> None:
        self.timeout = timeout

    def _fetch_json(self, url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        response = httpx.get(url, params=params, timeout=self.timeout, follow_redirects=True)
        response.raise_for_status()
        return response.json()

    def _fetch_with_retry(self, url: str) -> list[dict[str, Any]] | dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_FETCH_RETRIES + 1):
            try:
                return self._fetch_json(url)
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt < _FETCH_RETRIES:
                    time.sleep(_FETCH_RETRY_DELAY)
        raise last_exc  # type: ignore[misc]

    def _fetch_many(self, urls: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        status: dict[str, Any] = {}
        url_list = list(urls)
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(url_list)))) as executor:
            futures = {executor.submit(self._fetch_with_retry, url): url for url in url_list}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    payload = future.result()
                    items = payload if isinstance(payload, list) else []
                    rows.extend(items)
                    status[url] = {"ok": True, "rows": len(items)}
                except Exception as exc:
                    status[url] = {"ok": False, "error": str(exc)}
        return rows, status

    def _fetch_many_sequential(self, urls: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        status: dict[str, Any] = {}
        for url in urls:
            try:
                payload = self._fetch_json(url)
                items = payload if isinstance(payload, list) else []
                rows.extend(items)
                status[url] = {"ok": True, "rows": len(items)}
            except Exception as exc:
                status[url] = {"ok": False, "error": str(exc)}
        return rows, status

    def fetch_income_statements(self) -> tuple[dict[str, OfficialIncomeStatementRow], dict[str, Any]]:
        rows, status = self._fetch_many([*TWSE_INCOME_URLS, *TPEX_INCOME_URLS])
        incomes: dict[str, OfficialIncomeStatementRow] = {}
        for row in rows:
            stock_code = str(_pick(row, "公司代號", "SecuritiesCompanyCode") or "").strip()
            if not stock_code or not stock_code.isdigit():
                continue
            market = "TPEX" if "SecuritiesCompanyCode" in row else "TWSE"
            revenue = _to_float(_pick(row, "營業收入", "收入"))
            cost = _to_float(_pick(row, "營業成本", "支出"))
            gross_profit = _to_float(_pick(row, "營業毛利（毛損）淨額", "營業毛利（毛損）"))
            operating_income = _to_float(_pick(row, "營業利益（損失）", "營業利益"))
            net_income = _to_float(_pick(row, "淨利（淨損）歸屬於母公司業主", "稅後淨利", "本期淨利（淨損）"))
            incomes[stock_code] = OfficialIncomeStatementRow(
                stockCode=stock_code,
                companyName=str(_pick(row, "公司名稱", "CompanyName") or "").strip(),
                market=market,
                fiscalYear=_roc_year(_pick(row, "年度", "Year")),
                quarter=_quarter(_pick(row, "季別", "Season")),
                revenue=revenue,
                costOfRevenue=cost,
                grossProfit=gross_profit,
                grossMargin=_gross_margin(revenue, gross_profit),
                operatingIncome=operating_income,
                operatingMargin=_operating_margin(revenue, operating_income),
                netIncome=net_income,
                eps=_to_float(_pick(row, "基本每股盈餘（元）", "基本每股盈餘(元)", "基本每股盈餘")),
                source="TWSE OpenAPI t187ap06" if market == "TWSE" else "TPEx OpenAPI mopsfin_t187ap06",
            )
        return incomes, status

    def fetch_balance_sheets(self) -> tuple[dict[str, OfficialBalanceSheetRow], dict[str, Any]]:
        rows, status = self._fetch_many([*TWSE_BALANCE_URLS, *TPEX_BALANCE_URLS])
        balances: dict[str, OfficialBalanceSheetRow] = {}
        for row in rows:
            stock_code = str(_pick(row, "公司代號", "SecuritiesCompanyCode") or "").strip()
            if not stock_code or not stock_code.isdigit():
                continue
            market = "TPEX" if "SecuritiesCompanyCode" in row else "TWSE"
            balances[stock_code] = OfficialBalanceSheetRow(
                stockCode=stock_code,
                companyName=str(_pick(row, "公司名稱", "CompanyName") or "").strip(),
                market=market,
                fiscalYear=_roc_year(_pick(row, "年度", "Year")),
                quarter=_quarter(_pick(row, "季別", "Season")),
                inventory=_to_float(_pick(row, "存貨", "存貨合計", "存貨淨額")),
                source="TWSE OpenAPI t187ap07" if market == "TWSE" else "TPEx OpenAPI mopsfin_t187ap07",
            )
        return balances, status

    def fetch_twse_valuations(self, today: date | None = None) -> tuple[dict[str, OfficialValuationRow], dict[str, Any]]:
        today = today or date.today()
        status: dict[str, Any] = {}
        for offset in range(0, 14):
            target = today - timedelta(days=offset)
            params = {"response": "json", "date": target.strftime("%Y%m%d")}
            try:
                payload = self._fetch_json(TWSE_VALUATION_URL, params=params)
            except Exception as exc:
                status[target.isoformat()] = {"ok": False, "error": str(exc)}
                continue
            if not isinstance(payload, dict) or payload.get("stat") != "OK" or not payload.get("data"):
                status[target.isoformat()] = {"ok": False, "error": payload.get("stat") if isinstance(payload, dict) else "invalid payload"}
                continue
            fields = payload.get("fields", [])
            rows = payload.get("data", [])
            valuations: dict[str, OfficialValuationRow] = {}
            for item in rows:
                row = dict(zip(fields, item, strict=False))
                stock_code = str(row.get("證券代號") or "").strip()
                if not stock_code or not stock_code.isdigit():
                    continue
                valuations[stock_code] = OfficialValuationRow(
                    stockCode=stock_code,
                    companyName=str(row.get("證券名稱") or "").strip(),
                    market="TWSE",
                    date=str(payload.get("date") or target.strftime("%Y%m%d")),
                    per=_to_float(row.get("本益比")),
                    priceBookRatio=_to_float(row.get("股價淨值比")),
                    dividendYield=_to_float(row.get("殖利率(%)")),
                    fiscalQuarter=str(row.get("財報年/季") or ""),
                    source="TWSE BWIBBU_d",
                )
            status[target.isoformat()] = {"ok": True, "rows": len(valuations)}
            return valuations, status
        return {}, status

    def fetch_tpex_valuations(self) -> tuple[dict[str, OfficialValuationRow], dict[str, Any]]:
        status: dict[str, Any] = {}
        try:
            payload = self._fetch_json(TPEX_VALUATION_URL)
        except Exception as exc:
            return {}, {"TPEX": {"ok": False, "error": str(exc)}}
        rows = payload if isinstance(payload, list) else []
        valuations: dict[str, OfficialValuationRow] = {}
        for row in rows:
            stock_code = str(row.get("SecuritiesCompanyCode") or "").strip()
            if not stock_code or not stock_code.isdigit():
                continue
            valuations[stock_code] = OfficialValuationRow(
                stockCode=stock_code,
                companyName=str(row.get("CompanyName") or "").strip(),
                market="TPEX",
                date=str(row.get("Date") or ""),
                per=_to_float(row.get("PriceEarningRatio")),
                priceBookRatio=_to_float(row.get("PriceBookRatio")),
                dividendYield=_to_float(row.get("YieldRatio")),
                fiscalQuarter="",
                source="TPEx tpex_mainboard_peratio_analysis",
            )
        status["TPEX"] = {"ok": True, "rows": len(valuations)}
        return valuations, status

    def fetch_valuations(self) -> tuple[dict[str, OfficialValuationRow], dict[str, Any]]:
        twse, twse_status = self.fetch_twse_valuations()
        tpex, tpex_status = self.fetch_tpex_valuations()
        return {**twse, **tpex}, {"TWSE": twse_status, "TPEX": tpex_status}

    def fetch_bundle(self) -> OfficialFundamentalBundle:
        with ThreadPoolExecutor(max_workers=3) as executor:
            income_future = executor.submit(self.fetch_income_statements)
            balance_future = executor.submit(self.fetch_balance_sheets)
            valuation_future = executor.submit(self.fetch_valuations)
            incomes, income_status = income_future.result()
            balances, balance_status = balance_future.result()
            valuations, valuation_status = valuation_future.result()
        return OfficialFundamentalBundle(
            incomes=incomes,
            balances=balances,
            valuations=valuations,
            status={
                "incomeStatements": income_status,
                "balanceSheets": balance_status,
                "valuations": valuation_status,
                "incomeRows": len(incomes),
                "balanceRows": len(balances),
                "valuationRows": len(valuations),
            },
        )
