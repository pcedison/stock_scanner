from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

try:
    from seed_utils import resolve_seed_zip
except ModuleNotFoundError:  # Imported as scripts.hydrate_cloudflare_seed_inputs under pytest.
    from scripts.seed_utils import resolve_seed_zip

REQUIRED_OFFICIAL_ENTRIES = (
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
)
MONTHLY_HISTORY_ENTRY = "monthly_revenue_history.json"


class HistoryCoverageError(ValueError):
    pass


def _month_key(value: object) -> tuple[int, int]:
    try:
        year_text, month_text = str(value).split("-", 1)
        year, month = int(year_text), int(month_text)
    except (TypeError, ValueError):
        return (0, 0)
    return (year, month) if 1 <= month <= 12 else (0, 0)


def _previous_month(value: str) -> str:
    year, month = _month_key(value)
    if not year:
        return ""
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _valid_history(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("months"), dict):
        return None
    return value


def _read_history(path: Path) -> dict[str, Any] | None:
    try:
        return _valid_history(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _history_from_zip(archive: zipfile.ZipFile) -> dict[str, Any] | None:
    try:
        return _valid_history(json.loads(archive.read(MONTHLY_HISTORY_ENTRY).decode("utf-8")))
    except (KeyError, UnicodeError, json.JSONDecodeError):
        return None


def _merge_histories(histories: list[dict[str, Any]]) -> dict[str, Any]:
    merged_months: dict[str, dict[str, dict[str, Any]]] = {}
    updated_values: list[str] = []
    for history in histories:
        updated_at = history.get("updatedAt")
        if isinstance(updated_at, str) and updated_at:
            updated_values.append(updated_at)
        for stock_code, records in history.get("months", {}).items():
            if not isinstance(stock_code, str) or not isinstance(records, dict):
                continue
            target = merged_months.setdefault(stock_code, {})
            for month, record in records.items():
                if _month_key(month) != (0, 0) and isinstance(record, dict):
                    target[month] = record
    ordered = {
        stock_code: dict(sorted(records.items(), key=lambda item: _month_key(item[0])))
        for stock_code, records in sorted(merged_months.items())
        if records
    }
    return {
        "schemaVersion": 1,
        "updatedAt": max(updated_values) if updated_values else None,
        "months": ordered,
    }


def monthly_history_coverage(history: dict[str, Any]) -> dict[str, Any]:
    months = history.get("months", {})
    all_months = sorted(
        {month for records in months.values() if isinstance(records, dict) for month in records if _month_key(month) != (0, 0)},
        key=_month_key,
    )
    latest = all_months[-1] if all_months else ""
    previous = _previous_month(latest)
    latest_companies = 0
    consecutive_companies = 0
    for records in months.values():
        if not isinstance(records, dict) or latest not in records:
            continue
        latest_companies += 1
        if previous in records:
            consecutive_companies += 1
    return {
        "companies": len(months),
        "latestMonth": latest or None,
        "previousMonth": previous or None,
        "latestMonthCompanies": latest_companies,
        "consecutiveCompanies": consecutive_companies,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def hydrate_seed_inputs(
    data_dir: Path,
    r2_history_path: Path | None,
    *,
    min_consecutive_companies: int = 1000,
    extract_official: bool = True,
) -> dict[str, Any]:
    seed_zip = resolve_seed_zip(data_dir)
    checkout_history = _read_history(data_dir / MONTHLY_HISTORY_ENTRY)
    ignored_optional = 0
    r2_history = None
    if r2_history_path is not None and r2_history_path.exists():
        r2_history = _read_history(r2_history_path)
        ignored_optional = int(r2_history is None)

    with zipfile.ZipFile(seed_zip) as archive:
        zip_history = _history_from_zip(archive)
        required_payloads = {}
        for name in REQUIRED_OFFICIAL_ENTRIES:
            try:
                raw = archive.read(name)
                json.loads(raw.decode("utf-8"))
            except KeyError as exc:
                raise ValueError(f"Seed zip is missing required official entry: {name}") from exc
            required_payloads[name] = raw

    histories = [history for history in (zip_history, checkout_history, r2_history) if history is not None]
    if not histories:
        raise HistoryCoverageError("No valid monthly revenue history is available")
    merged = _merge_histories(histories)
    summary = monthly_history_coverage(merged)
    if summary["consecutiveCompanies"] < max(0, min_consecutive_companies):
        raise HistoryCoverageError(
            "Monthly revenue history lacks consecutive monthly history: "
            f"{summary['consecutiveCompanies']} companies cover {summary['previousMonth']} -> {summary['latestMonth']}; "
            f"minimum is {min_consecutive_companies}"
        )

    if extract_official:
        for name, raw in required_payloads.items():
            _atomic_write(data_dir / name, raw)
    encoded = json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_write(data_dir / MONTHLY_HISTORY_ENTRY, encoded)
    return {
        **summary,
        "seedZip": seed_zip.name,
        "historySources": len(histories),
        "ignoredOptionalCandidates": ignored_optional,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hydrate and merge persistent Cloudflare seed inputs")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--r2-history", type=Path)
    parser.add_argument("--min-consecutive-companies", type=int, default=1000)
    parser.add_argument("--skip-official-extract", action="store_true")
    args = parser.parse_args(argv)
    summary = hydrate_seed_inputs(
        args.data_dir,
        args.r2_history,
        min_consecutive_companies=args.min_consecutive_companies,
        extract_official=not args.skip_official_extract,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
