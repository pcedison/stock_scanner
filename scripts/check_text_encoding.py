from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", ".worktrees", "__pycache__", "node_modules"}


@dataclass(frozen=True)
class EncodingProblem:
    path: str
    kind: str
    line: int
    detail: str


def _is_text_path(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def _iter_text_files(paths: Iterable[str | Path]) -> Iterable[Path]:
    for raw_path in paths:
        path = Path(raw_path)
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.is_file():
            if _is_text_path(path):
                yield path
            continue
        if not path.is_dir():
            continue
        for child in path.rglob("*"):
            if child.is_file() and _is_text_path(child) and not any(part in SKIP_PARTS for part in child.parts):
                yield child


def scan_paths(paths: Iterable[str | Path]) -> list[EncodingProblem]:
    problems: list[EncodingProblem] = []
    for path in sorted(set(_iter_text_files(paths))):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(EncodingProblem(str(path), "invalid-utf8", exc.start + 1, str(exc)))
            continue

        for line_number, line in enumerate(text.splitlines(), 1):
            if "\ufffd" in line:
                problems.append(EncodingProblem(str(path), "replacement-character", line_number, "contains U+FFFD"))
            if any(0x80 <= ord(character) <= 0x9F or 0xE000 <= ord(character) <= 0xF8FF for character in line):
                problems.append(
                    EncodingProblem(str(path), "suspicious-control", line_number, "contains C1 control/private-use character")
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check repository text files for UTF-8 and mojibake markers.")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["backend", "frontend", "scripts", "tests", "cloudflare", "pyproject.toml", "README.md"],
    )
    args = parser.parse_args(argv)

    problems = scan_paths(args.paths)
    for problem in problems:
        print(f"{problem.path}:{problem.line}: {problem.kind}: {problem.detail}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
