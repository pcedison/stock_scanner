# Main Release Handoff Protocol

Use this protocol only when an agent is handing off main-branch release readiness or when the user explicitly asks for a shared Codex/Claude snapshot. Ordinary local tasks do not need it.

Success means the next agent can distinguish the current working tree, locally known `origin/main`, latest Validate/Deploy runs, and any production check results without changing branches or discarding work.

## Create the snapshot

First inspect the current workspace without mutating it:

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
```

When a fresh remote comparison is required, run `git fetch --prune origin`, then generate the snapshot:

```powershell
python scripts\write_agent_handoff.py --repo pcedison/stock_scanner
```

The command reads local Git state and GitHub workflow state, then writes `.tmp/agent_handoff.json` and `.tmp/agent_handoff.md`. By default it does not cancel runs, push, pull, switch branches, deploy, or change production.

Treat a dirty tree, a non-main branch, an ahead/behind result, or a failed workflow as evidence to reconcile with the assigned task. Do not automatically run `git switch`, `git pull`, reset, or discard changes merely to make the snapshot green.

## Handoff completion bar

Run validation that covers the actual change. Use the full Python suite only when the scope warrants it; otherwise record the targeted checks in the handoff message. Then regenerate the snapshot so its SHA and workflow evidence are current:

```powershell
python scripts\check_release_sync.py --repo pcedison/stock_scanner
python scripts\write_agent_handoff.py --repo pcedison/stock_scanner
```

When production verification is part of the requested release check, keep the strict seed gates:

```powershell
python scripts\write_agent_handoff.py `
  --repo pcedison/stock_scanner `
  --check-production-health `
  --check-production-smoke `
  --health-url https://stock-scanner-beta-api.pcedison.workers.dev/api/health
```

The strict path rejects stale production seed data and offline-seed production payloads by default. `--cancel-stale-deploys` performs an external GitHub mutation and requires explicit authorization; the default dry-run only reports candidates.
