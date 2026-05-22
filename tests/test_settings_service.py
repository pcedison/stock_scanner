import json
from pathlib import Path

import pytest

from backend.models.settings import ScannerSettings
import backend.services.settings_service as settings_service


def test_default_settings_path_uses_local_runtime_file(monkeypatch):
    monkeypatch.delenv("SETTINGS_PATH", raising=False)

    assert settings_service.settings_path() == settings_service.ROOT_DIR / "data" / "settings.local.json"


def test_settings_path_env_override_is_used_for_load_and_save(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "custom-settings.json"
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings = ScannerSettings(manual_scan_enabled=False, scan_tpex=False)

    assert settings_service.save_settings(settings) == settings

    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    assert settings_service.load_settings().model_dump() == settings.model_dump()


def test_corrupt_settings_recover_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("SETTINGS_PATH", str(path))

    recovered = settings_service.load_settings()

    assert recovered.model_dump() == ScannerSettings().model_dump()
    assert json.loads(path.read_text(encoding="utf-8")) == ScannerSettings().model_dump()


def test_save_settings_keeps_existing_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    original = ScannerSettings(manual_scan_enabled=True)
    path.write_text(original.model_dump_json(indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    real_replace = Path.replace

    def fail_replace(self, target):
        if self == path.with_suffix(".tmp") and Path(target) == path:
            raise OSError("replace failed")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        settings_service.save_settings(ScannerSettings(manual_scan_enabled=False))

    persisted = ScannerSettings.model_validate_json(path.read_text(encoding="utf-8"))
    assert persisted.manual_scan_enabled is True
    assert path.with_suffix(".tmp").exists()
