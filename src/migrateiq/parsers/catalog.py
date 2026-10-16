"""Table catalog built from a T-SQL DDL script.

Parsers use the catalog to fill ``Column.nullable``, ``Column.is_key`` and precision for tables an
artifact reads or writes, and to find IDENTITY (surrogate key) columns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from migrateiq.ir.models import Column, SourceTechnology
from migrateiq.parsers.base import map_type

logger = logging.getLogger(__name__)


def _key(schema: str, table: str) -> str:
    return f"{schema.lower()}.{table.lower()}"


def _type_params(data_type: exp.DataType) -> tuple[int | None, int | None]:
    """Return ``(precision, scale)`` from a sqlglot data type's parameters."""
    params: list[int] = []
    for param in data_type.expressions:
        text = param.sql(dialect="tsql")
        if not text.isdigit():
            # e.g. NVARCHAR(MAX): no fixed length.
            return None, None
        params.append(int(text))
    precision = params[0] if params else None
    scale = params[1] if len(params) > 1 else None
    return precision, scale


@dataclass
class Catalog:
    """Columns of every table declared in a DDL script, in declaration order.

    Attributes:
        tables: ``{"schema.table": [Column, ...]}`` with lower-case keys.
        identities: ``{"schema.table": [identity column names]}``.
    """

    tables: dict[str, list[Column]] = field(default_factory=dict)
    identities: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_ddl(cls, path: Path) -> Catalog:
        """Parse every ``CREATE TABLE`` statement in a T-SQL DDL file.

        Args:
            path: DDL script (T-SQL dialect).

        Returns:
            The catalog of declared tables.

        Raises:
            ValueError: If a ``CREATE TABLE`` cannot be read or uses an unmapped type.
        """
        catalog = cls()
        for statement in sqlglot.parse(path.read_text(encoding="utf-8"), read="tsql"):
            if (
                not isinstance(statement, exp.Create)
                or str(statement.args.get("kind", "")).upper() != "TABLE"
            ):
                continue
            schema_node = statement.this
            if not isinstance(schema_node, exp.Schema) or not isinstance(schema_node.this, exp.Table):
                raise ValueError(f"Unexpected CREATE TABLE shape in {path}: {statement.sql(dialect='tsql')}")
            table = schema_node.this
            key = _key(table.db or "dbo", table.name)
            columns: list[Column] = []
            identity: list[str] = []
            for col_def in schema_node.expressions:
                if not isinstance(col_def, exp.ColumnDef):
                    logger.warning("Skipping table-level element in %s: %s", key, col_def.sql(dialect="tsql"))
                    continue
                data_type = col_def.args.get("kind")
                if not isinstance(data_type, exp.DataType):
                    raise ValueError(f"Column {key}.{col_def.name} has no data type")
                type_name = data_type.this.name  # sqlglot DType name, e.g. "NVARCHAR", "DATETIME2"
                precision, scale = _type_params(data_type)
                nullable = True
                is_key = False
                for constraint in col_def.args.get("constraints") or []:
                    kind = constraint.args.get("kind")
                    if isinstance(kind, exp.NotNullColumnConstraint):
                        nullable = bool(kind.args.get("allow_null"))
                    elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        is_key = True
                        nullable = False
                    elif isinstance(
                        kind, (exp.AutoIncrementColumnConstraint, exp.GeneratedAsIdentityColumnConstraint)
                    ):
                        identity.append(col_def.name)
                columns.append(
                    Column(
                        name=col_def.name,
                        data_type=map_type(SourceTechnology.TSQL, type_name),
                        precision=precision,
                        scale=scale,
                        nullable=nullable,
                        is_key=is_key,
                    )
                )
            catalog.tables[key] = columns
            catalog.identities[key] = identity
        logger.debug("Catalog loaded %d tables from %s", len(catalog.tables), path)
        return catalog

    def table(self, schema: str, name: str) -> list[Column] | None:
        """Return the columns of ``schema.name`` (copies), or None if the table is unknown."""
        columns = self.tables.get(_key(schema, name))
        return None if columns is None else [c.model_copy() for c in columns]

    def column(self, schema: str, table: str, col: str) -> Column | None:
        """Return one column (a copy), matched case-insensitively, or None if unknown."""
        for column in self.tables.get(_key(schema, table), []):
            if column.name.lower() == col.lower():
                return column.model_copy()
        return None

    def identity_columns(self, schema: str, table: str) -> list[str]:
        """Return the IDENTITY columns of a table (empty if none or the table is unknown)."""
        return list(self.identities.get(_key(schema, table), []))
