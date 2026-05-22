import json
import os
import tempfile
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
        try:
            save_settings(recovered)
        except OSError:
            pass
        return recovered


def save_settings(settings: ScannerSettings) -> ScannerSettings:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(settings.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except OSError:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return settings
