# Current Architecture Snapshot

Last reviewed: 2026-05-17

This project is no longer a localStorage-only prototype. The current shape is:

- Frontend: static Pages-compatible app in `frontend/`, with local fallback only for degraded/offline use.
- Local API: FastAPI in `backend/`, backed by SQLite auth/session/settings state.
- Edge API: Python Cloudflare Worker in `cloudflare/`, backed by D1 sessions/holdings/settings and R2 cache objects.
- Shared expectation: FastAPI and Worker responses are guarded by contract tests under `tests/test_api_worker_contracts.py`.
- Deployment: feature branches run validation only; `main`, scheduled, and manual workflow runs perform the actual Cloudflare upload/deploy.
- Seed data: CI/deploy use the committed `data/official_cache_seed_2026-05-14.zip` in offline mode. The zip contains both official history inputs and the generated `cloudflare_seed/*` payload, so deploy validation does not depend on live TWSE/TPEx/MOPS APIs.
- Runtime health: Worker `/api/health` and app status expose cache manifest quality counts and mark undersized cache payloads as degraded.
- Security defaults: production FastAPI must use explicit HTTPS CORS origins and secure session cookies.

Useful commands:

```powershell
python scripts\validate_cloudflare_seed_inputs.py --zip data\official_cache_seed_2026-05-14.zip
$env:CLOUDFLARE_SEED_MODE='offline'; python scripts\build_cloudflare_seed.py; Remove-Item Env:\CLOUDFLARE_SEED_MODE
python -m pytest -q
npm run test:e2e
```

When refreshing committed seed inputs, rebuild `cloudflare/seed` first, then run:

```powershell
python scripts\package_cloudflare_seed_cache.py
```

The package script rewrites the zip and its `.sha256` sidecar from the current committed-data snapshot.
