import json
import os
from pathlib import Path

from backend.models.settings import ScannerSettings


ROOT_DIR = Path(__file__).resolve().parents[2]
SETTINGS_PATH = Path(os.getenv("SETTINGS_PATH", ROOT_DIR / "data" / "settings.json"))


def load_settings() -> ScannerSettings:
    if not SETTINGS_PATH.exists():
        return ScannerSettings()

    try:
        return ScannerSettings.model_validate_json(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        save_settings(ScannerSettings())
        return ScannerSettings()


def save_settings(settings: ScannerSettings) -> ScannerSettings:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    return settings
