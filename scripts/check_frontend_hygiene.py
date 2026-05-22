from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT_DIR / "frontend" / "app.js"
DEFAULT_DOM = ROOT_DIR / "frontend" / "dom.js"
DEFAULT_AUTH = ROOT_DIR / "frontend" / "auth.js"
SAFE_HTML_HELPER_PATTERN = re.compile(r"\b(?:escapeHtml|emptyStateHtml|setSafeHtml|render[A-Z][A-Za-z0-9_]*)\s*\(")
INNER_HTML_ASSIGNMENT_PATTERN = re.compile(r"\.innerHTML\s*=(?!=)")
RAW_TEMPLATE_PATH_PATTERN = re.compile(
    r"\$\{\s*[A-Za-z_$][\w$]*(?:(?:\?|\.)?\.[A-Za-z_$][\w$]*|\[[^\]]+\])+\s*\}"
)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def html_assignment_statement(lines: list[str], start_index: int) -> str:
    statement_lines = []
    captures_map_chain = ".map(" in lines[start_index]
    for line in lines[start_index:]:
        statement_lines.append(line)
        if ".map(" in line:
            captures_map_chain = True
        if captures_map_chain:
            if ".join(" in line and line.rstrip().endswith(";"):
                break
            if len(statement_lines) >= 120:
                break
            continue
        if line.rstrip().endswith(";"):
            break
    return "\n".join(statement_lines)


def is_static_html_assignment(statement: str) -> bool:
    rhs = statement.split("=", 1)[1].strip().rstrip(";")
    if rhs in {'""', "''", "``"}:
        return True
    if rhs.startswith(("`", '"', "'")) and "${" not in rhs:
        return True
    return False


def raw_template_path_interpolations(statement: str) -> list[str]:
    return [match.group(0) for match in RAW_TEMPLATE_PATH_PATTERN.finditer(statement)]


def dangerous_inner_html_assignments(source_text: str) -> list[dict[str, object]]:
    lines = source_text.splitlines()
    findings = []
    for index, line in enumerate(lines):
        if not INNER_HTML_ASSIGNMENT_PATTERN.search(line):
            continue
        statement = html_assignment_statement(lines, index)
        raw_interpolations = raw_template_path_interpolations(statement)
        if (SAFE_HTML_HELPER_PATTERN.search(statement) or is_static_html_assignment(statement)) and not raw_interpolations:
            continue
        finding = {"line": index + 1, "statement": statement.strip()}
        if raw_interpolations:
            finding["rawTemplateInterpolations"] = raw_interpolations
        findings.append(finding)
    return findings


def frontend_hygiene_report(
    app_path: Path = DEFAULT_APP,
    dom_path: Path = DEFAULT_DOM,
    auth_path: Path = DEFAULT_AUTH,
) -> dict[str, object]:
    app_text = app_path.read_text(encoding="utf-8")
    dom_text = dom_path.read_text(encoding="utf-8")
    auth_text = auth_path.read_text(encoding="utf-8") if auth_path.exists() else ""
    return {
        "appPath": display_path(app_path),
        "domPath": display_path(dom_path),
        "authPath": display_path(auth_path),
        "appLines": len(app_text.splitlines()),
        "innerHTMLAssignments": app_text.count("innerHTML"),
        "dangerousInnerHTMLAssignments": dangerous_inner_html_assignments(app_text),
        "insertAdjacentHTMLCalls": app_text.count("insertAdjacentHTML"),
        "hasSharedEscapeHelper": "function escapeHtml" in dom_text and "StockScannerDom" in dom_text,
        "hasSharedAuthHelper": "StockScannerAuth" in auth_text and "normalizeAuthUser" in auth_text,
        "emptyStateUsesSharedHelper": "emptyStateHtml(" in app_text,
    }


def validate_frontend_hygiene(report: dict[str, object], max_app_lines: int, max_inner_html: int) -> list[str]:
    problems: list[str] = []
    if int(report["appLines"]) > max_app_lines:
        problems.append(f"frontend/app.js has {report['appLines']} lines; budget is {max_app_lines}")
    if int(report["innerHTMLAssignments"]) > max_inner_html:
        problems.append(
            f"frontend/app.js has {report['innerHTMLAssignments']} innerHTML references; budget is {max_inner_html}"
        )
    dangerous_assignments = report.get("dangerousInnerHTMLAssignments", [])
    if dangerous_assignments:
        lines = ", ".join(str(item["line"]) for item in dangerous_assignments)
        problems.append(
            "frontend/app.js has dangerous innerHTML assignments without escapeHtml/render helper "
            f"or whitelist coverage on lines: {lines}"
        )
    if int(report["insertAdjacentHTMLCalls"]) > 0:
        problems.append("insertAdjacentHTML is not allowed in frontend/app.js")
    if not report["hasSharedEscapeHelper"]:
        problems.append("frontend/dom.js must own the shared escapeHtml helper")
    if not report["hasSharedAuthHelper"]:
        problems.append("frontend/auth.js must own shared auth identity helpers")
    if not report["emptyStateUsesSharedHelper"]:
        problems.append("frontend/app.js should use emptyStateHtml for empty/loading placeholders")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check frontend single-file and HTML-rendering hygiene budgets.")
    parser.add_argument("--max-app-lines", type=int, default=2668)
    parser.add_argument("--max-inner-html", type=int, default=19)
    args = parser.parse_args(argv)

    report = frontend_hygiene_report()
    problems = validate_frontend_hygiene(report, args.max_app_lines, args.max_inner_html)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if problems:
        print("Frontend hygiene check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
