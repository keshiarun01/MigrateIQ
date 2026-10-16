"""Tests for the shared parser base."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from migrateiq.ir.models import DataType, SourceTechnology
from migrateiq.parsers.base import UnknownLegacyType, map_type, run_ts_note


def _fixture_types(artifacts_dir: Path) -> dict[SourceTechnology, set[str]]:
    ssis = set()
    for f in (artifacts_dir / "ssis").glob("*.dtsx"):
        ssis |= set(re.findall(r'dataType="([^"]+)"', f.read_text(encoding="utf-8")))
    infa = set(
        re.findall(
            r'DATATYPE="([^"]+)"',
            (artifacts_dir / "informatica" / "m_LOAD_FACT_CLAIMS.xml").read_text(encoding="utf-8"),
        )
    )
    ddl = (artifacts_dir / "schema" / "insurance_ddl.sql").read_text(encoding="utf-8")
    tsql = {t.lower() for t in re.findall(r"^[ \t]+\w+[ \t]+([A-Z][A-Z0-9]*)\b", ddl, flags=re.MULTILINE)}
    return {SourceTechnology.SSIS: ssis, SourceTechnology.INFORMATICA: infa, SourceTechnology.TSQL: tsql}


def test_every_fixture_type_maps(artifacts_dir: Path) -> None:
    types = _fixture_types(artifacts_dir)
    assert all(types.values()), types
    for technology, names in types.items():
        for name in names:
            assert isinstance(map_type(technology, name), DataType), (technology, name)


@pytest.mark.parametrize(
    ("technology", "name", "expected"),
    [
        (SourceTechnology.SSIS, "wstr", DataType.STRING),
        (SourceTechnology.SSIS, "dbTimeStamp2", DataType.TIMESTAMP),
        (SourceTechnology.TSQL, "NVARCHAR", DataType.STRING),
        (SourceTechnology.TSQL, "bit", DataType.BOOLEAN),
        (SourceTechnology.INFORMATICA, "date/time", DataType.TIMESTAMP),
        (SourceTechnology.INFORMATICA, "integer", DataType.INT),
    ],
)
def test_known_types(technology: SourceTechnology, name: str, expected: DataType) -> None:
    assert map_type(technology, name) == expected


def test_unknown_type_raises() -> None:
    with pytest.raises(UnknownLegacyType, match="geography"):
        map_type(SourceTechnology.TSQL, "geography")


def test_run_ts_note_mentions_original_and_run_ts() -> None:
    note = run_ts_note("GETDATE()")
    assert "GETDATE()" in note
    assert "run_ts" in note
