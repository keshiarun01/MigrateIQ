"""Deterministic rules for T-SQL scalar expressions and conditions.

The text is parsed with sqlglot (dialect ``tsql``). An expression is translated to Spark SQL only when every
node is on a small allow-list: columns, numeric literals, the comparisons ``= <> != < > <= >=``, ``AND``,
``OR``, ``NOT``, ``IS [NOT] NULL``, parentheses, and ``UPPER``/``LTRIM``/``RTRIM``/``TRIM``. Everything else
stays ``pending`` with ``spark_sql=None`` for the translator agent.

Legacy semantics that are never "fixed" here:

* ``<>`` stays ``<>`` (NULL-unsafe, T20). ``!=`` is emitted as ``<>``. ``<=>`` is never produced.
* X2: SQL Server's default collation compares strings case-insensitively; Spark does not. Any comparison
  with a string operand stays ``pending`` with :data:`COLLATION_NOTE`.
* Runtime values (``SYSDATETIME()``, ``GETDATE()``, ``@RunTs``) resolve to the pinned ``run_ts`` argument.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping

import sqlglot
from sqlglot import exp

from migrateiq.ir.models import DataType, Expression, ExpressionDialect, TranslationStatus
from migrateiq.parsers.base import run_ts_note

logger = logging.getLogger(__name__)

PARSE_FAILURE_NOTE = "Could not parse T-SQL expression; left for the translator."
COLLATION_NOTE = (
    "SQL Server default collation is case-insensitive; Spark string comparison is case-sensitive (X2). "
    "Translator must emulate the legacy collation."
)
RUNTIME_NOTE = "Non-deterministic run time; codegen pins it to the run_ts job argument."
WATERMARK_NOTE = "Watermark variable {var}; resolve to the watermark job argument."
UNKNOWN_TYPE_NOTE = (
    "Type of column(s) {columns} is unknown; cannot tell whether the comparison is a string comparison (X2)."
)

#: Variables that hold the run time by default (case-insensitive match).
DEFAULT_RUNTIME_VARIABLES: frozenset[str] = frozenset({"@RunTs"})

#: Resolver signature: ``(qualifier or None, column name) -> DataType or None``.
ColumnTypeResolver = Callable[[str | None, str], DataType | None]

_RUNTIME_FUNCTION_TEXT = re.compile(r"\b(SYSDATETIME|GETDATE)\s*\(\s*\)", re.IGNORECASE)
_COMPARISONS: tuple[type[exp.Expression], ...] = (exp.EQ, exp.NEQ, exp.LT, exp.GT, exp.LTE, exp.GTE)
_STRING_COMPARISONS: tuple[type[exp.Expression], ...] = (*_COMPARISONS, exp.In, exp.Like, exp.Between)
_ALLOWED: tuple[type[exp.Expression], ...] = (
    exp.Column,
    exp.Identifier,
    *_COMPARISONS,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Is,
    exp.Null,
    exp.Paren,
    exp.Upper,
    exp.Trim,
)


def _make_resolver(
    column_types: Mapping[str, DataType] | ColumnTypeResolver | None,
) -> ColumnTypeResolver:
    """Wrap a mapping (``"alias.col"`` or ``"col"`` keys, case-insensitive) or a callable as a resolver."""
    if column_types is None:
        return lambda qualifier, name: None
    if callable(column_types) and not isinstance(column_types, Mapping):
        return column_types
    lowered = {k.lower(): v for k, v in column_types.items()}

    def resolve(qualifier: str | None, name: str) -> DataType | None:
        if qualifier:
            found = lowered.get(f"{qualifier.lower()}.{name.lower()}")
            if found is not None:
                return found
        return lowered.get(name.lower())

    return resolve


def variable_name(node: exp.Expression | None) -> str | None:
    """Return ``@name`` for a sqlglot T-SQL variable node, ``@@name`` for a system variable, else None.

    Args:
        node: Any sqlglot node.

    Returns:
        The variable spelling (case as written) or None if ``node`` is not a variable.
    """
    if not isinstance(node, exp.Parameter):
        return None
    inner = node.this
    if isinstance(inner, exp.Parameter):
        name = variable_name(inner)
        return None if name is None else f"@{name}"
    return f"@{inner.name}" if isinstance(inner, exp.Expression) else None


def _in_set(name: str | None, names: Iterable[str]) -> bool:
    return name is not None and name.lower() in {n.lower() for n in names}


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_string_literal(node: exp.Expression) -> bool:
    return (isinstance(node, exp.Literal) and node.is_string) or isinstance(node, exp.National)


def _operands(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.In):
        return [node.this, *node.expressions]
    if isinstance(node, exp.Between):
        return [node.this, node.args["low"], node.args["high"]]
    return [node.this, node.expression]


def text_has_runtime_value(text: str, runtime_vars: Iterable[str] = DEFAULT_RUNTIME_VARIABLES) -> bool:
    """True if the text mentions ``SYSDATETIME()``, ``GETDATE()`` or a runtime variable (text scan).

    Args:
        text: T-SQL text.
        runtime_vars: Variable spellings (``@RunTs``) that hold the run time.

    Returns:
        Whether a runtime value appears (string literals are not excluded; used only to add notes).
    """
    if _RUNTIME_FUNCTION_TEXT.search(text):
        return True
    return any(re.search(rf"{re.escape(v)}\b", text, re.IGNORECASE) for v in runtime_vars)


def parse_condition(sql_text: str) -> exp.Expression | None:
    """Parse one T-SQL scalar expression or condition with sqlglot, or return None if it does not parse."""
    try:
        return sqlglot.condition(sql_text, dialect="tsql")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError) as err:
        logger.debug("T-SQL expression parse failed (%s): %r", err, sql_text)
        return None


def translate(
    sql_text: str,
    column_types: Mapping[str, DataType] | ColumnTypeResolver | None = None,
    runtime_vars: Iterable[str] = DEFAULT_RUNTIME_VARIABLES,
    watermark_vars: Iterable[str] = (),
) -> Expression:
    """Build an ``Expression`` (dialect ``tsql``) for a T-SQL expression or condition.

    Args:
        sql_text: Verbatim T-SQL text; copied unchanged into ``Expression.original``.
        column_types: ``{"alias.col" | "col": DataType}`` or a resolver ``(qualifier, name) -> DataType``.
            Used to detect string comparisons (X2).
        runtime_vars: Variables that hold the run time (e.g. ``@RunTs``), matched case-insensitively.
        watermark_vars: Variables that hold the incremental watermark (e.g. ``@LastValue``).

    Returns:
        A ``deterministic`` expression when every node is on the allow-list, otherwise ``pending`` with
        ``spark_sql=None`` and notes explaining why (runtime value, watermark variable, X2 collation).
    """
    runtime_vars = tuple(runtime_vars)
    watermark_vars = tuple(watermark_vars)
    root = parse_condition(sql_text)
    if root is None:
        notes = PARSE_FAILURE_NOTE
        if text_has_runtime_value(sql_text, runtime_vars):
            notes = f"{notes} {run_ts_note(sql_text)}"
        return Expression(
            original=sql_text,
            dialect=ExpressionDialect.TSQL,
            translation_status=TranslationStatus.PENDING,
            notes=notes,
        )

    resolve = _make_resolver(column_types)
    nodes = list(root.walk())
    columns = sorted({c.name for c in nodes if isinstance(c, exp.Column) and c.name})

    bare = _unwrap(root)
    if isinstance(bare, exp.CurrentTimestamp) or _in_set(variable_name(bare), runtime_vars):
        return Expression(
            original=sql_text,
            dialect=ExpressionDialect.TSQL,
            spark_sql="current_timestamp()",
            translation_status=TranslationStatus.DETERMINISTIC,
            referenced_columns=[],
            notes=RUNTIME_NOTE,
        )

    notes: list[str] = []
    has_runtime = any(
        isinstance(n, exp.CurrentTimestamp) or _in_set(variable_name(n), runtime_vars) for n in nodes
    )
    if has_runtime:
        notes.append(run_ts_note(sql_text))
    watermarks: list[str] = []
    for node in nodes:
        name = variable_name(node)
        if _in_set(name, watermark_vars) and name not in watermarks:
            watermarks.append(name or "")
    notes.extend(WATERMARK_NOTE.format(var=w) for w in watermarks)

    string_compare = False
    unknown: list[str] = []
    for comparison in (n for n in nodes if isinstance(n, _STRING_COMPARISONS)):
        for operand in _operands(comparison):
            for leaf in _unwrap(operand).walk():
                if _is_string_literal(leaf):
                    string_compare = True
                elif isinstance(leaf, exp.Column):
                    data_type = resolve(leaf.table or None, leaf.name)
                    if data_type == DataType.STRING:
                        string_compare = True
                    elif data_type is None:
                        label = f"{leaf.table}.{leaf.name}" if leaf.table else leaf.name
                        if label not in unknown:
                            unknown.append(label)
    if string_compare:
        notes.append(COLLATION_NOTE)
    if unknown:
        notes.append(UNKNOWN_TYPE_NOTE.format(columns=", ".join(unknown)))

    covered = all(
        isinstance(n, _ALLOWED) or (isinstance(n, exp.Literal) and not n.is_string) for n in nodes
    ) and all(isinstance(n.args.get("expression"), exp.Null) for n in nodes if isinstance(n, exp.Is))
    if notes or not covered:
        return Expression(
            original=sql_text,
            dialect=ExpressionDialect.TSQL,
            translation_status=TranslationStatus.PENDING,
            referenced_columns=columns,
            notes=" ".join(notes) or None,
        )

    tree = root.copy()
    for column in tree.find_all(exp.Column):
        column.set("table", None)
        column.set("db", None)
        column.set("catalog", None)
    spark_sql = tree.sql(dialect="spark")
    if "<=>" in spark_sql or "!=" in spark_sql:
        # The allow-list never yields these; refuse rather than emit a changed comparison (T20).
        logger.warning("Unexpected comparison operator in Spark SQL %r; left pending", spark_sql)
        return Expression(
            original=sql_text,
            dialect=ExpressionDialect.TSQL,
            translation_status=TranslationStatus.PENDING,
            referenced_columns=columns,
        )
    return Expression(
        original=sql_text,
        dialect=ExpressionDialect.TSQL,
        spark_sql=spark_sql,
        translation_status=TranslationStatus.DETERMINISTIC,
        referenced_columns=columns,
    )
