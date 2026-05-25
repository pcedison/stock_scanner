from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT_DIR / "frontend" / "index.html"
PAGES_HOST_SUFFIX = ".pages.dev"
ASSET_PATTERN = re.compile(r"""(?:src|href)=["']([^"']+\?v=[^"']+)["']""")
CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-release-check/1.0"
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def validate_pages_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(PAGES_HOST_SUFFIX):
        raise RuntimeError("Pages frontend check URL must be an https://*.pages.dev URL")
    return url


def validate_project_name(project_name: str) -> str:
    if not PROJECT_NAME_PATTERN.fullmatch(project_name):
        raise RuntimeError("Cloudflare Pages project name contains unsupported characters")
    return project_name


def expected_cache_busted_assets(index_path: Path = DEFAULT_INDEX) -> list[str]:
    html = index_path.read_text(encoding="utf-8")
    return sorted(dict.fromkeys(ASSET_PATTERN.findall(html)))


def fetch_pages_html(url: str, timeout: int) -> str:
    safe_url = validate_pages_url(url)
    request = Request(safe_url, headers={"cache-control": "no-cache", "user-agent": CHECK_USER_AGENT})
    try:
        # validate_pages_url restricts requests to the Cloudflare Pages HTTPS domain.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            if response.status != 200:
                raise RuntimeError(f"{safe_url} returned HTTP {response.status}")
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{safe_url} returned HTTP {exc.code}: {body[:1000]}") from exc
    except URLError as exc:
        raise RuntimeError(f"{safe_url} could not be fetched: {exc}") from exc


def missing_assets(remote_html: str, expected_assets: Iterable[str]) -> list[str]:
    return [asset for asset in expected_assets if asset not in remote_html]


def validate_pages_frontend(url: str, index_path: Path = DEFAULT_INDEX, timeout: int = 10) -> dict[str, object]:
    expected = expected_cache_busted_assets(index_path)
    html = fetch_pages_html(url, timeout)
    missing = missing_assets(html, expected)
    if missing:
        raise RuntimeError(f"Pages frontend is missing cache-busted assets: {', '.join(missing)}")
    return {"url": validate_pages_url(url), "checkedAssets": expected, "ok": True}


def fetch_pages_deployments(project_name: str, timeout: int = 30) -> list[dict[str, object]]:
    project = validate_project_name(project_name)
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise RuntimeError("npx is required to inspect Cloudflare Pages deployments")

    completed = subprocess.run(
        [npx, "wrangler", "pages", "deployment", "list", "--project-name", project, "--json"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"Could not inspect Cloudflare Pages deployments: {detail[:1000]}")
    try:
        deployments = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Cloudflare Pages deployment list did not return JSON") from exc
    if not isinstance(deployments, list):
        raise RuntimeError("Cloudflare Pages deployment list returned an unexpected payload")
    return deployments


def latest_production_deployment(deployments: Iterable[dict[str, object]]) -> dict[str, object]:
    for deployment in deployments:
        if str(deployment.get("Environment") or "").lower() == "production":
            return deployment
    raise RuntimeError("Cloudflare Pages has no production deployments")


def deployment_source_matches(actual_source: str, expected_source: str) -> bool:
    actual = actual_source.strip().lower()
    expected = expected_source.strip().lower()
    if not expected:
        return True
    if len(actual) < 7 or len(expected) < 7:
        return actual == expected
    return expected.startswith(actual) or actual.startswith(expected)


def validate_pages_deployment_metadata(
    deployments: Iterable[dict[str, object]],
    expected_branch: str = "",
    expected_source: str = "",
) -> dict[str, object]:
    deployment = latest_production_deployment(deployments)
    branch = str(deployment.get("Branch") or "")
    source = str(deployment.get("Source") or "")

    if expected_branch and branch != expected_branch:
        raise RuntimeError(f"Latest Pages production deployment branch is {branch!r}, expected {expected_branch!r}")
    if expected_source and not deployment_source_matches(source, expected_source):
        raise RuntimeError(f"Latest Pages production deployment source is {source!r}, expected {expected_source!r}")
    return deployment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the public Cloudflare Pages frontend and latest production deployment metadata."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--project", help="Cloudflare Pages project name for deployment metadata verification.")
    parser.add_argument("--expected-branch", default="")
    parser.add_argument("--expected-source", default="")
    parser.add_argument("--metadata-timeout", type=int, default=30)
    args = parser.parse_args(argv)

    try:
        result = validate_pages_frontend(args.url, args.index, args.timeout)
        if args.project:
            deployments = fetch_pages_deployments(args.project, args.metadata_timeout)
            result["latestProductionDeployment"] = validate_pages_deployment_metadata(
                deployments,
                expected_branch=args.expected_branch,
                expected_source=args.expected_source,
            )
        elif args.expected_branch or args.expected_source:
            raise RuntimeError("--project is required when --expected-branch or --expected-source is set")
    except RuntimeError as exc:
        print(f"Pages frontend check failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
