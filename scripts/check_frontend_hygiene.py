from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import cast

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT_DIR / "frontend" / "app.js"
DEFAULT_DOM = ROOT_DIR / "frontend" / "dom.js"
DEFAULT_AUTH = ROOT_DIR / "frontend" / "auth.js"
DEFAULT_REFERENCE = ROOT_DIR / "frontend" / "reference_data.js"
DEFAULT_STRATEGY = ROOT_DIR / "frontend" / "strategy_content.js"
DEFAULT_STORAGE = ROOT_DIR / "frontend" / "storage.js"
DEFAULT_RENDERERS = ROOT_DIR / "frontend" / "renderers.js"
SAFE_HTML_HELPER_PATTERN = re.compile(r"\b(?:escapeHtml|emptyStateHtml|setSafeHtml|render[A-Z][A-Za-z0-9_]*)\s*\(")
INNER_HTML_ASSIGNMENT_PATTERN = re.compile(r"\.innerHTML\s*=(?!=)")
HTML_SINK_PATTERN = re.compile(r"(?:\.innerHTML\s*=(?!=)|\bsetSafeHtml\s*\()")
SAFE_HTML_CALL_PATTERN = re.compile(r"\bsetSafeHtml\s*\(")
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
            stripped = line.rstrip()
            # Terminate at the close of a `.map(...).join(...)` chain, or at the
            # close of the surrounding sink call/template literal (a line ending
            # in "`);"). Without the latter, templates whose join closes an
            # interpolation (`.join("")}`) never match the `.join(...);` form, so
            # the statement runs away up to the 120-line cap and sweeps in
            # unrelated downstream code.
            if (".join(" in line and stripped.endswith(";")) or stripped.endswith(");"):
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
    return rhs.startswith(("`", '"', "'")) and "${" not in rhs


def raw_template_path_interpolations(statement: str) -> list[str]:
    return [match.group(0) for match in RAW_TEMPLATE_PATH_PATTERN.finditer(statement)]


def dangerous_inner_html_assignments(source_text: str) -> list[dict[str, object]]:
    lines = source_text.splitlines()
    findings = []
    for index, line in enumerate(lines):
        if not HTML_SINK_PATTERN.search(line):
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
    reference_path: Path = DEFAULT_REFERENCE,
    strategy_path: Path = DEFAULT_STRATEGY,
    storage_path: Path = DEFAULT_STORAGE,
    renderers_path: Path = DEFAULT_RENDERERS,
) -> dict[str, object]:
    app_text = app_path.read_text(encoding="utf-8")
    dom_text = dom_path.read_text(encoding="utf-8")
    auth_text = auth_path.read_text(encoding="utf-8") if auth_path.exists() else ""
    reference_text = reference_path.read_text(encoding="utf-8") if reference_path.exists() else ""
    strategy_text = strategy_path.read_text(encoding="utf-8") if strategy_path.exists() else ""
    storage_text = storage_path.read_text(encoding="utf-8") if storage_path.exists() else ""
    renderers_text = renderers_path.read_text(encoding="utf-8") if renderers_path.exists() else ""
    return {
        "appPath": display_path(app_path),
        "domPath": display_path(dom_path),
        "authPath": display_path(auth_path),
        "referencePath": display_path(reference_path),
        "strategyPath": display_path(strategy_path),
        "storagePath": display_path(storage_path),
        "renderersPath": display_path(renderers_path),
        "appLines": len(app_text.splitlines()),
        "innerHTMLAssignments": len(INNER_HTML_ASSIGNMENT_PATTERN.findall(app_text)),
        "safeHtmlCalls": len(SAFE_HTML_CALL_PATTERN.findall(app_text)),
        "dangerousInnerHTMLAssignments": dangerous_inner_html_assignments(app_text),
        "insertAdjacentHTMLCalls": app_text.count("insertAdjacentHTML"),
        "hasSharedEscapeHelper": "function escapeHtml" in dom_text and "StockScannerDom" in dom_text,
        "hasSharedAuthHelper": "StockScannerAuth" in auth_text and "normalizeAuthUser" in auth_text,
        "hasSplitReferenceData": "StockScannerReferenceData" in reference_text and "DEFAULT_COMPANIES" in reference_text,
        "hasSplitStrategyContent": "StockScannerStrategyContent" in strategy_text and "STRATEGY_STATUS_DETAILS" in strategy_text,
        "hasSplitStorageHelper": "StockScannerStorage" in storage_text and "loadHoldingsFromStorage" in storage_text,
        "hasSplitRendererFactory": "StockScannerRenderers" in renderers_text and "createRenderers" in renderers_text,
        "appOwnsStrategyContent": "const STRATEGY_STATUS_DETAILS = [" in app_text,
        "emptyStateUsesSharedHelper": "emptyStateHtml(" in app_text,
    }


def validate_frontend_hygiene(report: dict[str, object], max_app_lines: int, max_inner_html: int) -> list[str]:
    problems: list[str] = []
    if cast(int, report["appLines"]) > max_app_lines:
        problems.append(f"frontend/app.js has {report['appLines']} lines; budget is {max_app_lines}")
    if cast(int, report["innerHTMLAssignments"]) > max_inner_html:
        problems.append(
            f"frontend/app.js has {report['innerHTMLAssignments']} innerHTML references; budget is {max_inner_html}"
        )
    dangerous_assignments = report.get("dangerousInnerHTMLAssignments", [])
    if dangerous_assignments:
        lines = ", ".join(str(item["line"]) for item in cast(list, dangerous_assignments))
        problems.append(
            "frontend/app.js has dangerous HTML sinks without escapeHtml/render helper "
            f"or whitelist coverage on lines: {lines}"
        )
    if cast(int, report["insertAdjacentHTMLCalls"]) > 0:
        problems.append("insertAdjacentHTML is not allowed in frontend/app.js")
    if not report["hasSharedEscapeHelper"]:
        problems.append("frontend/dom.js must own the shared escapeHtml helper")
    if not report["hasSharedAuthHelper"]:
        problems.append("frontend/auth.js must own shared auth identity helpers")
    if not report.get("hasSplitReferenceData", True):
        problems.append("frontend/reference_data.js must own fallback reference data")
    if not report.get("hasSplitStrategyContent", True):
        problems.append("frontend/strategy_content.js must own strategy explanatory content")
    if not report.get("hasSplitStorageHelper", True):
        problems.append("frontend/storage.js must own local persistence helpers")
    if not report.get("hasSplitRendererFactory", True):
        problems.append("frontend/renderers.js must own pure HTML renderers")
    if report.get("appOwnsStrategyContent", False):
        problems.append("frontend/app.js must not own the large strategy content table")
    if not report["emptyStateUsesSharedHelper"]:
        problems.append("frontend/app.js should use emptyStateHtml for empty/loading placeholders")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check frontend single-file and HTML-rendering hygiene budgets.")
    parser.add_argument("--max-app-lines", type=int, default=2400)
    parser.add_argument("--max-inner-html", type=int, default=0)
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
