"""Tests for the DDL catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from migrateiq.ir.models import DataType
from migrateiq.parsers.catalog import Catalog


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.from_ddl(Path(__file__).resolve().parents[2] / "sample_artifacts/schema/insurance_ddl.sql")


def test_identity_and_primary_key(catalog: Catalog) -> None:
    col = catalog.column("edw", "dim_policy", "policy_key")
    assert col is not None
    assert col.is_key and not col.nullable and col.data_type == DataType.INT
    assert catalog.identity_columns("edw", "dim_policy") == ["policy_key"]
    assert catalog.identity_columns("edw", "dim_policyholder") == []


def test_string_precision_not_null(catalog: Catalog) -> None:
    col = catalog.column("edw", "dim_policyholder", "full_name")
    assert col is not None
    assert (col.data_type, col.precision, col.scale, col.nullable) == (DataType.STRING, 101, None, False)


def test_decimal_precision_and_scale(catalog: Catalog) -> None:
    col = catalog.column("dbo", "policy", "annual_premium")
    assert col is not None
    assert (col.data_type, col.precision, col.scale) == (DataType.DECIMAL, 12, 2)


def test_explicit_null_is_nullable(catalog: Catalog) -> None:
    col = catalog.column("dbo", "claim", "policy_id")
    assert col is not None
    assert col.nullable and not col.is_key


def test_declaration_order_and_defaults_references(catalog: Catalog) -> None:
    claim = catalog.table("dbo", "claim")
    assert claim is not None
    assert [c.name for c in claim][:3] == ["claim_id", "claim_number", "policy_id"]
    is_deleted = catalog.column("dbo", "claim", "is_deleted")  # NOT NULL DEFAULT 0
    assert is_deleted is not None and is_deleted.data_type == DataType.BOOLEAN and not is_deleted.nullable
    fk = catalog.column("dbo", "policy", "policyholder_id")  # REFERENCES ...
    assert fk is not None and not fk.nullable and not fk.is_key
    created = catalog.column("dbo", "policyholder", "created_at")
    assert created is not None and created.data_type == DataType.TIMESTAMP and created.precision is None


def test_unknown_table_and_column(catalog: Catalog) -> None:
    assert catalog.table("edw", "nope") is None
    assert catalog.column("edw", "dim_policy", "nope") is None


def test_small_ddl(tmp_path: Path) -> None:
    ddl = tmp_path / "x.sql"
    ddl.write_text(
        "CREATE TABLE s.t (\n  id INT IDENTITY(1,1) NOT NULL PRIMARY KEY, -- key\n"
        "  note NVARCHAR(MAX) NULL\n);\n",
        encoding="utf-8",
    )
    cat = Catalog.from_ddl(ddl)
    assert cat.identity_columns("s", "t") == ["id"]
    note = cat.column("s", "t", "note")
    assert note is not None and note.precision is None and note.nullable
