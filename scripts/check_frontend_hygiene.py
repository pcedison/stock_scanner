from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT_DIR / "frontend" / "app.js"
DEFAULT_DOM = ROOT_DIR / "frontend" / "dom.js"


def frontend_hygiene_report(app_path: Path = DEFAULT_APP, dom_path: Path = DEFAULT_DOM) -> dict[str, object]:
    app_text = app_path.read_text(encoding="utf-8")
    dom_text = dom_path.read_text(encoding="utf-8")
    return {
        "appPath": str(app_path.relative_to(ROOT_DIR)),
        "domPath": str(dom_path.relative_to(ROOT_DIR)),
        "appLines": len(app_text.splitlines()),
        "innerHTMLAssignments": app_text.count("innerHTML"),
        "insertAdjacentHTMLCalls": app_text.count("insertAdjacentHTML"),
        "hasSharedEscapeHelper": "function escapeHtml" in dom_text and "StockScannerDom" in dom_text,
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
    if int(report["insertAdjacentHTMLCalls"]) > 0:
        problems.append("insertAdjacentHTML is not allowed in frontend/app.js")
    if not report["hasSharedEscapeHelper"]:
        problems.append("frontend/dom.js must own the shared escapeHtml helper")
    if not report["emptyStateUsesSharedHelper"]:
        problems.append("frontend/app.js should use emptyStateHtml for empty/loading placeholders")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check frontend single-file and HTML-rendering hygiene budgets.")
    parser.add_argument("--max-app-lines", type=int, default=2700)
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
