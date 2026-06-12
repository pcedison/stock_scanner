import sys

import pytest


@pytest.fixture(autouse=True)
def _drain_global_scan_cache_jobs(monkeypatch):
    # Keep pytest monkeypatches active until queued refresh jobs finish.
    _ = monkeypatch
    yield
    deps_module = sys.modules.get("backend.dependencies")
    if deps_module is None:
        return
    deps_module.scan_cache_service.wait_for_idle(timeout=5)
