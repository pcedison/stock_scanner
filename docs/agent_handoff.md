# Agent Handoff Protocol

Use this protocol when Codex and Claude Code work on the same repository.

## Start of Work

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
python scripts\write_agent_handoff.py --repo pcedison/stock_scanner
```

The generated files in `.tmp/agent_handoff.json` and `.tmp/agent_handoff.md`
are the shared snapshot. If either agent sees blockers, uncommitted changes, a
local SHA that differs from `origin/main`, or a failed latest deploy, it should
stop and resolve synchronization before editing.

## Before Handoff

```powershell
python -m pytest -q
python scripts\check_release_sync.py --repo pcedison/stock_scanner
python scripts\write_agent_handoff.py --repo pcedison/stock_scanner
```

When production verification is needed, keep the default strict seed gates:

```powershell
python scripts\write_agent_handoff.py `
  --repo pcedison/stock_scanner `
  --check-production-health `
  --check-production-smoke `
  --health-url https://stock-scanner-beta-api.pcedison.workers.dev/api/health
```

The strict path rejects stale production seed data and offline-seed production
payloads by default.
