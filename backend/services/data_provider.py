from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from backend.models.company import Company
from backend.models.financial import FundamentalSnapshot
from backend.models.settings import ScannerSettings


ROOT_DIR = Path(__file__).resolve().parents[2]


def normalize_query(value: str) -> str:
    return " ".join(value.strip().lower().split())


class MockDataProvider:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = data_dir or ROOT_DIR / "data"
        self._companies = self._load_companies()
        self._fundamentals = self._load_fundamentals()

    def _load_companies(self) -> list[Company]:
        path = self.data_dir / "sample_companies.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Company.model_validate(item) for item in raw]

    def _load_fundamentals(self) -> dict[str, FundamentalSnapshot]:
        path = self.data_dir / "sample_fundamentals.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        companies = {company.stockCode: company for company in self._companies}
        snapshots: dict[str, FundamentalSnapshot] = {}
        for stock_code, payload in raw.items():
            company = companies[stock_code]
            snapshots[stock_code] = FundamentalSnapshot.model_validate(
                {"company": company.model_dump(), **payload}
            )
        return snapshots

    def list_companies(self) -> list[Company]:
        return list(self._companies)

    def search_companies(self, query: str, limit: int = 20) -> list[Company]:
        normalized = normalize_query(query)
        if not normalized:
            return []

        scored: list[tuple[int, Company]] = []
        for company in self._companies:
            code = company.stockCode.lower()
            name = company.name.lower()
            haystack = f"{code} {name} {company.industryName.lower()}"
            if normalized == code or normalized == name:
                scored.append((0, company))
            elif code.startswith(normalized) or name.startswith(normalized):
                scored.append((1, company))
            elif normalized in haystack:
                scored.append((2, company))

        return [company for _, company in sorted(scored, key=lambda item: (item[0], item[1].stockCode))[:limit]]

    def get_company(self, stock_code: str) -> Optional[Company]:
        return next((company for company in self._companies if company.stockCode == stock_code), None)

    def get_snapshot(self, stock_code: str) -> Optional[FundamentalSnapshot]:
        return self._fundamentals.get(stock_code)

    def list_snapshots(self, settings: ScannerSettings) -> list[FundamentalSnapshot]:
        snapshots = []
        for snapshot in self._fundamentals.values():
            if snapshot.company.market == "TWSE" and not settings.scan_twse:
                continue
            if snapshot.company.market == "TPEX" and not settings.scan_tpex:
                continue
            snapshots.append(snapshot)
        return snapshots
