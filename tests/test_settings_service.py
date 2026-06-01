import json
from pathlib import Path

import pytest

import backend.services.settings_service as settings_service
from backend.models.settings import ScannerSettings


def test_default_settings_path_uses_local_runtime_file(monkeypatch):
    monkeypatch.delenv("SETTINGS_PATH", raising=False)

    assert settings_service.settings_path() == settings_service.ROOT_DIR / "data" / "settings.local.json"


def test_settings_path_env_override_is_used_for_load_and_save(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "custom-settings.json"
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()
    settings = ScannerSettings(manual_scan_enabled=False, scan_tpex=False)

    assert settings_service.save_settings(settings) == settings

    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    assert settings_service.load_settings().model_dump() == settings.model_dump()


def test_save_settings_refreshes_cache_when_file_signature_is_reused(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()

    original = ScannerSettings(manual_scan_enabled=True)
    updated = ScannerSettings(manual_scan_enabled=False)
    settings_service.save_settings(original)
    assert settings_service.load_settings().manual_scan_enabled is True
    cached_sig = settings_service._settings_cache["sig"]
    monkeypatch.setattr(settings_service, "_file_sig", lambda _: cached_sig)

    settings_service.save_settings(updated)

    assert settings_service.load_settings().manual_scan_enabled is False


def test_corrupt_settings_recover_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()

    recovered = settings_service.load_settings()

    assert recovered.model_dump() == ScannerSettings().model_dump()
    assert json.loads(path.read_text(encoding="utf-8")) == ScannerSettings().model_dump()


def test_save_settings_keeps_existing_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    original = ScannerSettings(manual_scan_enabled=True)
    path.write_text(original.model_dump_json(indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()
    real_replace = Path.replace

    def fail_replace(self, target):
        if self.parent == path.parent and self.name.startswith(f".{path.name}.") and Path(target) == path:
            raise OSError("replace failed")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        settings_service.save_settings(ScannerSettings(manual_scan_enabled=False))

    persisted = ScannerSettings.model_validate_json(path.read_text(encoding="utf-8"))
    assert persisted.manual_scan_enabled is True
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_load_settings_returns_defaults_when_file_absent(tmp_path, monkeypatch):
    path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()

    result = settings_service.load_settings()

    assert result.model_dump() == ScannerSettings().model_dump()
    assert not path.exists()  # absent file isn't created on read


def test_corrupt_settings_logs_when_default_rewrite_fails(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()

    def boom(_settings):
        raise OSError("cannot save")

    monkeypatch.setattr(settings_service, "save_settings", boom)

    recovered = settings_service.load_settings()  # save failure is logged, not raised

    assert recovered.model_dump() == ScannerSettings().model_dump()


def test_load_settings_returns_cached_result_on_concurrent_population(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    path.write_text(ScannerSettings().model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()
    sig = settings_service._file_sig(path)
    sentinel = ScannerSettings(manual_scan_enabled=False)
    real_validate = ScannerSettings.model_validate_json

    def populate_then_parse(data):
        # mimic another thread filling the cache for this sig mid-read
        settings_service._settings_cache["sig"] = sig
        settings_service._settings_cache["result"] = sentinel
        return real_validate(data)

    monkeypatch.setattr(settings_service.ScannerSettings, "model_validate_json", populate_then_parse)

    assert settings_service.load_settings() is sentinel  # keeps the concurrently-cached result


def test_save_settings_swallows_temp_cleanup_failure(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.json"
    monkeypatch.setenv("SETTINGS_PATH", str(path))
    settings_service._settings_cache.clear()
    real_replace = Path.replace

    def fail_replace(self, target):
        if Path(target) == path:
            raise OSError("replace failed")
        return real_replace(self, target)

    def fail_unlink(self, missing_ok=False):
        raise OSError("cleanup failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    # The original replace error propagates; the cleanup failure is only logged.
    with pytest.raises(OSError, match="replace failed"):
        settings_service.save_settings(ScannerSettings())
