"""Shared parser contract, hashing, and legacy-type maps.

Parsers are deterministic: no LLM, no network, no randomness, no clock reads except
``SourceArtifact.parsed_at``.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Protocol

from migrateiq.ir.models import DataType, MigrationUnit, SourceTechnology

logger = logging.getLogger(__name__)

PARSER_VERSION = "0.1.0"

RUN_TS_NOTE = (
    "Runtime value {original!r}: resolve to the pinned run_ts job argument (MIGRATEIQ_RUN_TS). "
    "Left pending; codegen injects run_ts."
)


#: Logical system per schema, from the header of sample_artifacts/schema/insurance_ddl.sql
#: (source OLTP "PolicyAdmin" = dbo; EDW "InsuranceDW" = edw, etl, audit). Used when an artifact carries no
#: connection metadata for a table.
SCHEMA_SYSTEMS: dict[str, str] = {
    "dbo": "PolicyAdmin",
    "edw": "InsuranceDW",
    "etl": "InsuranceDW",
    "audit": "InsuranceDW",
}
SYSTEM_RULE = (
    "System inferred from schema (dbo -> PolicyAdmin; edw/etl/audit -> InsuranceDW) per insurance_ddl.sql."
)


class UnknownLegacyType(ValueError):
    """Raised when a legacy data type has no entry in the type map."""


class Parser(Protocol):
    """Interface every technology parser implements."""

    technology: SourceTechnology

    def parse(self, path: Path) -> MigrationUnit:
        """Parse one legacy artifact into a ``MigrationUnit``."""
        ...


_TYPE_MAPS: dict[SourceTechnology, dict[str, DataType]] = {
    SourceTechnology.TSQL: {
        "int": DataType.INT,
        "bigint": DataType.BIGINT,
        "bit": DataType.BOOLEAN,
        "char": DataType.STRING,
        "varchar": DataType.STRING,
        "nchar": DataType.STRING,
        "nvarchar": DataType.STRING,
        "decimal": DataType.DECIMAL,
        "numeric": DataType.DECIMAL,
        "date": DataType.DATE,
        "datetime": DataType.TIMESTAMP,
        "datetime2": DataType.TIMESTAMP,
    },
    SourceTechnology.SSIS: {
        "i4": DataType.INT,
        "i8": DataType.BIGINT,
        "bool": DataType.BOOLEAN,
        "str": DataType.STRING,
        "wstr": DataType.STRING,
        "numeric": DataType.DECIMAL,
        "dbdate": DataType.DATE,
        "dbtimestamp": DataType.TIMESTAMP,
        "dbtimestamp2": DataType.TIMESTAMP,
    },
    SourceTechnology.INFORMATICA: {
        "integer": DataType.INT,
        "int": DataType.INT,
        "bigint": DataType.BIGINT,
        "string": DataType.STRING,
        "varchar": DataType.STRING,
        "char": DataType.STRING,
        "decimal": DataType.DECIMAL,
        "date": DataType.DATE,
        "date/time": DataType.TIMESTAMP,
        "datetime2": DataType.TIMESTAMP,
        "bit": DataType.BOOLEAN,
    },
}


def map_type(technology: SourceTechnology, legacy_type: str) -> DataType:
    """Map a legacy type name (without length/precision) to an IR ``DataType``.

    Args:
        technology: Technology whose type vocabulary ``legacy_type`` belongs to.
        legacy_type: Type name as written in the artifact, e.g. ``"wstr"`` or ``"NVARCHAR"``.

    Returns:
        The matching ``DataType``.

    Raises:
        UnknownLegacyType: If the type is not in the map. Callers record an ``UnsupportedConstruct``.
    """
    try:
        return _TYPE_MAPS[technology][legacy_type.strip().lower()]
    except KeyError:
        raise UnknownLegacyType(f"{technology.value}: unknown legacy type {legacy_type!r}") from None


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_ts_note(original: str) -> str:
    """Return the standard note for an expression that uses a runtime value (T3)."""
    return RUN_TS_NOTE.format(original=original)
