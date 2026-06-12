from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.adapters._utils import to_float as _to_float

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_BACKTEST_PATH = ROOT_DIR / "data" / "backtest_history.csv"


@dataclass(frozen=True)
class BacktestRow:
    stockCode: str
    period: str
    closePrice: float
    cumulativeRevenueYoY: float | None
    monthlyRevenueYoY: float | None
    epsYoY: float | None
    netIncomeYoY: float | None
    per: float | None
    inventoryTurnover: float | None


def _load_rows(path: Path) -> list[BacktestRow]:
    rows: list[BacktestRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            stock_code = str(raw.get("stock_code") or raw.get("stockCode") or "").strip()
            period = str(raw.get("period") or raw.get("month") or "").strip()
            close_price = _to_float(raw.get("close_price") or raw.get("closePrice"))
            if not stock_code or not period or close_price is None or close_price <= 0:
                continue
            rows.append(
                BacktestRow(
                    stockCode=stock_code,
                    period=period,
                    closePrice=close_price,
                    cumulativeRevenueYoY=_to_float(
                        raw.get("cumulative_revenue_yoy") or raw.get("cumulativeRevenueYoY")
                    ),
                    monthlyRevenueYoY=_to_float(raw.get("monthly_revenue_yoy") or raw.get("monthlyRevenueYoY")),
                    epsYoY=_to_float(raw.get("eps_yoy") or raw.get("epsYoY")),
                    netIncomeYoY=_to_float(raw.get("net_income_yoy") or raw.get("netIncomeYoY")),
                    per=_to_float(raw.get("per")),
                    inventoryTurnover=_to_float(raw.get("inventory_turnover") or raw.get("inventoryTurnover")),
                )
            )
    return sorted(rows, key=lambda row: (row.stockCode, row.period))


def _entry_signal(row: BacktestRow) -> bool:
    return (
        row.cumulativeRevenueYoY is not None
        and row.cumulativeRevenueYoY >= 50
        and row.per is not None
        and row.per < 20
        and row.inventoryTurnover is not None
        and row.inventoryTurnover > 2.5
    )


def _exit_signal(row: BacktestRow) -> bool:
    x1_triggered = False
    if row.monthlyRevenueYoY is not None and row.cumulativeRevenueYoY is not None:
        x1_passed = row.cumulativeRevenueYoY >= row.monthlyRevenueYoY * 0.5
        x1_triggered = not x1_passed
    return (
        x1_triggered
        or (row.epsYoY is not None and row.epsYoY <= -10)
        or (row.netIncomeYoY is not None and row.netIncomeYoY < 0)
    )


def run_backtest(path: str | Path | None = None) -> dict[str, Any]:
    source_path = Path(path) if path is not None else DEFAULT_BACKTEST_PATH
    if not source_path.exists():
        return {
            "status": "NO_DATA",
            "sourcePath": str(source_path),
            "trades": [],
            "metrics": {"tradeCount": 0, "winRate": None, "totalReturn": None, "maxDrawdown": None},
            "note": "尚未提供 data/backtest_history.csv；可依 template 匯入歷史月營收、季報與價格後回測。",
        }

    rows = _load_rows(source_path)
    trades: list[dict[str, Any]] = []
    equity_curve = [1.0]
    positions: dict[str, BacktestRow] = {}

    for row in rows:
        open_row = positions.get(row.stockCode)
        if open_row and _exit_signal(row):
            trade_return = (row.closePrice - open_row.closePrice) / open_row.closePrice
            equity_curve.append(equity_curve[-1] * (1 + trade_return))
            trades.append(
                {
                    "stockCode": row.stockCode,
                    "entryPeriod": open_row.period,
                    "exitPeriod": row.period,
                    "entryPrice": open_row.closePrice,
                    "exitPrice": row.closePrice,
                    "return": trade_return,
                }
            )
            positions.pop(row.stockCode, None)
            continue
        if not open_row and _entry_signal(row):
            positions[row.stockCode] = row

    wins = [trade for trade in trades if trade["return"] > 0]
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value - peak) / peak)

    return {
        "status": "OK",
        "sourcePath": str(source_path),
        "rowCount": len(rows),
        "openPositions": sorted(positions),
        "trades": trades,
        "metrics": {
            "tradeCount": len(trades),
            "winRate": len(wins) / len(trades) if trades else None,
            "totalReturn": equity_curve[-1] - 1 if equity_curve else None,
            "maxDrawdown": max_drawdown,
        },
    }
