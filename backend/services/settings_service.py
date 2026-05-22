import json
import os
from pathlib import Path

from backend.models.settings import ScannerSettings


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = ROOT_DIR / "data" / "settings.local.json"
SETTINGS_PATH_ENV = "SETTINGS_PATH"


def settings_path() -> Path:
    return Path(os.getenv(SETTINGS_PATH_ENV, DEFAULT_SETTINGS_PATH))


def load_settings() -> ScannerSettings:
    path = settings_path()
    if not path.exists():
        return ScannerSettings()

    try:
        return ScannerSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        recovered = ScannerSettings()
        save_settings(recovered)
        return recovered


def save_settings(settings: ScannerSettings) -> ScannerSettings:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(settings.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return settings
