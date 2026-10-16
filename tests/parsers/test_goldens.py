"""Parser goldens: each approved file in tests/golden/parser/ must match a fresh parse (via the registry).

Goldens are read-only and written only after user approval (CLAUDE.md). ``sha256`` and ``parsed_at`` are
ignored by the golden helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.registry import parse_artifact
from tests.helpers.golden import assert_matches_golden

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "parser"
GOLDENS = sorted(GOLDEN_DIR.glob("*.ir.json"))


def test_goldens_exist() -> None:
    assert GOLDENS, f"no parser goldens in {GOLDEN_DIR}"


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.name.removesuffix(".ir.json") for g in GOLDENS])
def test_parser_matches_golden(golden: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    doc = json.loads(golden.read_text(encoding="utf-8"))
    assert golden.name == f"{doc['unit_id']}.ir.json"
    catalog = Catalog.from_ddl(REPO_ROOT / "sample_artifacts" / "schema" / "insurance_ddl.sql")
    unit = parse_artifact(Path(doc["source_artifact"]["file_path"]), catalog)
    assert_matches_golden(unit, golden)
