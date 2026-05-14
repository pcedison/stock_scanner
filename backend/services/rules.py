from __future__ import annotations

from backend.models.analysis import AnalysisResult, RuleResult
from backend.models.financial import AnnualFinancial, FundamentalSnapshot
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.calendar import load_market_calendar


def _latest_annual_net_incomes(snapshot: FundamentalSnapshot, years: int) -> list[float | None]:
    ordered = sorted(snapshot.annualFinancials, key=lambda item: item.year)
    return [item.netIncome for item in ordered[-years:]]


def _latest_annual_financials(snapshot: FundamentalSnapshot, years: int) -> list[AnnualFinancial]:
    ordered = sorted(snapshot.annualFinancials, key=lambda item: item.year)
    return ordered[-years:]


def _annual_net_income_evidence(rows: list[AnnualFinancial]) -> list[dict]:
    return [
        {
            "label": str(item.year),
            "value": item.netIncome,
            "unit": "thousand_twd",
            "metric": "annual_net_income",
        }
        for item in sorted(rows, key=lambda row: row.year, reverse=True)
    ]


def _annual_net_income_message(rows: list[AnnualFinancial], years: int, *, growth_rule: bool = False) -> str:
    values = [item.netIncome for item in rows]
    available_values = [value for value in values if value is not None]
    if len(rows) < years or len(available_values) < years:
        return f"已取得 {len(available_values)} / {years} 年年度淨利，仍有年度待補；詳見年度表格。"
    if growth_rule:
        is_growing = all(later > earlier for earlier, later in zip(available_values, available_values[1:]))
        return "近 3 年年度淨利連續成長；詳見年度表格。" if is_growing else "近 3 年年度淨利未連續成長；詳見年度表格。"
    loss_count = sum(1 for value in available_values if value < 0)
    return f"近 {years} 年年度淨利有 {loss_count} 年虧損；詳見年度表格。" if loss_count else f"近 {years} 年年度淨利皆為正；詳見年度表格。"


def _annual_net_income_yoy(snapshot: FundamentalSnapshot) -> float | None:
    ordered = sorted(snapshot.annualFinancials, key=lambda item: item.year)
    if len(ordered) < 2:
        return None
    previous = ordered[-2].netIncome
    latest = ordered[-1].netIncome
    if previous is None or latest is None or previous == 0:
        return None
    return ((latest - previous) / abs(previous)) * 100


def _is_spring_month(snapshot: FundamentalSnapshot, settings: ScannerSettings) -> bool:
    if not settings.spring_festival_guard:
        return False
    if snapshot.monthlyRevenue.isSpringFestivalMonth:
        return True
    year = int(snapshot.monthlyRevenue.month[:4])
    return load_market_calendar(year).is_spring_festival_month(snapshot.monthlyRevenue.month)


def _revenue_growth_for_entry(snapshot: FundamentalSnapshot, settings: ScannerSettings) -> float | None:
    monthly = snapshot.monthlyRevenue
    if settings.revenue_growth_mode == "monthly":
        return monthly.monthlyRevenueYoY
    if settings.revenue_growth_mode == "trailing_3m_avg":
        return monthly.trailingThreeMonthAverageYoY
    return monthly.cumulativeRevenueYoY


def _rule_missing(code: str, title: str, message: str, evidence: list[dict] | None = None) -> RuleResult:
    return RuleResult(code=code, title=title, passed=False, severity="INSUFFICIENT_DATA", message=message, evidence=evidence)


def _financial_entry_rules(snapshot: FundamentalSnapshot) -> list[RuleResult]:
    valuation = snapshot.valuation
    quarterly = snapshot.quarterlyFinancial
    rules = [
        RuleResult(
            code="FIN0",
            title="金融業專用策略",
            passed=True,
            severity="INFO",
            message="金融業不使用存貨週轉率，改以 ROE、逾放比、資本適足率、利差、股利與淨利趨勢檢查。",
        ),
        (
            _rule_missing("FIN1", "ROE >= 8%", "缺少 ROE，不能確認金融業獲利品質。")
            if valuation.roe is None
            else RuleResult(
                code="FIN1",
                title="ROE >= 8%",
                passed=valuation.roe >= 8,
                severity="WATCH" if valuation.roe < 8 else "INFO",
                message=f"ROE 為 {valuation.roe:.1f}%。",
            )
        ),
        (
            _rule_missing("FIN2", "逾放比 <= 2%", "缺少逾放比，不能確認授信資產品質。")
            if valuation.nonPerformingLoanRatio is None
            else RuleResult(
                code="FIN2",
                title="逾放比 <= 2%",
                passed=valuation.nonPerformingLoanRatio <= 2,
                severity="WARNING" if valuation.nonPerformingLoanRatio > 2 else "INFO",
                message=f"逾放比為 {valuation.nonPerformingLoanRatio:.2f}%。",
            )
        ),
        (
            _rule_missing("FIN3", "資本適足率 >= 10.5%", "缺少資本適足率，不能確認資本緩衝。")
            if valuation.capitalAdequacyRatio is None
            else RuleResult(
                code="FIN3",
                title="資本適足率 >= 10.5%",
                passed=valuation.capitalAdequacyRatio >= 10.5,
                severity="WARNING" if valuation.capitalAdequacyRatio < 10.5 else "INFO",
                message=f"資本適足率為 {valuation.capitalAdequacyRatio:.1f}%。",
            )
        ),
        (
            _rule_missing("FIN4", "淨利差為正", "缺少淨利差或利差資料，不能確認本業利差。")
            if valuation.netInterestMargin is None
            else RuleResult(
                code="FIN4",
                title="淨利差為正",
                passed=valuation.netInterestMargin > 0,
                severity="WATCH" if valuation.netInterestMargin <= 0 else "INFO",
                message=f"淨利差為 {valuation.netInterestMargin:.2f}%。",
            )
        ),
        (
            _rule_missing("FIN5", "殖利率 >= 3%", "缺少殖利率，不能確認金融股股利政策。")
            if valuation.dividendYield is None
            else RuleResult(
                code="FIN5",
                title="殖利率 >= 3%",
                passed=valuation.dividendYield >= 3,
                severity="WATCH" if valuation.dividendYield < 3 else "INFO",
                message=f"殖利率為 {valuation.dividendYield:.2f}%。",
            )
        ),
        (
            _rule_missing("FIN6", "最新季淨利年增率 >= 0%", "缺少淨利年增率，不能確認金融業獲利是否衰退。")
            if quarterly.netIncomeYoY is None
            else RuleResult(
                code="FIN6",
                title="最新季淨利年增率 >= 0%",
                passed=quarterly.netIncomeYoY >= 0,
                severity="WARNING" if quarterly.netIncomeYoY < 0 else "INFO",
                message=f"最新季淨利年增率為 {quarterly.netIncomeYoY:.1f}%。",
            )
        ),
    ]
    return rules


def _healthy_entry_rules(snapshot: FundamentalSnapshot, settings: ScannerSettings) -> list[RuleResult]:
    company = snapshot.company
    annual_5_rows = _latest_annual_financials(snapshot, 5)
    annual_3_rows = _latest_annual_financials(snapshot, 3)
    annual_5y = _latest_annual_net_incomes(snapshot, 5)
    annual_3y = _latest_annual_net_incomes(snapshot, 3)
    revenue_growth = _revenue_growth_for_entry(snapshot, settings)

    if company.isFinancial and not settings.exclude_financial_industry:
        return _financial_entry_rules(snapshot)

    e3 = (
        _rule_missing("E3", "今年累計營收年增率 >= 50%", f"目前採用 {settings.revenue_growth_mode}，但缺少對應營收年增率資料。")
        if revenue_growth is None
        else RuleResult(
            code="E3",
            title="今年累計營收年增率 >= 50%",
            passed=revenue_growth >= 50,
            severity="WATCH" if revenue_growth < 50 else "INFO",
            message=f"目前採用 {settings.revenue_growth_mode}，年增率為 {revenue_growth:.1f}%。",
        )
    )
    e4 = (
        _rule_missing("E4", "本益比小於 15", "官方估值未提供 PER 或 PER 不適用，不能判定估值是否不貴。")
        if snapshot.valuation.per is None
        else RuleResult(
            code="E4",
            title="本益比小於 15",
            passed=snapshot.valuation.per < 15,
            severity="WATCH" if snapshot.valuation.per >= 15 else "INFO",
            message=f"PER 為 {snapshot.valuation.per:.1f}。",
        )
    )
    e5 = (
        _rule_missing("E5", "存貨週轉率大於 2.5", "存貨週轉率尚未自動補齊，不能判定庫存是否健康。")
        if snapshot.valuation.inventoryTurnover is None
        else RuleResult(
            code="E5",
            title="存貨週轉率大於 2.5",
            passed=snapshot.valuation.inventoryTurnover > 2.5,
            severity="WATCH" if snapshot.valuation.inventoryTurnover <= 2.5 else "INFO",
            message=f"存貨週轉率為 {snapshot.valuation.inventoryTurnover:.2f}。",
        )
    )

    e1_evidence = _annual_net_income_evidence(annual_5_rows)
    e2_evidence = _annual_net_income_evidence(annual_3_rows)
    e1 = (
        _rule_missing(
            "E1",
            "近 5 年沒有虧損",
            _annual_net_income_message(annual_5_rows, 5),
            evidence=e1_evidence,
        )
        if len(annual_5y) < 5 or any(value is None for value in annual_5y)
        else RuleResult(
            code="E1",
            title="近 5 年沒有虧損",
            passed=all(value >= 0 for value in annual_5y if value is not None),
            severity="WATCH" if any(value < 0 for value in annual_5y if value is not None) else "INFO",
            message=_annual_net_income_message(annual_5_rows, 5),
            evidence=e1_evidence,
        )
    )
    e2_growth_ok = all(
        later > earlier for earlier, later in zip(annual_3y, annual_3y[1:]) if earlier is not None and later is not None
    )
    e2 = (
        _rule_missing(
            "E2",
            "近 3 年淨利正成長",
            _annual_net_income_message(annual_3_rows, 3, growth_rule=True),
            evidence=e2_evidence,
        )
        if len(annual_3y) < 3 or any(value is None for value in annual_3y)
        else RuleResult(
            code="E2",
            title="近 3 年淨利正成長",
            passed=e2_growth_ok,
            severity="WATCH" if not e2_growth_ok else "INFO",
            message=_annual_net_income_message(annual_3_rows, 3, growth_rule=True),
            evidence=e2_evidence,
        )
    )

    rules = [
        e1,
        e2,
        e3,
        e4,
        e5,
        RuleResult(
            code="E6",
            title="排除金融業",
            passed=not (settings.exclude_financial_industry and company.isFinancial),
            severity="EXCLUDED" if settings.exclude_financial_industry and company.isFinancial else "INFO",
            message=(
                f"{company.name} 屬於 {company.industryName}，主策略預設排除金融業。"
                if settings.exclude_financial_industry and company.isFinancial
                else f"{company.name} 非金融排除標的。"
            ),
        ),
    ]
    if snapshot.quarterlyFinancial.eps is not None or snapshot.quarterlyFinancial.netIncome is not None:
        rules.append(
            RuleResult(
                code="OFFICIAL_Q",
                title="最新季官方財報資料",
                passed=True,
                severity="INFO",
                message=(
                    f"{snapshot.quarterlyFinancial.quarter} EPS "
                    f"{snapshot.quarterlyFinancial.eps if snapshot.quarterlyFinancial.eps is not None else '缺資料'}，"
                    f"淨利 {snapshot.quarterlyFinancial.netIncome if snapshot.quarterlyFinancial.netIncome is not None else '缺資料'}，"
                    f"毛利率 {snapshot.quarterlyFinancial.grossMargin if snapshot.quarterlyFinancial.grossMargin is not None else '缺資料'}%。"
                ),
            )
        )
    if snapshot.valuation.per is not None or snapshot.valuation.priceBookRatio is not None:
        rules.append(
            RuleResult(
                code="OFFICIAL_VALUATION",
                title="官方估值資料",
                passed=True,
                severity="INFO",
                message=(
                    f"估值日期 {snapshot.valuation.valuationDate or '未標示'}，"
                    f"PER {snapshot.valuation.per if snapshot.valuation.per is not None else '缺資料'}，"
                    f"PBR {snapshot.valuation.priceBookRatio if snapshot.valuation.priceBookRatio is not None else '缺資料'}，"
                    f"殖利率 {snapshot.valuation.dividendYield if snapshot.valuation.dividendYield is not None else '缺資料'}%。"
                ),
            )
        )
    return rules


def _exit_rules(snapshot: FundamentalSnapshot, settings: ScannerSettings) -> list[RuleResult]:
    monthly = snapshot.monthlyRevenue
    quarterly = snapshot.quarterlyFinancial
    spring_guard = _is_spring_month(snapshot, settings)
    yoy_drop = None
    if monthly.previousMonthRevenueYoY is not None and monthly.monthlyRevenueYoY is not None:
        yoy_drop = monthly.previousMonthRevenueYoY - monthly.monthlyRevenueYoY

    spring_support_growth = monthly.janFebCombinedRevenueYoY
    if spring_support_growth is None:
        spring_support_growth = monthly.trailingThreeMonthAverageYoY
    spring_support_ok = spring_support_growth is not None and spring_support_growth >= 30

    x1_triggered = monthly.monthlyRevenueYoY is not None and monthly.monthlyRevenueYoY < 30
    x2_triggered = yoy_drop is not None and yoy_drop > 20
    x1_message = (
        f"月營收年增率為 {monthly.monthlyRevenueYoY:.1f}%。"
        if monthly.monthlyRevenueYoY is not None
        else "缺少月營收年增率資料。"
    )
    x2_message = (
        f"本月較上月年增率降溫 {yoy_drop:.1f} 個百分點。"
        if yoy_drop is not None
        else "缺少上月年增率，未觸發突然降溫規則。"
    )
    if spring_guard and x1_triggered:
        x1_message += " 目前為春節保護月份，需搭配 1+2 月或近 3 個月平均判斷。"
    if spring_guard and x2_triggered:
        x2_message += " 目前為春節保護月份，需搭配 1+2 月或近 3 個月平均判斷。"
    if spring_guard and (x1_triggered or x2_triggered) and spring_support_growth is not None:
        x1_message += f" 輔助營收年增率為 {spring_support_growth:.1f}%。"

    x1_rule = (
        _rule_missing("X1", "月營收年增率不可低於 30%", "缺少月營收年增率，不能確認是否低於出場門檻。")
        if monthly.monthlyRevenueYoY is None
        else RuleResult(
            code="X1",
            title="月營收年增率不可低於 30%",
            passed=not x1_triggered or (spring_guard and spring_support_ok),
            severity="WATCH" if x1_triggered and spring_guard else ("WARNING" if x1_triggered else "INFO"),
            message=x1_message,
        )
    )
    x2_rule = (
        _rule_missing("X2", "月營收年增率不可突然降溫超過 20 個百分點", "缺少上月年增率，不能確認本月是否突然降溫。")
        if monthly.previousMonthRevenueYoY is None or monthly.monthlyRevenueYoY is None
        else RuleResult(
            code="X2",
            title="月營收年增率不可突然降溫超過 20 個百分點",
            passed=not x2_triggered or (spring_guard and spring_support_ok),
            severity="WATCH" if x2_triggered and spring_guard else ("WARNING" if x2_triggered else "INFO"),
            message=x2_message,
        )
    )

    rules = [
        x1_rule,
        x2_rule,
        (
            _rule_missing("X3", "EPS 不可衰退", "缺少最新季 EPS 年增率，不能確認 EPS 是否轉弱。")
            if quarterly.epsYoY is None
            else RuleResult(
            code="X3",
            title="EPS 不可衰退",
            passed=quarterly.epsYoY >= 0,
            severity="WARNING" if quarterly.epsYoY < 0 else "INFO",
            message=f"最新季 EPS 年增率為 {quarterly.epsYoY:.1f}%。",
            )
        ),
        (
            _rule_missing("X4", "季度 EPS 不可減少超過 10%", "缺少最新季 EPS 年增率，不能確認是否大幅衰退。")
            if quarterly.epsYoY is None
            else RuleResult(
            code="X4",
            title="季度 EPS 不可減少超過 10%",
            passed=quarterly.epsYoY > -10,
            severity="EXIT" if quarterly.epsYoY <= -10 else "INFO",
            message=f"最新季 EPS 年增率為 {quarterly.epsYoY:.1f}%。",
            )
        ),
        (
            _rule_missing("X5", "淨利不可衰退", "缺少最新季淨利年增率，不能確認淨利是否轉弱。")
            if quarterly.netIncomeYoY is None
            else RuleResult(
            code="X5",
            title="淨利不可衰退",
            passed=quarterly.netIncomeYoY >= 0 and (_annual_net_income_yoy(snapshot) is None or _annual_net_income_yoy(snapshot) >= 0),
            severity="EXIT" if quarterly.netIncomeYoY < 0 or (_annual_net_income_yoy(snapshot) is not None and _annual_net_income_yoy(snapshot) < 0) else "INFO",
            message=f"最新季淨利年增率為 {quarterly.netIncomeYoY:.1f}%；年度淨利年增率為 {_annual_net_income_yoy(snapshot):.1f}%。" if _annual_net_income_yoy(snapshot) is not None else f"最新季淨利年增率為 {quarterly.netIncomeYoY:.1f}%。",
            )
        ),
        (
            _rule_missing("T3", "毛利率追蹤", "缺少毛利率年增率，仍可判斷核心出場規則，但持有追蹤不完整。")
            if quarterly.grossMarginYoY is None
            else RuleResult(
                code="T3",
                title="毛利率追蹤",
                passed=quarterly.grossMarginYoY >= 0,
                severity="WARNING" if quarterly.grossMarginYoY < 0 else "INFO",
                message=f"最新季毛利率年增率為 {quarterly.grossMarginYoY:.1f}%。",
            )
        ),
    ]
    if spring_guard and (x1_triggered or x2_triggered):
        rules.append(
            RuleResult(
                code="SPRING_FESTIVAL_WATCH",
                title="春節月份保護",
                passed=True,
                severity="WATCH",
                message="春節月份可能扭曲單月營收，需搭配 1+2 月或近 3 個月平均再確認。",
            )
        )
    return rules


def _add_watch_rules(entry_reasons: list[RuleResult], exit_reasons: list[RuleResult], snapshot: FundamentalSnapshot) -> list[RuleResult]:
    entry_ok = all(reason.passed for reason in entry_reasons)
    exit_ok = all(reason.passed for reason in exit_reasons if reason.code.startswith("X"))
    return [
        RuleResult(code="A1", title="原進場條件仍符合", passed=entry_ok, severity="WATCH" if not entry_ok else "INFO", message="E1-E6 全部通過才可列入加碼觀察。"),
        RuleResult(code="A2", title="累計營收年增率仍 >= 50%", passed=snapshot.monthlyRevenue.cumulativeRevenueYoY is not None and snapshot.monthlyRevenue.cumulativeRevenueYoY >= 50, severity="WATCH" if not (snapshot.monthlyRevenue.cumulativeRevenueYoY is not None and snapshot.monthlyRevenue.cumulativeRevenueYoY >= 50) else "INFO", message=f"累計營收年增率為 {snapshot.monthlyRevenue.cumulativeRevenueYoY if snapshot.monthlyRevenue.cumulativeRevenueYoY is not None else '缺資料'}%。"),
        RuleResult(code="A3", title="最新季 EPS 年增率 > 0", passed=snapshot.quarterlyFinancial.epsYoY is not None and snapshot.quarterlyFinancial.epsYoY > 0, severity="WATCH" if not (snapshot.quarterlyFinancial.epsYoY is not None and snapshot.quarterlyFinancial.epsYoY > 0) else "INFO", message=f"最新季 EPS 年增率為 {snapshot.quarterlyFinancial.epsYoY if snapshot.quarterlyFinancial.epsYoY is not None else '缺資料'}%。"),
        RuleResult(code="A4", title="最新季淨利年增率 > 0", passed=snapshot.quarterlyFinancial.netIncomeYoY is not None and snapshot.quarterlyFinancial.netIncomeYoY > 0, severity="WATCH" if not (snapshot.quarterlyFinancial.netIncomeYoY is not None and snapshot.quarterlyFinancial.netIncomeYoY > 0) else "INFO", message=f"最新季淨利年增率為 {snapshot.quarterlyFinancial.netIncomeYoY if snapshot.quarterlyFinancial.netIncomeYoY is not None else '缺資料'}%。"),
        RuleResult(code="A5", title="PER 仍 < 15", passed=snapshot.valuation.per is not None and snapshot.valuation.per < 15, severity="WATCH" if not (snapshot.valuation.per is not None and snapshot.valuation.per < 15) else "INFO", message=f"PER 為 {snapshot.valuation.per if snapshot.valuation.per is not None else '缺資料'}。"),
        RuleResult(code="A6", title="存貨週轉率仍 > 2.5", passed=snapshot.valuation.inventoryTurnover is not None and snapshot.valuation.inventoryTurnover > 2.5, severity="WATCH" if not (snapshot.valuation.inventoryTurnover is not None and snapshot.valuation.inventoryTurnover > 2.5) else "INFO", message=f"存貨週轉率為 {snapshot.valuation.inventoryTurnover if snapshot.valuation.inventoryTurnover is not None else '缺資料'}。"),
        RuleResult(code="A7", title="未觸發任何出場條件", passed=exit_ok, severity="WATCH" if not exit_ok else "INFO", message="X1-X5 未觸發時才可加碼觀察。"),
    ]


class RuleEngine:
    def evaluate_entry(self, snapshot: FundamentalSnapshot, settings: ScannerSettings) -> AnalysisResult:
        reasons = _healthy_entry_rules(snapshot, settings)
        company = snapshot.company
        if any(reason.code == "E6" and not reason.passed for reason in reasons):
            status = "EXCLUDED"
            summary = "金融業或策略排除產業，未納入主策略。"
        elif any(reason.severity == "INSUFFICIENT_DATA" for reason in reasons):
            status = "INSUFFICIENT_DATA"
            summary = "已有部分公開揭露資料，但完整策略因子待補，暫不硬給進場或排除結論。"
        elif all(reason.passed for reason in reasons):
            status = "ENTRY"
            summary = "所有進場條件通過，列入適合進場清單。"
        else:
            status = "WATCH"
            failed = "、".join(reason.code for reason in reasons if not reason.passed)
            summary = f"尚未通過所有進場條件，需觀察：{failed}。"

        return AnalysisResult(
            stockCode=company.stockCode,
            companyName=company.name,
            status=status,
            summary=summary,
            reasons=reasons,
            company=company,
        )

    def evaluate_holding(
        self,
        snapshot: FundamentalSnapshot,
        holding: Holding,
        settings: ScannerSettings,
    ) -> AnalysisResult:
        company = snapshot.company
        entry_reasons = _healthy_entry_rules(snapshot, settings)
        if any(reason.code == "E6" and not reason.passed for reason in entry_reasons):
            return AnalysisResult(
                stockCode=company.stockCode,
                companyName=company.name,
                status="EXCLUDED",
                summary="持股屬於策略排除產業，請改用人工判斷。",
                reasons=entry_reasons,
                company=company,
            )
        if any(reason.severity == "INSUFFICIENT_DATA" for reason in entry_reasons):
            return AnalysisResult(
                stockCode=company.stockCode,
                companyName=company.name,
                status="INSUFFICIENT_DATA",
                summary="已有部分公開揭露資料，但完整持股追蹤因子待補，暫不硬給續抱或出場結論。",
                reasons=entry_reasons,
                company=company,
            )

        exit_reasons = _exit_rules(snapshot, settings)
        add_reasons = _add_watch_rules(entry_reasons, exit_reasons, snapshot)
        failed_exit = [reason for reason in exit_reasons if reason.code.startswith("X") and not reason.passed]
        high_priority_exit = any(reason.code in {"X4", "X5"} and not reason.passed for reason in exit_reasons)
        mild_warning = any(reason.code in {"X1", "X2", "X3", "T3"} and not reason.passed for reason in exit_reasons)

        if any(reason.severity == "INSUFFICIENT_DATA" for reason in exit_reasons):
            status = "INSUFFICIENT_DATA"
            summary = "持有追蹤因子待補，暫不硬給續抱或出場結論。"
        elif high_priority_exit:
            status = "EXIT"
            summary = "已觸發高優先出場條件，建議出清或至少大幅降低部位。"
        elif mild_warning:
            status = "WARNING"
            failed = "、".join(reason.code for reason in failed_exit)
            summary = f"成長訊號轉弱，進入警戒：{failed}。"
        elif all(reason.passed for reason in add_reasons):
            status = "ADD_WATCH"
            summary = "進場條件仍符合且季 EPS/淨利維持成長，可列入加碼觀察。"
        else:
            status = "HOLD"
            summary = "未觸發出場條件，持續追蹤月營收與季報。"

        holding_note = RuleResult(
            code="HOLDING",
            title="目前持股",
            passed=True,
            message=f"本地持股為 {holding.shares} 股，平均成本 {holding.averageCost if holding.averageCost is not None else '未填'}。",
        )
        return AnalysisResult(
            stockCode=company.stockCode,
            companyName=company.name,
            status=status,
            summary=summary,
            reasons=[holding_note, *exit_reasons, *entry_reasons, *add_reasons],
            company=company,
        )
