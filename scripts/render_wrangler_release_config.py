from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
CLOUDFLARE_DIR = ROOT_DIR / "cloudflare"
PRODUCTION_CONFIG = CLOUDFLARE_DIR / "wrangler.toml"
STAGING_TEMPLATE = CLOUDFLARE_DIR / "wrangler.staging.template.toml"
# Committed production crons (cloudflare/wrangler.toml) are the source of truth; the
# release profiles keep them and only guarantee dispatch stays enabled alongside them.
PRODUCTION_CRONS = ["*/20 21-23 * * SUN-THU", "*/20 0-13 * * MON-FRI"]
SENTINEL_PATTERN = re.compile(r"__[A-Z0-9_]+__")
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ALLOWED_PRODUCTION_SWITCHES = {
    ("triggers", "crons"),
    ("vars", "GITHUB_DISPATCH_ENABLED"),
    ("vars", "MARKET_SCAN_API_VERSION"),
    ("vars", "EDGE_CACHE_ENABLED"),
    ("cache", "enabled"),
}


def _load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _production_database_id(production_config: Path = PRODUCTION_CONFIG) -> str:
    config = _load_toml(production_config)
    return str(config["d1_databases"][0]["database_id"])


def _production_bucket_name(production_config: Path = PRODUCTION_CONFIG) -> str:
    config = _load_toml(production_config)
    return str(config["r2_buckets"][0]["bucket_name"])


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, list) and all(not isinstance(item, dict) for item in value):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML scalar: {value!r}")


def _emit_table(lines: list[str], name: str, table: dict[str, Any]) -> None:
    scalar_items = [(key, value) for key, value in table.items() if not isinstance(value, (dict, list))]
    list_scalars = [
        (key, value) for key, value in table.items() if isinstance(value, list) and not any(isinstance(i, dict) for i in value)
    ]
    if scalar_items or list_scalars:
        lines.append(f"[{name}]")
        for key, value in [*scalar_items, *list_scalars]:
            lines.append(f"{key} = {_toml_scalar(value)}")
        lines.append("")
    for key, value in table.items():
        if isinstance(value, dict):
            _emit_table(lines, f"{name}.{key}", value)


def dumps_toml(config: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in config.items():
        if isinstance(value, dict) or (isinstance(value, list) and any(isinstance(item, dict) for item in value)):
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    if lines:
        lines.append("")
    for key, value in config.items():
        if isinstance(value, dict):
            _emit_table(lines, key, value)
    for key, value in config.items():
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            for item in value:
                if not isinstance(item, dict):
                    raise TypeError(f"mixed TOML array is not supported for {key}")
                lines.append(f"[[{key}]]")
                for item_key, item_value in item.items():
                    if isinstance(item_value, (dict, list)) and not (
                        isinstance(item_value, list) and not any(isinstance(i, dict) for i in item_value)
                    ):
                        raise TypeError(f"nested array-table value is not supported for {key}.{item_key}")
                    lines.append(f"{item_key} = {_toml_scalar(item_value)}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _reject_sentinels(text: str) -> None:
    match = SENTINEL_PATTERN.search(text)
    if match:
        raise ValueError(f"generated config still contains sentinel {match.group(0)}")


def _validate_staging_inputs(
    database_id: str,
    bucket_name: str,
    production_database_id: str,
    production_bucket_name: str,
) -> None:
    if not UUID_PATTERN.fullmatch(database_id):
        raise ValueError("staging database id must be a UUID")
    if database_id == production_database_id:
        raise ValueError("staging database id must not equal production database id")
    if not bucket_name or bucket_name == production_bucket_name:
        raise ValueError("staging bucket must not equal production bucket")
    if "__" in database_id or "__" in bucket_name:
        raise ValueError("staging values must not contain sentinel markers")


def render_staging_config(
    *,
    template_path: Path = STAGING_TEMPLATE,
    database_id: str,
    bucket_name: str,
    production_database_id: str | None = None,
    production_bucket_name: str | None = None,
) -> str:
    production_database_id = production_database_id or _production_database_id()
    production_bucket_name = production_bucket_name or _production_bucket_name()
    _validate_staging_inputs(database_id, bucket_name, production_database_id, production_bucket_name)
    text = template_path.read_text(encoding="utf-8")
    text = text.replace("__STAGING_D1_DATABASE_ID__", database_id).replace("__STAGING_R2_BUCKET_NAME__", bucket_name)
    _reject_sentinels(text)
    config = tomllib.loads(text)
    if config["vars"]["GITHUB_DISPATCH_ENABLED"] != "false" or config["triggers"]["crons"] != []:
        raise ValueError("staging config must keep dispatch and cron disabled")
    if config["d1_databases"][0]["database_id"] == production_database_id:
        raise ValueError("staging D1 binding points at production")
    if config["r2_buckets"][0]["bucket_name"] == production_bucket_name:
        raise ValueError("staging R2 binding points at production")
    return text


def _diff_paths(left: Any, right: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(left, dict) and isinstance(right, dict):
        paths: set[tuple[str, ...]] = set()
        for key in set(left) | set(right):
            paths |= _diff_paths(left.get(key), right.get(key), (*prefix, str(key)))
        return paths
    if isinstance(left, list) and isinstance(right, list) and all(isinstance(i, dict) for i in left + right):
        paths = set()
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
            paths |= _diff_paths(left_item, right_item, (*prefix, str(index)))
        if len(left) != len(right):
            paths.add(prefix)
        return paths
    return set() if left == right else {prefix}


def _validate_only_release_switches_changed(base: dict[str, Any], rendered: dict[str, Any]) -> None:
    illegal = {path for path in _diff_paths(base, rendered) if path not in ALLOWED_PRODUCTION_SWITCHES}
    if illegal:
        raise ValueError(f"production profile changed non-release settings: {sorted(illegal)!r}")
    if base.get("d1_databases") != rendered.get("d1_databases") or base.get("r2_buckets") != rendered.get("r2_buckets"):
        raise ValueError("production profile must not mutate D1/R2 bindings")


def render_production_profile(
    profile: str,
    *,
    production_config: Path = PRODUCTION_CONFIG,
    enable_production_cron: bool = False,
    enable_production_v2: bool = False,
    confirm_production_v1_rollback: bool = False,
) -> str:
    if profile not in {"production-cron", "production-v2", "production-v1-rollback"}:
        raise ValueError("unsupported production profile")
    if not enable_production_cron:
        raise ValueError(f"{profile} requires --enable-production-cron")
    if profile == "production-v2" and not enable_production_v2:
        raise ValueError("production-v2 requires --enable-production-v2")
    if profile == "production-v1-rollback" and not confirm_production_v1_rollback:
        raise ValueError("production-v1-rollback requires --confirm-production-v1-rollback")
    if profile == "production-cron" and (enable_production_v2 or confirm_production_v1_rollback):
        raise ValueError("production-cron accepts only --enable-production-cron")

    base = _load_toml(production_config)
    rendered = copy.deepcopy(base)
    rendered.setdefault("triggers", {})["crons"] = list(PRODUCTION_CRONS)
    rendered.setdefault("vars", {})["GITHUB_DISPATCH_ENABLED"] = "true"
    rendered.setdefault("cache", {})["enabled"] = profile == "production-v2"
    rendered["vars"]["MARKET_SCAN_API_VERSION"] = "v2" if profile == "production-v2" else "v1"
    rendered["vars"]["EDGE_CACHE_ENABLED"] = "true" if profile == "production-v2" else "false"
    _validate_only_release_switches_changed(base, rendered)
    text = dumps_toml(rendered)
    _reject_sentinels(text)
    return text


def _ensure_output_path(output: Path, expected_name: str) -> Path:
    resolved = output.resolve()
    cloudflare = CLOUDFLARE_DIR.resolve()
    if resolved.parent != cloudflare or resolved.name != expected_name:
        raise ValueError(f"output must be cloudflare/{expected_name}")
    return resolved


def wrangler_version() -> tuple[int, int, int]:
    completed = subprocess.run(
        ["npx.cmd" if sys.platform.startswith("win") else "npx", "wrangler", "--version"],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout + completed.stderr)
    if completed.returncode != 0 or not match:
        raise RuntimeError("unable to determine wrangler version")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def validate_wrangler_minimum(minimum: tuple[int, int, int] = (4, 69, 0)) -> None:
    if wrangler_version() < minimum:
        raise RuntimeError(f"wrangler must be >= {'.'.join(map(str, minimum))}")


def write_config(output: Path, text: str) -> None:
    output.write_text(text, encoding="utf-8")
    tomllib.loads(text)
    _reject_sentinels(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render fail-closed full Wrangler release configs.")
    subparsers = parser.add_subparsers(dest="profile", required=True)

    staging = subparsers.add_parser("staging")
    staging.add_argument("--database-id", required=True)
    staging.add_argument("--bucket-name", required=True)
    staging.add_argument("--output", type=Path, required=True)

    for profile in ("production-cron", "production-v2", "production-v1-rollback"):
        release = subparsers.add_parser(profile)
        release.add_argument("--enable-production-cron", action="store_true")
        release.add_argument("--enable-production-v2", action="store_true")
        release.add_argument("--confirm-production-v1-rollback", action="store_true")
        release.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        validate_wrangler_minimum()
        if args.profile == "staging":
            output = _ensure_output_path(args.output, "wrangler.staging.generated.toml")
            text = render_staging_config(database_id=args.database_id, bucket_name=args.bucket_name)
        else:
            output = _ensure_output_path(args.output, f"wrangler.{args.profile}.generated.toml")
            text = render_production_profile(
                args.profile,
                enable_production_cron=args.enable_production_cron,
                enable_production_v2=args.enable_production_v2,
                confirm_production_v1_rollback=args.confirm_production_v1_rollback,
            )
        write_config(output, text)
    except (RuntimeError, ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
        print(f"Release config render failed: {exc}", file=sys.stderr)
        return 1
    print(f"Rendered {output.relative_to(ROOT_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
