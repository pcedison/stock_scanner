import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock

from backend.models.settings import ScannerSettings

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = ROOT_DIR / "data" / "settings.local.json"
SETTINGS_PATH_ENV = "SETTINGS_PATH"

logger = logging.getLogger(__name__)

_settings_cache: dict = {}
_settings_cache_lock = Lock()


def settings_path() -> Path:
    return Path(os.getenv(SETTINGS_PATH_ENV, DEFAULT_SETTINGS_PATH))


def _file_sig(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return None


def load_settings() -> ScannerSettings:
    path = settings_path()
    sig = _file_sig(path)
    if sig is None:
        return ScannerSettings()

    with _settings_cache_lock:
        if _settings_cache.get("sig") == sig:
            return _settings_cache["result"]

    try:
        result = ScannerSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning("Settings file at %s is corrupt or unreadable; reverting to defaults", path)
        result = ScannerSettings()
        try:
            save_settings(result)
        except OSError as exc:
            logger.debug("Unable to rewrite default settings at %s: %s", path, exc)

    with _settings_cache_lock:
        if _settings_cache.get("sig") != sig:
            _settings_cache["sig"] = sig
            _settings_cache["result"] = result
        else:
            result = _settings_cache["result"]
    return result


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
            except OSError as exc:
                logger.debug("Unable to remove temporary settings file %s: %s", tmp_path, exc)
        raise
    with _settings_cache_lock:
        _settings_cache["sig"] = _file_sig(path)
        _settings_cache["result"] = settings
    return settings
