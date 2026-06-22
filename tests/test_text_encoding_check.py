from scripts.check_text_encoding import scan_paths


def test_text_encoding_scan_flags_invalid_utf8_and_mojibake_markers(tmp_path):
    (tmp_path / "good.py").write_text('LABEL = "台積電"\n', encoding="utf-8")
    (tmp_path / "replacement.js").write_text(f'const label = "壞{chr(0xFFFD)}字";\n', encoding="utf-8")
    (tmp_path / "private_use.md").write_text("bad \ue000 marker\n", encoding="utf-8")
    (tmp_path / "invalid.txt").write_bytes(b"\xff\xfe")

    problems = scan_paths([tmp_path])
    kinds = {problem.kind for problem in problems}

    assert "replacement-character" in kinds
    assert "suspicious-control" in kinds
    assert "invalid-utf8" in kinds


def test_repository_text_files_are_utf8_without_mojibake_markers():
    problems = scan_paths(["backend", "frontend", "scripts", "tests", "cloudflare", "pyproject.toml", "README.md"])

    assert problems == []
