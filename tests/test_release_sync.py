import json
import subprocess
import sys

from scripts import check_release_sync, write_agent_handoff

ORIGIN_SHA = "b" * 40
STALE_SHA = "a" * 40


def _completed(args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _run_main(monkeypatch, capsys, fake_run, argv=None):
    monkeypatch.setattr(check_release_sync.subprocess, "run", fake_run)
    exit_code = check_release_sync.main(argv or ["--repo", "acme/stock-scanner"])
    output = json.loads(capsys.readouterr().out)
    return exit_code, output


def _run_payload(runs):
    return json.dumps({"workflow_runs": runs})


def _workflow_run(run_id, sha, *, status="completed", conclusion="success", branch="main"):
    return {
        "id": run_id,
        "name": "Deploy to Cloudflare",
        "workflow_id": 123,
        "head_branch": branch,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "event": "push",
        "html_url": f"https://github.example/runs/{run_id}",
        "created_at": "2026-05-22T01:00:00Z",
        "updated_at": "2026-05-22T01:05:00Z",
    }


def test_release_sync_ok_when_head_origin_and_latest_runs_match(monkeypatch, capsys):
    validate = _workflow_run(10, ORIGIN_SHA)
    deploy = _workflow_run(20, ORIGIN_SHA)

    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "rev-parse", "origin/main"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "branch", "--show-current"):
            return _completed(args, "main\n")
        if command == ("git", "status", "--porcelain"):
            return _completed(args, "")
        if command == ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _completed(args, "0\t0\n")
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/ci.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([validate]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([deploy]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=50",
        ):
            return _completed(args, _run_payload([deploy]))
        raise AssertionError(f"unexpected command: {command}")

    exit_code, output = _run_main(monkeypatch, capsys, fake_run)

    assert exit_code == 0
    assert output["ok"] is True
    assert output["dryRun"] is True
    assert output["blockers"] == []
    assert output["git"]["headMatchesOriginMain"] is True


def test_stale_deploy_run_blocks_but_dry_run_does_not_cancel(monkeypatch, capsys):
    validate = _workflow_run(10, ORIGIN_SHA)
    latest_deploy = _workflow_run(20, ORIGIN_SHA)
    stale_deploy = _workflow_run(19, STALE_SHA, status="waiting", conclusion=None)
    cancel_calls = []

    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "rev-parse", "origin/main"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "branch", "--show-current"):
            return _completed(args, "main\n")
        if command == ("git", "status", "--porcelain"):
            return _completed(args, "")
        if command == ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _completed(args, "0\t0\n")
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/ci.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([validate]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([latest_deploy]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=50",
        ):
            return _completed(args, _run_payload([latest_deploy, stale_deploy]))
        if command[-1].endswith("/cancel"):
            cancel_calls.append(command)
            return _completed(args, "")
        raise AssertionError(f"unexpected command: {command}")

    exit_code, output = _run_main(monkeypatch, capsys, fake_run)

    assert exit_code == 1
    assert output["ok"] is False
    assert output["dryRun"] is True
    assert output["staleDeployRuns"][0]["id"] == 19
    assert output["cancellations"] == [{"cancelled": False, "dryRun": True, "headSha": STALE_SHA, "id": 19}]
    assert cancel_calls == []


def test_cancel_stale_deploys_only_cancels_old_main_blocking_runs(monkeypatch, capsys):
    validate = _workflow_run(10, ORIGIN_SHA)
    latest_deploy = _workflow_run(20, ORIGIN_SHA, status="queued", conclusion=None)
    stale_waiting = _workflow_run(19, STALE_SHA, status="waiting", conclusion=None)
    stale_feature = _workflow_run(18, STALE_SHA, status="waiting", conclusion=None, branch="feature")
    old_completed = _workflow_run(17, STALE_SHA, status="completed", conclusion="failure")
    origin_waiting = _workflow_run(16, ORIGIN_SHA, status="waiting", conclusion=None)
    cancel_calls = []

    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "rev-parse", "origin/main"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "branch", "--show-current"):
            return _completed(args, "main\n")
        if command == ("git", "status", "--porcelain"):
            return _completed(args, "")
        if command == ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _completed(args, "0\t0\n")
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/ci.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([validate]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([latest_deploy]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=50",
        ):
            return _completed(
                args,
                _run_payload([latest_deploy, stale_waiting, stale_feature, old_completed, origin_waiting]),
            )
        if command == ("gh", "api", "--method", "POST", "/repos/acme/stock-scanner/actions/runs/19/cancel"):
            cancel_calls.append(command)
            return _completed(args, "")
        raise AssertionError(f"unexpected command: {command}")

    exit_code, output = _run_main(
        monkeypatch,
        capsys,
        fake_run,
        ["--repo", "acme/stock-scanner", "--cancel-stale-deploys"],
    )

    assert exit_code == 1
    assert output["dryRun"] is False
    assert [run["id"] for run in output["staleDeployRuns"]] == [19]
    assert output["cancellations"] == [{"cancelled": True, "dryRun": False, "headSha": STALE_SHA, "id": 19}]
    assert len(cancel_calls) == 1


def test_dirty_or_diverged_local_state_is_blocking(monkeypatch, capsys):
    local_sha = "c" * 40
    validate = _workflow_run(10, ORIGIN_SHA)
    deploy = _workflow_run(20, ORIGIN_SHA)

    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, local_sha)
        if command == ("git", "rev-parse", "origin/main"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "branch", "--show-current"):
            return _completed(args, "release-check\n")
        if command == ("git", "status", "--porcelain"):
            return _completed(args, " M README.md\n")
        if command == ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _completed(args, "0\t1\n")
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/ci.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([validate]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([deploy]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=50",
        ):
            return _completed(args, _run_payload([deploy]))
        raise AssertionError(f"unexpected command: {command}")

    exit_code, output = _run_main(monkeypatch, capsys, fake_run)

    assert exit_code == 1
    assert "Local HEAD does not match origin/main" in output["blockers"]
    assert "Working tree has uncommitted changes" in output["blockers"]
    assert output["git"]["workingTreeChanges"] == [" M README.md"]
    assert output["git"]["aheadOfOriginMain"] == 1


def test_tool_error_returns_exit_code_2(monkeypatch, capsys):
    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, "", "fatal: not a git repository", 128)
        raise AssertionError(f"unexpected command: {command}")

    exit_code, output = _run_main(monkeypatch, capsys, fake_run)

    assert exit_code == 2
    assert output["errorType"] == "tool_or_config"


def test_production_checks_use_strict_seed_gates_by_default(monkeypatch, capsys):
    validate = _workflow_run(10, ORIGIN_SHA)
    deploy = _workflow_run(20, ORIGIN_SHA)
    optional_commands = []

    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "rev-parse", "origin/main"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "branch", "--show-current"):
            return _completed(args, "main\n")
        if command == ("git", "status", "--porcelain"):
            return _completed(args, "")
        if command == ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _completed(args, "0\t0\n")
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/ci.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([validate]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([deploy]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=50",
        ):
            return _completed(args, _run_payload([deploy]))
        if command[0] == sys.executable:
            optional_commands.append(command)
            return _completed(args, "{}")
        raise AssertionError(f"unexpected command: {command}")

    exit_code, output = _run_main(
        monkeypatch,
        capsys,
        fake_run,
        [
            "--repo",
            "acme/stock-scanner",
            "--check-production-health",
            "--check-production-smoke",
            "--health-url",
            "https://worker.example/api/health",
        ],
    )

    assert exit_code == 0
    assert output["ok"] is True
    assert len(optional_commands) == 2
    for command in optional_commands:
        assert "--max-cache-age-hours" in command
        assert "36" in command
        assert "--reject-offline-seed" in command


def test_write_agent_handoff_outputs_shared_snapshot(monkeypatch, tmp_path, capsys):
    validate = _workflow_run(10, ORIGIN_SHA)
    deploy = _workflow_run(20, ORIGIN_SHA)

    def fake_run(args, **kwargs):
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "rev-parse", "origin/main"):
            return _completed(args, ORIGIN_SHA)
        if command == ("git", "branch", "--show-current"):
            return _completed(args, "main\n")
        if command == ("git", "status", "--porcelain"):
            return _completed(args, "")
        if command == ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _completed(args, "0\t0\n")
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/ci.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([validate]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=10",
        ):
            return _completed(args, _run_payload([deploy]))
        if command == (
            "gh",
            "api",
            "/repos/acme/stock-scanner/actions/workflows/cloudflare-deploy.yml/runs?branch=main&per_page=50",
        ):
            return _completed(args, _run_payload([deploy]))
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(check_release_sync.subprocess, "run", fake_run)
    json_output = tmp_path / "handoff.json"
    markdown_output = tmp_path / "handoff.md"

    exit_code = write_agent_handoff.main(
        [
            "--repo",
            "acme/stock-scanner",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )
    printed = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert printed["releaseSync"]["ok"] is True
    assert json.loads(json_output.read_text(encoding="utf-8"))["releaseSync"]["git"]["localHead"] == ORIGIN_SHA
    assert "Required next-agent startup" in markdown_output.read_text(encoding="utf-8")
