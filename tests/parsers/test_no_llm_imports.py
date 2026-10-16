"""Parsers must stay deterministic: no LLM or AWS SDK imports (parsers/CLAUDE.md)."""

from __future__ import annotations

import ast
from pathlib import Path

PARSERS_DIR = Path(__file__).resolve().parents[2] / "src" / "migrateiq" / "parsers"
FORBIDDEN = ("langchain", "anthropic", "boto3", "botocore", "migrateiq.agents")


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _is_forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f"{f}.") or module.startswith(f"{f}_") for f in FORBIDDEN)


def test_parsers_exist() -> None:
    assert list(PARSERS_DIR.rglob("*.py"))


def test_no_llm_imports_in_parsers() -> None:
    offenders = [
        f"{path.relative_to(PARSERS_DIR)}: {module}"
        for path in sorted(PARSERS_DIR.rglob("*.py"))
        for module in _imported_modules(path)
        if _is_forbidden(module)
    ]
    assert not offenders, offenders


def test_detector_catches_forbidden() -> None:
    assert _is_forbidden("langchain_aws")
    assert _is_forbidden("migrateiq.agents.llm")
    assert not _is_forbidden("migrateiq.ir.models")
