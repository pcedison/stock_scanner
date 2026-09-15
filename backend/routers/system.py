"""System / status endpoints: health, app-status, integrations, backtest, settings."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Request

from backend import dependencies as deps
from backend.dependencies import _backtest_status_cached, official_provider
from backend.models.settings import ScannerSettings
from backend.services.integrations import integration_status
from backend.services.scheduler import should_wake_up
from backend.services.settings_service import save_settings

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    settings = deps.load_settings()
    return {
        "status": "ok",
        "dataSource": "mock" if settings.use_mock_data else "official_twse_tpex",
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/api/runtime-config")
def runtime_config() -> dict:
    # Parity with the Worker's /api/runtime-config so local development follows the
    # same contract. The v1 whole-scan read is retired, so the version is always v2;
    # FastAPI never sits behind the Cloudflare edge cache.
    return {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False}


@router.get("/api/integrations/status")
def integrations_status() -> dict:
    return integration_status()


@router.get("/api/backtest")
def backtest_status() -> dict:
    return _backtest_status_cached()


@router.get("/api/app-status")
def app_status(today: date | None = None) -> dict:
    settings = deps.load_settings()
    official_status = official_provider.status(refresh=False) if not settings.use_mock_data else None
    data_source_payload = {
        "activeProvider": "MockDataProvider" if settings.use_mock_data else "OfficialDataProvider",
        "mockDataAvailable": True,
        "officialDataAvailable": official_status is not None,
    }
    scheduler_payload = should_wake_up(today).__dict__
    auto_scan_payload = {
        "action": "ready",
        "autoScanEnabled": settings.auto_scan_full_market,
        "manualScanEnabled": settings.manual_scan_enabled,
        "scan": None,
    }
    return {
        "dataSourceStatus": data_source_payload,
        "schedulerStatus": scheduler_payload,
        "schedulerAutoScan": auto_scan_payload,
        "integrationStatus": integration_status(),
        "backtestStatus": _backtest_status_cached(),
    }


@router.get("/api/settings")
def get_settings() -> ScannerSettings:
    return deps.load_settings()


@router.put("/api/settings")
def put_settings(settings: ScannerSettings, request: Request) -> ScannerSettings:
    deps._require_super_user(request)
    return save_settings(settings)
