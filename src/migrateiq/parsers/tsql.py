"""Deterministic T-SQL parser: one stored procedure or trigger -> one ``MigrationUnit``.

``sqlglot.parse`` cannot read procedural T-SQL as a whole (``BEGIN TRY``/``BEGIN CATCH`` and trigger headers
fall back to ``exp.Command``, and ``IF NOT UPDATE(col)`` is mangled). So this module:

1. tokenizes the file itself (strings, bracketed names and comments respected, exact source offsets kept),
2. strips the ``CREATE [OR ALTER] PROCEDURE|TRIGGER`` header (name, parameters, trigger table and events),
3. splits the body into a block tree (``BEGIN``/``END``, ``TRY``/``CATCH``, ``IF``/``WHILE``, leaves),
4. hands each leaf statement to ``sqlglot.parse_one(read="tsql")`` and recognises a small set of shapes.

Every statement or block that is not recognised becomes an ``UnsupportedConstruct`` with its exact source
text. Only ``SET NOCOUNT ON``, plain ``DECLARE @v TYPE [= literal]``, ``DROP TABLE IF EXISTS #temp`` and a
bare ``RETURN`` are ignored without IR (each logged at DEBUG).

Recognised procedure shapes (see ``usp_Load_FactPremium.sql``): run_ts defaulting
(``SET @p = ISNULL(@p, SYSDATETIME())``), the watermark read/initialise/compute/advance pattern (T19, X4),
``[WITH cte AS (...)] SELECT ... INTO #temp FROM ... JOIN ...`` with ``ROW_NUMBER() ... = 1`` dedupe
(T16, T17, T18), ``INSERT INTO table (...) SELECT ...``, ``MERGE ... USING #temp`` (T20), ``OUTPUT $action
INTO @t`` counts and ``etl.job_log`` writes in ``TRY``/``CATCH``. Triggers become a minimal shell plus a
``trigger`` construct (T21) and a ``source_row_mutation`` construct for updates of the trigger table (T22).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from migrateiq.ir.models import (
    Column,
    ColumnMapping,
    DataQualityRule,
    DatasetRef,
    DataType,
    DeduplicateStep,
    DerivedColumnStep,
    DQRuleType,
    Expression,
    FilterStep,
    JoinStep,
    JoinType,
    LoadMode,
    LoadStrategy,
    MigrationUnit,
    PostLoadAction,
    RunParameter,
    RunParameterKind,
    Severity,
    SourceArtifact,
    SourceTechnology,
    Step,
    Target,
    UnsupportedConstruct,
    WatermarkSpec,
)
from migrateiq.parsers import base
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.expr_rules import tsql_expr

logger = logging.getLogger(__name__)

_RUNTIME_FUNCTIONS = frozenset({"SYSDATETIME", "GETDATE"})
_STMT_START = frozenset(
    {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "SET",
        "DECLARE",
        "IF",
        "WHILE",
        "BEGIN",
        "RETURN",
        "THROW",
        "EXEC",
        "EXECUTE",
        "OPEN",
        "FETCH",
        "CLOSE",
        "DEALLOCATE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "WITH",
        "PRINT",
        "RAISERROR",
        "WAITFOR",
        "GOTO",
        "BREAK",
        "CONTINUE",
        "COMMIT",
        "ROLLBACK",
        "SAVE",
        "USE",
    }
)
_SET_OPS = frozenset({"UNION", "ALL", "EXCEPT", "INTERSECT"})
_JOIN_WORDS = frozenset({"INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "JOIN"})
_SELECT_CLAUSE_END = frozenset(
    {"WHERE", "GROUP", "HAVING", "ORDER", "OPTION", "UNION", "EXCEPT", "INTERSECT"}
)
_BUSINESS_RULE = re.compile(r"Business rule\s+(BR-\d+)\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
_TWO_CHAR_PUNCT = ("<>", "!=", "<=", ">=", "!<", "!>", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "::")
_WORD = re.compile(r"[^\W\d][\w#$@]*|[#$][\w#$@]+")
_VAR = re.compile(r"@@?[\w#$@]+")
_NUMBER = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def slug(name: str) -> str:
    """Lower-case ``name``, turn runs of non-alphanumerics into ``_`` and strip leading/trailing ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


class TsqlSyntaxError(ValueError):
    """Raised when the T-SQL text cannot be tokenized or its block structure is unbalanced."""


class _NotRecognized(Exception):  # noqa: N818 - internal control flow, never escapes this module
    """A statement does not match any recognised shape; becomes an ``UnsupportedConstruct``."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Tok:
    kind: str  # word, var, string, number, quoted, punct
    text: str
    start: int
    end: int  # exclusive

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == "word" else ""

    @property
    def name(self) -> str:
        """Identifier text without ``[]`` / ``""`` quoting."""
        if self.kind == "quoted":
            return self.text[1:-1].replace("]]", "]").replace('""', '"')
        return self.text


@dataclass(frozen=True)
class _Comment:
    start: int
    end: int
    body: str


def _tokenize(src: str) -> tuple[list[_Tok], list[_Comment]]:  # noqa: C901 - one linear scanner
    toks: list[_Tok] = []
    comments: list[_Comment] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
        elif src.startswith("--", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            comments.append(_Comment(i, j, src[i + 2 : j]))
            i = j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while depth:
                if j >= n:
                    raise TsqlSyntaxError(f"unterminated /* comment at offset {i}")
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            comments.append(_Comment(i, j, src[i + 2 : j - 2]))
            i = j
        elif ch == "'" or (ch in "Nn" and src.startswith("'", i + 1)):
            j = i + (2 if ch != "'" else 1)
            while True:
                if j >= n:
                    raise TsqlSyntaxError(f"unterminated string literal at offset {i}")
                if src[j] == "'":
                    if src.startswith("''", j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            toks.append(_Tok("string", src[i:j], i, j))
            i = j
        elif ch in '["':
            close = "]" if ch == "[" else '"'
            j = i + 1
            while True:
                if j >= n:
                    raise TsqlSyntaxError(f"unterminated quoted identifier at offset {i}")
                if src[j] == close:
                    if src.startswith(close * 2, j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            toks.append(_Tok("quoted", src[i:j], i, j))
            i = j
        elif ch == "@":
            m = _VAR.match(src, i)
            if not m:
                toks.append(_Tok("punct", ch, i, i + 1))
                i += 1
            else:
                toks.append(_Tok("var", m.group(0), i, m.end()))
                i = m.end()
        elif ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUMBER.match(src, i)
            assert m is not None
            toks.append(_Tok("number", m.group(0), i, m.end()))
            i = m.end()
        elif (m := _WORD.match(src, i)) is not None:
            toks.append(_Tok("word", m.group(0), i, m.end()))
            i = m.end()
        else:
            two = src[i : i + 2]
            width = 2 if two in _TWO_CHAR_PUNCT else 1
            toks.append(_Tok("punct", src[i : i + width], i, i + width))
            i += width
    return toks, comments


# ---------------------------------------------------------------------------
# Block tree
# ---------------------------------------------------------------------------
@dataclass
class _Leaf:
    lo: int
    hi: int  # token range [lo, hi), includes the terminating ';' if present


@dataclass
class _Block:
    kind: str  # begin, try, catch
    lo: int
    hi: int
    children: list[_Node]
    body_lo: int
    body_hi: int


@dataclass
class _Cond:
    keyword: str  # IF, WHILE
    lo: int
    hi: int
    cond_lo: int
    cond_hi: int
    then: _Node
    otherwise: _Node | None


_Node = _Leaf | _Block | _Cond


class _Doc:
    """Source text, tokens and comments of one file, plus slicing helpers."""

    def __init__(self, src: str) -> None:
        self.src = src
        self.toks, self.comments = _tokenize(src)

    def up(self, i: int) -> str:
        return self.toks[i].up if 0 <= i < len(self.toks) else ""

    def is_punct(self, i: int, text: str) -> bool:
        return 0 <= i < len(self.toks) and self.toks[i].kind == "punct" and self.toks[i].text == text

    def text(self, lo: int, hi: int) -> str:
        """Exact source slice covering tokens ``[lo, hi)`` (comments and whitespace inside are kept)."""
        if hi <= lo:
            return ""
        return self.src[self.toks[lo].start : self.toks[hi - 1].end]

    def node_text(self, node: _Node) -> str:
        return self.text(node.lo, node.hi)

    def strip_semicolon(self, lo: int, hi: int) -> int:
        while hi > lo and self.is_punct(hi - 1, ";"):
            hi -= 1
        return hi

    def matching_paren(self, i: int, hi: int) -> int:
        """Index of the ``)`` matching the ``(`` at ``i``."""
        depth = 0
        for j in range(i, hi):
            if self.is_punct(j, "("):
                depth += 1
            elif self.is_punct(j, ")"):
                depth -= 1
                if depth == 0:
                    return j
        raise TsqlSyntaxError(f"unbalanced parentheses at offset {self.toks[i].start}")

    def depth0(self, lo: int, hi: int) -> Iterator[int]:
        """Indices in ``[lo, hi)`` at parenthesis depth 0 (relative to ``lo``)."""
        depth = 0
        for j in range(lo, hi):
            if self.is_punct(j, "("):
                if depth == 0:
                    yield j
                depth += 1
            elif self.is_punct(j, ")"):
                depth -= 1
                if depth == 0:
                    yield j
            elif depth == 0:
                yield j

    def find0(self, lo: int, hi: int, word: str) -> int | None:
        return next((j for j in self.depth0(lo, hi) if self.up(j) == word), None)

    def split_commas(self, lo: int, hi: int) -> list[tuple[int, int]]:
        parts: list[tuple[int, int]] = []
        start = lo
        for j in self.depth0(lo, hi):
            if self.is_punct(j, ","):
                parts.append((start, j))
                start = j + 1
        parts.append((start, hi))
        return [p for p in parts if p[1] > p[0]]

    def comments_between(self, start: int, end: int) -> list[_Comment]:
        return [c for c in self.comments if start <= c.start and c.end <= end]


class _Splitter:
    """Split a token range into a tree of blocks, conditionals and leaf statements."""

    def __init__(self, doc: _Doc, lo: int, hi: int) -> None:
        self.doc = doc
        self.pos = lo
        self.hi = hi

    def statements(self, stop: Callable[[int], bool] | None = None) -> list[_Node]:
        out: list[_Node] = []
        doc = self.doc
        while self.pos < self.hi:
            if doc.is_punct(self.pos, ";"):
                self.pos += 1
                continue
            if stop is not None and stop(self.pos):
                return out
            if doc.up(self.pos) == "END":
                raise TsqlSyntaxError(f"unexpected END at offset {doc.toks[self.pos].start}")
            out.append(self.statement())
        if stop is not None:
            raise TsqlSyntaxError("missing END")
        return out

    def _optional_semicolon(self) -> None:
        if self.pos < self.hi and self.doc.is_punct(self.pos, ";"):
            self.pos += 1

    def statement(self) -> _Node:
        doc = self.doc
        word = doc.up(self.pos)
        if word == "BEGIN":
            nxt = doc.up(self.pos + 1)
            if nxt in ("TRY", "CATCH"):
                lo = self.pos
                self.pos += 2
                body_lo = self.pos
                children = self.statements(lambda p: doc.up(p) == "END" and doc.up(p + 1) == nxt)
                body_hi = self.pos
                self.pos += 2
                self._optional_semicolon()
                return _Block(nxt.lower(), lo, self.pos, children, body_lo, body_hi)
            if nxt not in ("TRAN", "TRANSACTION", "DISTRIBUTED"):
                lo = self.pos
                self.pos += 1
                body_lo = self.pos
                children = self.statements(
                    lambda p: doc.up(p) == "END" and doc.up(p + 1) not in ("TRY", "CATCH")
                )
                body_hi = self.pos
                self.pos += 1
                self._optional_semicolon()
                return _Block("begin", lo, self.pos, children, body_lo, body_hi)
        if word in ("IF", "WHILE"):
            lo = self.pos
            cond_lo = self.pos + 1
            cond_hi = self._condition_end(cond_lo)
            self.pos = cond_hi
            while self.pos < self.hi and doc.is_punct(self.pos, ";"):
                self.pos += 1
            then = self.statement()
            otherwise: _Node | None = None
            if word == "IF" and doc.up(self.pos) == "ELSE":
                self.pos += 1
                otherwise = self.statement()
            return _Cond(word, lo, self.pos, cond_lo, cond_hi, then, otherwise)
        lo = self.pos
        self.pos = self._leaf_end(lo)
        return _Leaf(lo, self.pos)

    def _condition_end(self, lo: int) -> int:
        doc = self.doc
        case = 0
        for j in doc.depth0(lo, self.hi):
            word = doc.up(j)
            if word == "CASE":
                case += 1
            elif word == "END" and case:
                case -= 1
            elif (
                j > lo
                and case == 0
                and word in _STMT_START
                and not (word == "UPDATE" and doc.is_punct(j + 1, "("))
            ):
                return j
        raise TsqlSyntaxError(f"IF/WHILE without a statement at offset {doc.toks[lo - 1].start}")

    def _leaf_end(self, lo: int) -> int:  # noqa: C901 - statement-boundary rules in one place
        doc = self.doc
        lead = doc.up(lo)
        body_seen = False
        set_seen = False
        depth = 0
        case = 0
        j = lo + 1
        while j < self.hi:
            tok = doc.toks[j]
            if tok.kind == "punct":
                if tok.text == ";" and depth == 0:
                    return j + 1
                if tok.text == "(":
                    depth += 1
                elif tok.text == ")":
                    depth -= 1
                    if depth < 0:
                        raise TsqlSyntaxError(f"unbalanced ')' at offset {tok.start}")
            elif depth == 0 and tok.kind == "word":
                word = tok.up
                prev = doc.up(j - 1)
                if word == "CASE":
                    case += 1
                elif word == "END":
                    if not case:
                        return j
                    case -= 1
                elif case == 0 and (word in _STMT_START or word == "ELSE"):
                    if word == "WITH" and doc.is_punct(j + 1, "("):
                        pass  # table hint, e.g. WITH (NOLOCK)
                    elif word == "IF" and lead in ("DROP", "CREATE", "ALTER") and doc.up(j + 1) == "EXISTS":
                        pass  # DROP TABLE IF EXISTS
                    elif prev == "FOR":
                        pass  # DECLARE c CURSOR FOR SELECT / FOR UPDATE
                    elif (
                        lead == "WITH"
                        and not body_seen
                        and word in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE")
                    ):
                        lead, body_seen = word, word != "INSERT"
                    elif word == "SELECT" and prev in _SET_OPS:
                        pass
                    elif lead == "MERGE" and word in ("UPDATE", "SET", "INSERT", "DELETE"):
                        pass
                    elif lead == "INSERT" and not body_seen and word in ("SELECT", "EXEC", "EXECUTE"):
                        body_seen = True
                    elif lead == "UPDATE" and not set_seen and word == "SET":
                        set_seen = True
                    else:
                        return j
            j += 1
        return self.hi


def _leaves(nodes: list[_Node]) -> Iterator[_Leaf]:
    for node in nodes:
        if isinstance(node, _Leaf):
            yield node
        elif isinstance(node, _Block):
            yield from _leaves(node.children)
        else:
            yield from _leaves([node.then] + ([node.otherwise] if node.otherwise else []))


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
@dataclass
class _Param:
    name: str
    type_text: str
    default_text: str | None
    snippet: str


@dataclass
class _Header:
    kind: str  # procedure, trigger
    create_lo: int
    schema: str | None
    name: str
    params: list[_Param]
    options: str | None
    trigger_schema: str | None = None
    trigger_table: str | None = None
    trigger_timing: str | None = None
    trigger_events: list[str] = field(default_factory=list)
    body_lo: int = 0
    body_hi: int = 0


def _dotted_name(doc: _Doc, i: int) -> tuple[list[str], int]:
    parts = [doc.toks[i].name]
    i += 1
    while doc.is_punct(i, ".") and i + 1 < len(doc.toks):
        parts.append(doc.toks[i + 1].name)
        i += 2
    return parts, i


def _parse_header(doc: _Doc, lo: int, hi: int) -> _Header | None:  # noqa: C901 - one linear header grammar
    i = lo
    if doc.up(i) == "CREATE":
        i += 1
        if doc.up(i) == "OR" and doc.up(i + 1) == "ALTER":
            i += 2
    elif doc.up(i) == "ALTER":
        i += 1
    else:
        return None
    kind_word = doc.up(i)
    if kind_word not in ("PROC", "PROCEDURE", "TRIGGER"):
        return None
    kind = "trigger" if kind_word == "TRIGGER" else "procedure"
    parts, i = _dotted_name(doc, i + 1)
    schema = parts[-2] if len(parts) > 1 else None
    header = _Header(kind=kind, create_lo=lo, schema=schema, name=parts[-1], params=[], options=None)

    if kind == "trigger":
        if doc.up(i) != "ON":
            raise TsqlSyntaxError("CREATE TRIGGER without ON <table>")
        table_parts, i = _dotted_name(doc, i + 1)
        header.trigger_schema = table_parts[-2] if len(table_parts) > 1 else None
        header.trigger_table = table_parts[-1]
        if doc.up(i) == "WITH":
            opt_lo = i
            while i < hi and doc.up(i) not in ("FOR", "AFTER", "INSTEAD"):
                i += 1
            header.options = doc.text(opt_lo, i)
        if doc.up(i) == "INSTEAD" and doc.up(i + 1) == "OF":
            header.trigger_timing, i = "INSTEAD OF", i + 2
        elif doc.up(i) in ("FOR", "AFTER"):
            header.trigger_timing, i = doc.up(i), i + 1
        else:
            raise TsqlSyntaxError("CREATE TRIGGER without FOR/AFTER/INSTEAD OF")
        while i < hi and doc.up(i) != "AS":
            if doc.toks[i].kind == "word" and doc.up(i) in ("INSERT", "UPDATE", "DELETE"):
                header.trigger_events.append(doc.up(i))
            elif not doc.is_punct(i, ","):
                header.options = (header.options + " " if header.options else "") + doc.toks[i].text
            i += 1
        if i >= hi:
            raise TsqlSyntaxError("CREATE TRIGGER without AS")
        header.body_lo, header.body_hi = i + 1, hi
        return header

    as_idx = next(
        (j for j in doc.depth0(i, hi) if doc.up(j) == "AS" and doc.up(j - 1) not in ("EXEC", "EXECUTE")), None
    )
    if as_idx is None:
        raise TsqlSyntaxError("CREATE PROCEDURE without AS")
    p_lo, p_hi = i, as_idx
    with_idx = doc.find0(p_lo, p_hi, "WITH")
    if with_idx is not None:
        header.options = doc.text(with_idx, p_hi)
        p_hi = with_idx
    if p_hi > p_lo and doc.is_punct(p_lo, "(") and doc.matching_paren(p_lo, p_hi) == p_hi - 1:
        p_lo, p_hi = p_lo + 1, p_hi - 1
    for seg_lo, seg_hi in doc.split_commas(p_lo, p_hi):
        if doc.toks[seg_lo].kind != "var":
            raise TsqlSyntaxError(f"cannot read procedure parameter {doc.text(seg_lo, seg_hi)!r}")
        eq = next((j for j in doc.depth0(seg_lo, seg_hi) if doc.is_punct(j, "=")), None)
        type_hi = eq if eq is not None else seg_hi
        default = None if eq is None else doc.text(eq + 1, seg_hi)
        header.params.append(
            _Param(
                name=doc.toks[seg_lo].text,
                type_text=doc.text(seg_lo + 1, type_hi),
                default_text=default,
                snippet=doc.text(seg_lo, seg_hi),
            )
        )
    header.body_lo, header.body_hi = as_idx + 1, hi
    return header


def _batches(doc: _Doc) -> list[tuple[int, int]]:
    """Token ranges of the batches separated by ``GO`` (a ``GO`` that starts its line)."""
    out: list[tuple[int, int]] = []
    start = 0
    for i, tok in enumerate(doc.toks):
        line_start = doc.src.rfind("\n", 0, tok.start) + 1
        if tok.up == "GO" and not doc.src[line_start : tok.start].strip():
            out.append((start, i))
            start = i + 1
    out.append((start, len(doc.toks)))
    return [b for b in out if b[1] > b[0]]


# ---------------------------------------------------------------------------
# sqlglot helpers
# ---------------------------------------------------------------------------
def _parse_sql(text: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(text, read="tsql")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as err:
        logger.debug("sqlglot could not parse statement (%s): %r", err, text)
        return None


def _table_parts(table: exp.Expression | None) -> tuple[str | None, str, bool] | None:
    """``(schema or None, name, is_temp)`` of a real or temp table; None for anything else."""
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        return None
    temp = bool(table.this.args.get("temporary")) or table.name.startswith("#")
    name = table.name.lstrip("#")
    return (table.db or None), (f"#{name}" if temp else name), temp


def _table_alias(table: exp.Table) -> str:
    alias = table.args.get("alias")
    return alias.name if isinstance(alias, exp.TableAlias) and alias.name else table.name


def _ordered_columns(trees: list[exp.Expression], qualifier: str | None) -> list[str]:
    """Column names used with ``qualifier`` (case-insensitive), in order of appearance, deduplicated."""
    found: list[tuple[int, int, str]] = []
    columns = [c for tree in trees if tree is not None for c in tree.find_all(exp.Column)]
    for order, col in enumerate(columns):
        if qualifier is not None and col.table.lower() != qualifier.lower():
            continue
        start = col.this.meta.get("start") if isinstance(col.this, exp.Expression) else None
        found.append((start if isinstance(start, int) else 10**9, order, col.name))
    names: list[str] = []
    for _, _, name in sorted(found):
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names


def _is_number(node: exp.Expression | None, value: str | None = None) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and (value is None or node.this == value)


def _is_scalar_literal(node: exp.Expression | None) -> bool:
    if isinstance(node, exp.Neg):
        return _is_number(node.this)
    return isinstance(node, (exp.Literal, exp.National, exp.Null))


# ---------------------------------------------------------------------------
# Shared builder state
# ---------------------------------------------------------------------------
class _Builder:
    """State shared by the procedure and trigger builders."""

    def __init__(self, doc: _Doc, header: _Header, catalog: Catalog) -> None:
        self.doc = doc
        self.header = header
        self.catalog = catalog
        self.rules: list[str] = []
        self.unsupported: list[UnsupportedConstruct] = []
        self.runtime_spellings: set[str] = set()
        self._system_rule_added = False

    def add_rule(self, rule: str) -> None:
        if rule not in self.rules:
            self.rules.append(rule)

    def add_unsupported(self, kind: str, snippet: str, reason: str, suggested: str | None = None) -> None:
        logger.warning("Unsupported T-SQL construct (%s): %s", kind, reason)
        self.unsupported.append(
            UnsupportedConstruct(
                kind=kind, original_snippet=snippet, reason=reason, suggested_pattern=suggested
            )
        )

    def system_of(self, schema: str | None) -> str:
        system = base.SCHEMA_SYSTEMS.get((schema or "dbo").lower())
        if system is None:
            raise _NotRecognized("unknown_schema", f"No logical system known for schema {schema!r}")
        return system

    def note_system_rule(self) -> None:
        if not self._system_rule_added:
            self._system_rule_added = True
            self.add_rule(base.SYSTEM_RULE)

    def catalog_columns(self, schema: str | None, table: str, names: list[str]) -> list[Column]:
        columns: list[Column] = []
        for name in names:
            column = self.catalog.column(schema or "dbo", table, name)
            if column is None:
                raise _NotRecognized(
                    "unknown_column", f"Column {schema}.{table}.{name} is not in the catalog"
                )
            columns.append(column)
        return columns

    def scan_runtime_spellings(self, lo: int, hi: int) -> None:
        doc = self.doc
        for i in range(lo, hi):
            word = doc.up(i)
            if word in _RUNTIME_FUNCTIONS and doc.is_punct(i + 1, "(") and doc.is_punct(i + 2, ")"):
                self.runtime_spellings.add(f"{doc.toks[i].text}()")
            elif word == "CURRENT_TIMESTAMP":
                self.runtime_spellings.add(doc.toks[i].text)

    def rule_comments(self, start: int, end: int) -> list[tuple[str, str]]:
        """``(BR-id, text)`` of ``Business rule BR-n: ...`` comments in a source span."""
        out: list[tuple[str, str]] = []
        for comment in self.doc.comments_between(start, end):
            m = _BUSINESS_RULE.search(comment.body)
            if m:
                out.append((m.group(1).upper(), _collapse_ws(m.group(2))))
        return out


# ---------------------------------------------------------------------------
# SELECT pipeline (CTE + SELECT [INTO] + joins -> steps)
# ---------------------------------------------------------------------------
@dataclass
class _Var:
    name: str
    literal: exp.Expression | None = None
    table_columns: list[str] = field(default_factory=list)
    is_table: bool = False
    snippet: str = ""


@dataclass
class _Pipeline:
    sources: list[DatasetRef]
    references: list[DatasetRef]
    steps: list[Step]
    rules: list[str]
    output: str
    mappings: list[ColumnMapping]
    inner_joins: list[str]


@dataclass
class _Staged:
    step_id: str
    types: dict[str, DataType | None]
    inner_joins: list[str]


class _ProcBuilder(_Builder):
    """Build a ``MigrationUnit`` from a stored procedure body."""

    def __init__(self, doc: _Doc, header: _Header, catalog: Catalog) -> None:
        super().__init__(doc, header, catalog)
        self.sources: list[DatasetRef] = []
        self.references: list[DatasetRef] = []
        self.steps: list[Step] = []
        self.targets: list[Target] = []
        self.post_actions: list[PostLoadAction] = []
        self.params = {p.name.lower(): p for p in header.params}
        self.vars: dict[str, _Var] = {}
        self.run_ts_vars: list[str] = []
        self.staged: dict[str, _Staged] = {}
        self.temp_targets: dict[str, tuple[str | None, str]] = {}
        self.merge_out_vars: set[str] = set()
        self.count_vars: dict[str, str] = {}
        self.wm_read: dict[str, str] | None = None
        self.wm_init: str | None = None
        self.wm_compute: dict[str, str] | None = None
        self.wm_advance = False
        self.wm_snippets: list[str] = []
        self.pending_merge: dict[str, object] | None = None

    # ------------------------------------------------------------------ helpers
    def var_known(self, name: str | None) -> bool:
        return name is not None and (name.lower() in self.vars or name.lower() in self.params)

    def is_run_ts(self, name: str | None) -> bool:
        return name is not None and name.lower() in {v.lower() for v in self.run_ts_vars}

    def watermark_vars(self) -> list[str]:
        return [self.wm_read["var"]] if self.wm_read else []

    def translate(self, text: str, resolver: tsql_expr.ColumnTypeResolver) -> Expression:
        return tsql_expr.translate(text, resolver, self.run_ts_vars, self.watermark_vars())

    def describe_value(self, node: exp.Expression, text: str) -> str:
        name = tsql_expr.variable_name(node)
        if self.is_run_ts(name):
            return "run_ts"
        if isinstance(node, exp.Null):
            return "NULL"
        return text

    # ------------------------------------------------------------------ entry
    def build(self, nodes: list[_Node]) -> None:
        for leaf in _leaves(nodes):
            tree = _parse_sql(self.doc.node_text(leaf))
            if isinstance(tree, exp.Merge):
                using = _table_parts(tree.args.get("using"))
                target = _table_parts(tree.this)
                if using and target and using[2] and not target[2]:
                    self.temp_targets[using[1].lower()] = (target[0], target[1])
        self.walk(nodes, in_try=False, start=self.doc.toks[self.header.body_lo].start)

    def walk(self, nodes: list[_Node], in_try: bool, start: int) -> None:
        prev_end = start
        for index, node in enumerate(nodes):
            node_start = self.doc.toks[node.lo].start
            for br, text in self.rule_comments(prev_end, node_start):
                self.add_rule(f"{br}: {text}")
            prev_end = self.doc.toks[node.hi - 1].end
            try:
                if isinstance(node, _Block):
                    if node.kind == "begin":
                        self.walk(node.children, in_try, self.doc.toks[node.lo].end)
                    elif node.kind == "try":
                        nxt = nodes[index + 1] if index + 1 < len(nodes) else None
                        if not (isinstance(nxt, _Block) and nxt.kind == "catch"):
                            raise _NotRecognized("error_handling", "BEGIN TRY without BEGIN CATCH")
                        self.walk(node.children, True, self.doc.toks[node.lo + 1].end)
                    else:
                        self.catch_block(node)
                elif isinstance(node, _Cond):
                    self.conditional(node)
                else:
                    self.leaf(node, in_try)
            except _NotRecognized as err:
                self.add_unsupported(err.kind, self.doc.node_text(node), err.reason)
            except TsqlSyntaxError as err:
                self.add_unsupported("unrecognized_statement", self.doc.node_text(node), str(err))

    # ------------------------------------------------------------------ leaves
    def leaf(self, node: _Leaf, in_try: bool) -> None:  # noqa: C901 - statement dispatch table
        doc = self.doc
        text = doc.node_text(node)
        first, second = doc.up(node.lo), doc.up(node.lo + 1)
        body_hi = doc.strip_semicolon(node.lo, node.hi)
        if first == "SET" and second == "NOCOUNT" and doc.up(node.lo + 2) == "ON" and body_hi == node.lo + 3:
            logger.debug("Ignored: %s", text)
            return
        if first == "RETURN" and body_hi == node.lo + 1:
            logger.debug("Ignored: %s", text)
            return
        if first == "RETURN":
            raise _NotRecognized(
                "return_value", "RETURN with a value (procedure return code) has no IR equivalent"
            )
        if first == "THROW":
            raise _NotRecognized("error_handling", "THROW outside a recognised BEGIN CATCH block")
        if first in ("OPEN", "FETCH", "CLOSE", "DEALLOCATE"):
            raise _NotRecognized("cursor", "Cursor processing is row-by-row; no IR equivalent")
        if first in ("EXEC", "EXECUTE"):
            if doc.is_punct(node.lo + 1, "(") or second == "SP_EXECUTESQL":
                raise _NotRecognized("dynamic_sql", "Dynamic SQL cannot be analysed statically")
            raise _NotRecognized("procedure_call", "Call to another procedure; its logic is not visible here")
        if first in ("COMMIT", "ROLLBACK", "SAVE") or (
            first == "BEGIN" and second in ("TRAN", "TRANSACTION")
        ):
            raise _NotRecognized("transaction_control", "Explicit transaction control has no IR equivalent")
        if first == "DECLARE":
            if any(doc.up(j) == "CURSOR" for j in doc.depth0(node.lo, body_hi)):
                raise _NotRecognized(
                    "cursor", "Cursor declaration; row-by-row processing has no IR equivalent"
                )
            self.declare(node, text)
            return
        if first == "SET" and doc.toks[node.lo + 1].kind == "var":
            self.set_variable(node, text)
            return
        if first == "SET":
            raise _NotRecognized("session_option", "SET session option has no IR equivalent")
        if first == "DROP":
            tree = _parse_sql(text)
            if (
                isinstance(tree, exp.Drop)
                and str(tree.args.get("kind", "")).upper() == "TABLE"
                and tree.args.get("exists")
                and all((_table_parts(t) or (None, "", False))[2] for t in tree.find_all(exp.Table))
            ):
                logger.debug("Ignored: %s", text)
                return
            raise _NotRecognized("ddl", "DROP of a permanent object is not migrated")
        if first in ("SELECT", "WITH"):
            tree = _parse_sql(text)
            if isinstance(tree, exp.Select) and isinstance(tree.args.get("into"), exp.Into):
                self.select_into(node, tree)
                return
            if (
                isinstance(tree, exp.Select)
                and tree.expressions
                and all(isinstance(e, exp.EQ) and tsql_expr.variable_name(e.this) for e in tree.expressions)
            ):
                self.variable_select(node, tree, text)
                return
            raise _NotRecognized(
                "unrecognized_statement", "SELECT that is neither SELECT INTO #temp nor a variable assignment"
            )
        if first == "MERGE":
            tree = _parse_sql(text)
            if not isinstance(tree, exp.Merge):
                raise _NotRecognized("merge", "MERGE statement could not be parsed")
            self.merge(node, tree)
            return
        if first == "INSERT":
            tree = _parse_sql(text)
            if isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Values):
                self.post_actions.append(self.job_log(node, tree, on="success"))
                return
            if isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Select):
                self.insert_select(node, tree)
                return
            raise _NotRecognized(
                "insert", "INSERT is neither INSERT ... SELECT nor a job_log INSERT ... VALUES"
            )
        raise _NotRecognized("unrecognized_statement", "Statement not recognised by the T-SQL parser")

    def declare(self, node: _Leaf, text: str) -> None:
        tree = _parse_sql(text)
        if not isinstance(tree, exp.Declare):
            raise _NotRecognized("unrecognized_statement", "DECLARE could not be parsed")
        found: list[_Var] = []
        for item in tree.expressions:
            names = item.this if isinstance(item.this, list) else [item.this]
            kind = item.args.get("kind")
            default = item.args.get("default") or None
            for node_name in names:
                name = tsql_expr.variable_name(node_name)
                if name is None:
                    raise _NotRecognized(
                        "unrecognized_statement", "DECLARE of something other than a variable"
                    )
                if isinstance(kind, exp.Schema):
                    cols = [c.name for c in kind.expressions if isinstance(c, exp.ColumnDef)]
                    found.append(_Var(name, None, cols, True, text))
                elif default is not None and not _is_scalar_literal(default):
                    raise _NotRecognized(
                        "variable_initializer", f"DECLARE {name} has a non-literal initial value"
                    )
                else:
                    found.append(_Var(name, default, [], False, text))
        for var in found:
            self.vars[var.name.lower()] = var
            logger.debug("Recorded variable %s from: %s", var.name, text)

    def set_variable(self, node: _Leaf, text: str) -> None:
        tree = _parse_sql(text)
        items = tree.expressions if isinstance(tree, exp.Set) else []
        if len(items) != 1 or not isinstance(items[0], exp.SetItem) or not isinstance(items[0].this, exp.EQ):
            raise _NotRecognized("variable_assignment", "SET could not be parsed as one variable assignment")
        eq = items[0].this
        name = tsql_expr.variable_name(eq.this)
        value = eq.expression
        if not self.var_known(name):
            raise _NotRecognized("variable_assignment", f"SET of undeclared variable {name}")
        if isinstance(value, exp.CurrentTimestamp) or (
            isinstance(value, exp.Coalesce)
            and tsql_expr.variable_name(value.this) == name
            and len(value.expressions) == 1
            and isinstance(value.expressions[0], exp.CurrentTimestamp)
        ):
            spelling = doc_var_spelling(self.doc, node, name or "")
            if spelling not in self.run_ts_vars:
                self.run_ts_vars.append(spelling)
            return
        if (
            self.wm_read is not None
            and name is not None
            and name.lower() == self.wm_read["var"].lower()
            and isinstance(value, exp.Coalesce)
            and tsql_expr.variable_name(value.this) == name
            and len(value.expressions) == 1
            and isinstance(value.expressions[0], exp.Literal)
            and value.expressions[0].is_string
        ):
            self.wm_init = value.expressions[0].this
            self.wm_snippets.append(text)
            return
        raise _NotRecognized("variable_assignment", f"SET {name} is outside the run_ts / watermark patterns")

    def variable_select(self, node: _Leaf, tree: exp.Select, text: str) -> None:
        from_ = tree.args.get("from_")
        source = from_.this if isinstance(from_, exp.From) else None
        if tree.args.get("joins") or tree.args.get("group") or tree.args.get("with_"):
            raise _NotRecognized("variable_select", "Variable SELECT with joins, GROUP BY or CTEs")
        items = [(tsql_expr.variable_name(e.this), e.expression) for e in tree.expressions]
        # Row counts of a MERGE OUTPUT table variable.
        if isinstance(source, exp.Table) and tsql_expr.variable_name(source.this):
            table_var = tsql_expr.variable_name(source.this) or ""
            if table_var.lower() not in self.merge_out_vars:
                raise _NotRecognized(
                    "variable_select", f"SELECT from table variable {table_var} not fed by MERGE OUTPUT"
                )
            counts: dict[str, str] = {}
            for var, value in items:
                action = _count_of_action(value)
                if action is None or not self.var_known(var):
                    raise _NotRecognized(
                        "variable_select",
                        "Only SUM(CASE WHEN col = '<action>' THEN 1 ELSE 0 END) is recognised",
                    )
                counts[(var or "").lower()] = action
            self.count_vars.update(counts)
            columns = {c: "$action" for c in self.vars[table_var.lower()].table_columns}
            self.post_actions.append(
                PostLoadAction(
                    kind="row_counts",
                    on="success",
                    target_table=table_var,
                    columns=columns,
                    original_snippet=text,
                )
            )
            return
        parts = _table_parts(source)
        if parts is None or len(items) != 1:
            raise _NotRecognized("variable_select", "Variable SELECT not recognised")
        var, value = items[0]
        schema, table, temp = parts
        where = tree.args.get("where")
        # Watermark compute: SELECT @New = MAX(col) FROM #staged.
        if temp and isinstance(value, exp.Max) and isinstance(value.this, exp.Column) and where is None:
            staged = self.staged.get(table.lower())
            if staged is None:
                raise _NotRecognized(
                    "variable_select", f"MAX over {table}, which is not a recognised staging table"
                )
            self.wm_compute = {
                "var": var or "",
                "column": value.this.name,
                "step": staged.step_id,
                "temp": table,
            }
            self.wm_snippets.append(text)
            if staged.inner_joins:
                self.add_rule(
                    f"Watermark = MAX({value.this.name}) of {table}, i.e. after the INNER JOIN to "
                    f"{', '.join(staged.inner_joins)}; join-dropped rows with an older {value.this.name} "
                    "are not re-read (X4)."
                )
            return
        # Watermark read: SELECT @v = col FROM store WHERE key = @job.
        job_var = (
            tsql_expr.variable_name(where.this.expression)
            if isinstance(where, exp.Where) and isinstance(where.this, exp.EQ)
            else None
        )
        key = where.this.this if job_var else None
        if (
            not temp
            and schema
            and isinstance(value, exp.Column)
            and isinstance(key, exp.Column)
            and job_var
            and self.vars.get(job_var.lower()) is not None
            and isinstance(self.vars[job_var.lower()].literal, exp.Literal)
            and self.wm_read is None
        ):
            literal = self.vars[job_var.lower()].literal
            assert isinstance(literal, exp.Literal)
            self.wm_read = {
                "var": var or "",
                "store": f"{schema}.{table}",
                "schema": schema,
                "table": table,
                "column": value.name,
                "key": key.name,
                "job_var": job_var,
                "job_name": literal.this,
            }
            self.wm_snippets.append(text)
            return
        raise _NotRecognized("variable_select", "Variable SELECT outside the watermark / row-count patterns")

    # ------------------------------------------------------------------ SELECT INTO / INSERT SELECT
    def select_into(self, node: _Leaf, tree: exp.Select) -> None:
        into = tree.args["into"]
        parts = _table_parts(into.this)
        if parts is None or not parts[2]:
            raise _NotRecognized("select_into", "SELECT INTO a permanent table is not supported")
        temp = parts[1]
        target = self.temp_targets.get(temp.lower())
        target_types: dict[str, DataType] = {}
        if target is not None:
            for col in self.catalog.table(target[0] or "dbo", target[1]) or []:
                target_types[col.name.lower()] = col.data_type
        pipe = self.pipeline(
            node, tree, output_id=slug(temp), component=temp, target_types=target_types, target_columns=None
        )
        self.commit(pipe)
        self.staged[temp.lower()] = _Staged(
            pipe.output, {m.target_column.lower(): m.target_type for m in pipe.mappings}, pipe.inner_joins
        )

    def insert_select(self, node: _Leaf, tree: exp.Insert) -> None:
        schema_node = tree.this
        if not isinstance(schema_node, exp.Schema):
            raise _NotRecognized("insert", "INSERT ... SELECT without a column list")
        parts = _table_parts(schema_node.this)
        if parts is None or parts[2] or not parts[0]:
            raise _NotRecognized("insert", "INSERT ... SELECT into a temp or unqualified table")
        schema, table, _ = parts
        cols = [c.name for c in schema_node.expressions]
        columns = self.catalog_columns(schema, table, cols)
        types = {c.name.lower(): c.data_type for c in columns}
        select = tree.expression
        assert isinstance(select, exp.Select)
        pipe = self.pipeline(
            node,
            select,
            output_id=f"sel_{slug(table)}",
            component=f"{schema}.{table}",
            target_types=types,
            target_columns=cols,
        )
        dataset = DatasetRef(
            alias=f"tgt_{slug(table)}",
            system=self.system_of(schema),
            schema_name=schema or "",
            table=table,
            columns=columns,
        )
        self.commit(pipe)
        self.note_system_rule()
        self.targets.append(
            Target(input=pipe.output, dataset=dataset, load_strategy=LoadStrategy(mode=LoadMode.APPEND))
        )

    def commit(self, pipe: _Pipeline) -> None:
        for dataset in pipe.sources:
            if any(d.alias == dataset.alias for d in self.sources + self.references):
                raise _NotRecognized("select_into", f"Dataset alias {dataset.alias} already used")
        if pipe.sources or pipe.references:
            self.note_system_rule()
        self.sources.extend(pipe.sources)
        for ref in pipe.references:
            if not any(r.alias == ref.alias for r in self.references):
                self.references.append(ref)
        self.steps.extend(pipe.steps)
        for rule in pipe.rules:
            self.add_rule(rule)

    def pipeline(  # noqa: C901 - one linear pattern check
        self,
        node: _Leaf,
        select: exp.Select,
        output_id: str,
        component: str,
        target_types: dict[str, DataType],
        target_columns: list[str] | None,
    ) -> _Pipeline:
        doc = self.doc
        lo, hi = node.lo, doc.strip_semicolon(node.lo, node.hi)
        for arg in (
            "group",
            "having",
            "order",
            "limit",
            "distinct",
            "qualify",
            "windows",
            "laterals",
            "pivots",
        ):
            if select.args.get(arg):
                raise _NotRecognized("select_into", f"SELECT with {arg.upper()} is not supported")
        if not isinstance(select, exp.Select):
            raise _NotRecognized("select_into", "Set operations (UNION/EXCEPT/INTERSECT) are not supported")

        sources: list[DatasetRef] = []
        references: list[DatasetRef] = []
        steps: list[Step] = []
        rules: list[str] = []
        types: dict[str, dict[str, DataType | None]] = {}  # alias -> column -> type
        inner_joins: list[str] = []

        # ---- CTEs (token ranges)
        cte_ranges: dict[str, tuple[int, int]] = {}
        main = lo
        if doc.up(lo) == "WITH":
            i = lo + 1
            while True:
                name_tok = doc.toks[i]
                if doc.up(i + 1) != "AS" or not doc.is_punct(i + 2, "("):
                    raise _NotRecognized("select_into", "CTE with a column list or unexpected syntax")
                close = doc.matching_paren(i + 2, hi)
                cte_ranges[name_tok.name.lower()] = (i + 3, close)
                i = close + 1
                if doc.is_punct(i, ","):
                    i += 1
                    continue
                break
            main = i
        elif doc.up(lo) == "INSERT":
            main = next(j for j in doc.depth0(lo, hi) if doc.up(j) == "SELECT")
        if doc.up(main) != "SELECT":
            raise _NotRecognized("select_into", "Expected SELECT after the CTE list")

        ctes: dict[str, dict[str, object]] = {}
        with_ = select.args.get("with_")
        for cte in with_.expressions if isinstance(with_, exp.With) else []:
            cte_name = cte.alias.lower()
            if cte_name not in cte_ranges:
                raise _NotRecognized("select_into", f"CTE {cte.alias} not found in the text")
            ctes[cte_name] = self.cte(cte, cte_ranges[cte_name], sources, steps, rules)

        # ---- main FROM
        from_ = select.args.get("from_")
        base_table = from_.this if isinstance(from_, exp.From) else None
        parts = _table_parts(base_table)
        if parts is None or not isinstance(base_table, exp.Table):
            raise _NotRecognized("select_into", "FROM must be one table, temp table or CTE")
        schema, table, temp = parts
        base_alias = _table_alias(base_table)
        where = select.args.get("where")
        main_parts: list[exp.Expression] = [*select.expressions, *(select.args.get("joins") or [])]
        if isinstance(where, exp.Where):
            main_parts.append(where)
        rn_alias: str | None = None
        if schema is None and not temp and table.lower() in ctes:
            info = ctes[table.lower()]
            current = str(info["output"])
            types[base_alias.lower()] = dict(info["types"])  # type: ignore[call-overload]
            rn_alias = info["rn"]  # type: ignore[assignment]
            if rn_alias is not None:
                # ROW_NUMBER() is computed in the CTE, so the dedupe runs on the CTE rows, before any join.
                cond = where.this if isinstance(where, exp.Where) else None
                if not (
                    isinstance(cond, exp.EQ)
                    and isinstance(cond.this, exp.Column)
                    and cond.this.name.lower() == rn_alias.lower()
                    and cond.this.table.lower() in ("", base_alias.lower())
                    and _is_number(cond.expression, "1")
                ):
                    raise _NotRecognized(
                        "select_into",
                        f"ROW_NUMBER() is only recognised with WHERE {base_alias}.{rn_alias} = 1",
                    )
                for column in select.find_all(exp.Column):
                    if column.name.lower() == rn_alias.lower() and column is not cond.this:
                        raise _NotRecognized(
                            "select_into", f"{rn_alias} is used outside WHERE {rn_alias} = 1"
                        )
                dedup = info["dedup"]
                assert isinstance(dedup, DeduplicateStep)
                steps.append(dedup.model_copy(update={"inputs": [current]}))
                current = dedup.id
        elif temp:
            staged = self.staged.get(table.lower())
            if staged is None:
                raise _NotRecognized("select_into", f"Temp table {table} is not a recognised staging table")
            current = staged.step_id
            types[base_alias.lower()] = dict(staged.types)
        else:
            if not schema:
                raise _NotRecognized("select_into", f"Table {table} must be schema-qualified")
            cols = _ordered_columns(main_parts, base_alias)
            dataset = DatasetRef(
                alias=f"src_{slug(table)}",
                system=self.system_of(schema),
                schema_name=schema,
                table=table,
                columns=self.catalog_columns(schema, table, cols),
            )
            sources.append(dataset)
            current = dataset.alias
            types[base_alias.lower()] = {
                c.name.lower(): c.data_type for c in self.catalog.table(schema, table) or []
            }

        # ---- joins
        select_list_end = next((j for j in doc.depth0(main + 1, hi) if doc.up(j) in ("INTO", "FROM")), hi)
        join_toks = [j for j in doc.depth0(select_list_end, hi) if doc.up(j) == "JOIN"]
        joins = select.args.get("joins") or []
        if len(join_toks) != len(joins):
            raise _NotRecognized("select_into", "Could not align JOIN clauses with the parsed statement")
        clause_end = next((j for j in doc.depth0(select_list_end, hi) if doc.up(j) in _SELECT_CLAUSE_END), hi)
        for n_join, (join, join_tok) in enumerate(zip(joins, join_toks, strict=True)):
            jparts = _table_parts(join.this)
            if jparts is None or jparts[2] or not jparts[0] or not isinstance(join.this, exp.Table):
                raise _NotRecognized("select_into", "JOIN must be to a schema-qualified table")
            jschema, jtable, _ = jparts
            on = join.args.get("on")
            side = str(join.args.get("side") or "").upper()
            kind = str(join.args.get("kind") or "").upper()
            if on is None or kind == "CROSS" or join.args.get("using"):
                raise _NotRecognized("select_into", "Only JOIN ... ON is supported")
            join_type = {
                "": JoinType.INNER,
                "LEFT": JoinType.LEFT,
                "RIGHT": JoinType.RIGHT,
                "FULL": JoinType.FULL,
            }.get(side)
            if join_type is None or kind not in ("", "INNER", "OUTER"):
                raise _NotRecognized("select_into", f"Unsupported join {side} {kind}")
            on_idx = doc.find0(join_tok, hi, "ON")
            if on_idx is None:
                raise _NotRecognized("select_into", "JOIN without ON")
            if n_join + 1 < len(join_toks):
                end = join_toks[n_join + 1]
                while doc.up(end - 1) in _JOIN_WORDS - {"JOIN"}:
                    end -= 1
            else:
                end = clause_end
            jalias = _table_alias(join.this)
            cols = _ordered_columns(main_parts, jalias)
            ref = DatasetRef(
                alias=f"ref_{slug(jtable)}",
                system=self.system_of(jschema),
                schema_name=jschema or "",
                table=jtable,
                columns=self.catalog_columns(jschema, jtable, cols),
            )
            references.append(ref)
            types[jalias.lower()] = {
                c.name.lower(): c.data_type for c in self.catalog.table(jschema or "dbo", jtable) or []
            }
            resolver = _resolver(types)
            condition = self.translate(doc.text(on_idx + 1, end), resolver)
            ranged = any(isinstance(n, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)) for n in on.walk())
            step_id = f"jn_{slug(jtable)}"
            steps.append(
                JoinStep(
                    id=step_id,
                    inputs=[current, ref.alias],
                    join_type=join_type,
                    condition=condition,
                    source_component=component,
                    description=(
                        f"{join_type.value.upper()} JOIN {jschema}.{jtable} {jalias}; ON condition kept "
                        "verbatim" + (" (point-in-time range join)." if ranged else ".")
                    ),
                )
            )
            if join_type == JoinType.INNER:
                inner_joins.append(f"{jschema}.{jtable}")
                rules.append(
                    f"INNER JOIN to {jschema}.{jtable}"
                    + (" (point-in-time)" if ranged else "")
                    + f" drops {table} rows with no matching {jschema}.{jtable} row; preserve this "
                    "(T18) and report the dropped-row count."
                )
            current = step_id

        # ---- WHERE
        resolver = _resolver(types)
        if rn_alias is not None:
            pass  # WHERE <alias>.rn = 1 is the dedupe recognised above
        elif isinstance(where, exp.Where):
            where_idx = doc.find0(select_list_end, hi, "WHERE")
            if where_idx is None:
                raise _NotRecognized("select_into", "Could not locate the WHERE clause")
            flt_id = f"flt_{output_id}"
            steps.append(
                FilterStep(
                    id=flt_id,
                    inputs=[current],
                    condition=self.translate(doc.text(where_idx + 1, hi), resolver),
                    source_component=component,
                )
            )
            current = flt_id

        # ---- projection
        unqualified = [c for c in select.expressions if any(not col.table for col in c.find_all(exp.Column))]
        if unqualified and len(types) > 1:
            raise _NotRecognized("select_into", "Unqualified column in a multi-table SELECT")
        segments = doc.split_commas(main + 1, select_list_end)
        if len(segments) != len(select.expressions):
            raise _NotRecognized("select_into", "Could not align SELECT items with the parsed statement")
        if target_columns is not None and len(target_columns) != len(segments):
            raise _NotRecognized("insert", "INSERT column list and SELECT list differ in length")
        mappings: list[ColumnMapping] = []
        for position, (item, (seg_lo, seg_hi)) in enumerate(zip(select.expressions, segments, strict=True)):
            if isinstance(item, exp.Alias):
                alias = item.alias
                last = doc.toks[seg_hi - 1]
                if last.name.lower() == alias.lower() and doc.up(seg_hi - 2) == "AS":
                    expr_lo, expr_hi = seg_lo, seg_hi - 2
                elif last.name.lower() == alias.lower():
                    expr_lo, expr_hi = seg_lo, seg_hi - 1
                elif doc.toks[seg_lo].name.lower() == alias.lower() and doc.is_punct(seg_lo + 1, "="):
                    expr_lo, expr_hi = seg_lo + 2, seg_hi
                else:
                    raise _NotRecognized("select_into", f"Could not locate alias {alias} in the SELECT item")
                name = alias
            elif isinstance(item, exp.Column):
                name, expr_lo, expr_hi = item.name, seg_lo, seg_hi
            elif target_columns is not None:
                name, expr_lo, expr_hi = target_columns[position], seg_lo, seg_hi
            else:
                raise _NotRecognized("select_into", "SELECT item without a column name or alias")
            if isinstance(item, exp.Star) or any(isinstance(n, exp.Star) for n in item.walk()):
                raise _NotRecognized("select_into", "SELECT * is not supported")
            target_name = target_columns[position] if target_columns is not None else name
            mappings.append(
                ColumnMapping(
                    target_column=target_name,
                    expression=self.translate(doc.text(expr_lo, expr_hi), resolver),
                    target_type=target_types.get(target_name.lower()),
                )
            )
        steps.append(
            DerivedColumnStep(
                id=output_id,
                inputs=[current],
                source_component=component,
                mappings=mappings,
                keep_input_columns=False,
                description=f"SELECT list of the statement that fills {component}.",
            )
        )
        return _Pipeline(sources, references, steps, rules, output_id, mappings, inner_joins)

    def cte(
        self,
        cte: exp.CTE,
        token_range: tuple[int, int],
        sources: list[DatasetRef],
        steps: list[Step],
        rules: list[str],
    ) -> dict[str, object]:
        doc = self.doc
        name = cte.alias
        body = cte.this
        if not isinstance(body, exp.Select) or body.args.get("joins") or body.args.get("with_"):
            raise _NotRecognized("select_into", f"CTE {name} must be a single-table SELECT")
        for arg in ("group", "having", "order", "limit", "distinct", "into"):
            if body.args.get(arg):
                raise _NotRecognized("select_into", f"CTE {name} with {arg.upper()} is not supported")
        from_ = body.args.get("from_")
        table_node = from_.this if isinstance(from_, exp.From) else None
        parts = _table_parts(table_node)
        if parts is None or parts[2] or not parts[0] or not isinstance(table_node, exp.Table):
            raise _NotRecognized("select_into", f"CTE {name} must read one schema-qualified table")
        schema, table, _ = parts
        alias = _table_alias(table_node)
        out_types: dict[str, DataType | None] = {}
        rn: str | None = None
        window: exp.Window | None = None
        for item in body.expressions:
            if isinstance(item, exp.Column) and item.table.lower() in ("", alias.lower()):
                column = self.catalog.column(schema or "dbo", table, item.name)
                out_types[item.name.lower()] = None if column is None else column.data_type
            elif (
                isinstance(item, exp.Alias)
                and isinstance(item.this, exp.Window)
                and isinstance(item.this.this, exp.RowNumber)
                and rn is None
            ):
                rn, window = item.alias, item.this
                out_types[rn.lower()] = DataType.BIGINT
            else:
                raise _NotRecognized(
                    "select_into", f"CTE {name} projection {item.sql(dialect='tsql')!r} is not a plain column"
                )
        dataset = DatasetRef(
            alias=f"src_{slug(table)}",
            system=self.system_of(schema),
            schema_name=schema or "",
            table=table,
            columns=self.catalog_columns(schema, table, _ordered_columns([body], None)),
        )
        if any(d.alias == dataset.alias for d in sources):
            raise _NotRecognized("select_into", f"Table {table} read twice")
        sources.append(dataset)
        current = dataset.alias
        table_types = {c.name.lower(): c.data_type for c in self.catalog.table(schema or "dbo", table) or []}
        resolver = _resolver({alias.lower(): dict(table_types), "": dict(table_types)})
        where = body.args.get("where")
        body_lo, body_hi = token_range
        if isinstance(where, exp.Where):
            where_idx = doc.find0(body_lo, body_hi, "WHERE")
            if where_idx is None:
                raise _NotRecognized("select_into", f"Could not locate the WHERE clause of CTE {name}")
            flt_id = f"flt_{slug(name)}"
            steps.append(
                FilterStep(
                    id=flt_id,
                    inputs=[current],
                    condition=self.translate(doc.text(where_idx + 1, body_hi), resolver),
                    source_component=name,
                    description=(
                        "CTE WHERE clause; T-SQL applies WHERE before ROW_NUMBER()."
                        if rn
                        else "CTE WHERE clause."
                    ),
                )
            )
            current = flt_id
        dedup: DeduplicateStep | None = None
        if rn is not None and window is not None:
            partition = window.args.get("partition_by") or []
            order = window.args.get("order")
            ordered = order.expressions if isinstance(order, exp.Order) else []
            if not partition or not all(isinstance(p, exp.Column) for p in partition):
                raise _NotRecognized("select_into", "ROW_NUMBER() PARTITION BY must list plain columns")
            if not all(isinstance(o, exp.Ordered) and isinstance(o.this, exp.Column) for o in ordered):
                raise _NotRecognized("select_into", "ROW_NUMBER() ORDER BY must list plain columns")
            partition_by = [p.name for p in partition]
            order_by = [f"{o.this.name} {'DESC' if o.args.get('desc') else 'ASC'}" for o in ordered]
            dedup = DeduplicateStep(
                id=f"dedup_{slug(name)}",
                inputs=[current],
                partition_by=partition_by,
                order_by=order_by,
                source_component=name,
                description=(
                    f"ROW_NUMBER() OVER (PARTITION BY {', '.join(partition_by)} "
                    f"ORDER BY {', '.join(order_by)}) AS {rn} with WHERE {rn} = 1: one row per "
                    f"{', '.join(partition_by)}."
                ),
            )
            rules.append(
                f"ROW_NUMBER() ties on {', '.join(o.this.name for o in ordered)} for the same "
                f"{', '.join(partition_by)} are non-deterministic in SQL Server (X3)."
            )
        return {"output": current, "types": out_types, "rn": rn, "dedup": dedup}

    # ------------------------------------------------------------------ MERGE
    def merge(self, node: _Leaf, tree: exp.Merge) -> None:  # noqa: C901 - one linear pattern check
        doc = self.doc
        lo, hi = node.lo, doc.strip_semicolon(node.lo, node.hi)
        target = _table_parts(tree.this)
        using = _table_parts(tree.args.get("using"))
        if target is None or target[2] or not target[0] or not isinstance(tree.this, exp.Table):
            raise _NotRecognized("merge", "MERGE target must be a schema-qualified table")
        if using is None or not using[2] or using[1].lower() not in self.staged:
            raise _NotRecognized(
                "merge", "MERGE USING must be a staging temp table built earlier in the procedure"
            )
        schema, table, _ = target
        staged = self.staged[using[1].lower()]
        tgt_alias = _table_alias(tree.this).lower()
        src_alias = _table_alias(tree.args["using"]).lower()

        def side(col: exp.Expression) -> str | None:
            return col.table.lower() if isinstance(col, exp.Column) else None

        # business keys from ON
        keys: list[str] = []
        conjuncts = (
            list(tree.args["on"].flatten())
            if isinstance(tree.args.get("on"), exp.And)
            else [tree.args.get("on")]
        )
        for eq in conjuncts:
            if not isinstance(eq, exp.EQ):
                raise _NotRecognized("merge", "MERGE ON must be equality conditions joined by AND")
            pair = {side(eq.this): eq.this, side(eq.expression): eq.expression}
            if (
                set(pair) != {tgt_alias, src_alias}
                or pair[tgt_alias].name.lower() != pair[src_alias].name.lower()
            ):
                raise _NotRecognized("merge", "MERGE ON must compare same-named target and source columns")
            keys.append(pair[tgt_alias].name)

        update_cols: list[str] = []
        update_values: dict[str, tuple[exp.Expression, str]] = {}
        insert_cols: list[str] = []
        insert_values: dict[str, tuple[exp.Expression, str]] = {}
        condition: Expression | None = None
        resolver = _resolver(
            {
                tgt_alias: {c.name.lower(): c.data_type for c in self.catalog.table(schema, table) or []},
                src_alias: dict(staged.types),
            }
        )
        whens = tree.args.get("whens")
        for when in whens.expressions if isinstance(whens, exp.Whens) else []:
            then = when.args.get("then")
            if when.args.get("source"):
                raise _NotRecognized("merge", "WHEN NOT MATCHED BY SOURCE is not supported")
            if when.args.get("matched") and isinstance(then, exp.Update) and not update_cols:
                if when.args.get("condition") is not None:
                    condition = self.translate(self.merge_condition_text(lo, hi), resolver)
                for eq in then.expressions:
                    if not isinstance(eq, exp.EQ) or not isinstance(eq.this, exp.Column):
                        raise _NotRecognized("merge", "UPDATE SET items must be column = value")
                    update_cols.append(eq.this.name)
                    update_values[eq.this.name.lower()] = (eq.expression, eq.expression.sql(dialect="tsql"))
            elif not when.args.get("matched") and isinstance(then, exp.Insert) and not insert_cols:
                cols = then.this.expressions if isinstance(then.this, exp.Tuple) else []
                vals = then.expression.expressions if isinstance(then.expression, exp.Tuple) else []
                if not cols or len(cols) != len(vals):
                    raise _NotRecognized("merge", "INSERT column and VALUES lists must match")
                for col, val in zip(cols, vals, strict=True):
                    insert_cols.append(col.name)
                    insert_values[col.name.lower()] = (val, val.sql(dialect="tsql"))
            else:
                raise _NotRecognized(
                    "merge", "Only WHEN MATCHED [AND ...] THEN UPDATE and WHEN NOT MATCHED THEN INSERT"
                )
        if not insert_cols:
            raise _NotRecognized("merge", "MERGE without WHEN NOT MATCHED THEN INSERT")

        def from_src(value: exp.Expression, column: str) -> bool:
            return (
                isinstance(value, exp.Column)
                and value.table.lower() == src_alias
                and value.name.lower() == column.lower()
            )

        for values in (update_values, insert_values):
            for column, (value, _) in values.items():
                if isinstance(value, exp.Column):
                    if not from_src(value, column):
                        raise _NotRecognized("merge", f"{column} must be set from src.{column}")
                    if value.name.lower() not in staged.types:
                        raise _NotRecognized("merge", f"src.{value.name} is not produced by the staging step")
                elif not (isinstance(value, exp.Null) or self.is_run_ts(tsql_expr.variable_name(value))):
                    raise _NotRecognized("merge", f"{column} value must be a src column, run_ts or NULL")

        returning = tree.args.get("returning")
        out_var: str | None = None
        if returning is not None:
            out_var = tsql_expr.variable_name(returning.args.get("into"))
            items = returning.expressions
            var = self.vars.get((out_var or "").lower())
            if (
                var is None
                or not var.is_table
                or len(items) != len(var.table_columns)
                or not all(isinstance(i, exp.Column) and i.name.lower() == "$action" for i in items)
            ):
                raise _NotRecognized("merge", "Only OUTPUT $action INTO @table_variable is recognised")

        insert_only = [
            c for c in insert_cols if c.lower() not in {u.lower() for u in update_cols} and c not in keys
        ]
        update_only = [
            c
            for c in update_cols
            if c.lower() not in insert_values or not from_src(insert_values[c.lower()][0], c)
        ]
        columns = self.catalog_columns(schema, table, insert_cols)
        dataset = DatasetRef(
            alias=f"tgt_{slug(table)}",
            system=self.system_of(schema),
            schema_name=schema or "",
            table=table,
            columns=columns,
        )

        parts: list[str] = []
        for col in insert_only:
            value, text = insert_values[col.lower()]
            parts.append(f"{col} = {self.describe_value(value, text)} on INSERT")
        for col in update_only:
            value, text = update_values[col.lower()]
            on_insert = insert_values.get(col.lower())
            insert_desc = "not set" if on_insert is None else self.describe_value(*on_insert)
            parts.append(f"{col} = {self.describe_value(value, text)} on UPDATE and {insert_desc} on INSERT")

        if out_var:
            self.merge_out_vars.add(out_var.lower())
        self.note_system_rule()
        if parts:
            self.add_rule("; ".join(parts) + ".")
        self.pending_merge = {
            "dataset": dataset,
            "input": staged.step_id,
            "keys": keys,
            "condition": condition,
            "insert_only": insert_only,
            "update_only": update_only,
        }

    def merge_condition_text(self, lo: int, hi: int) -> str:
        doc = self.doc
        for j in doc.depth0(lo, hi):
            if doc.up(j) == "WHEN" and doc.up(j + 1) == "MATCHED" and doc.up(j + 2) == "AND":
                cond_lo = j + 3
                then = next((k for k in doc.depth0(cond_lo, hi) if doc.up(k) == "THEN"), None)
                if then is None:
                    break
                if doc.is_punct(cond_lo, "(") and doc.matching_paren(cond_lo, hi) == then - 1:
                    return doc.text(cond_lo + 1, then - 1)
                return doc.text(cond_lo, then)
        raise _NotRecognized("merge", "Could not locate the WHEN MATCHED AND condition text")

    # ------------------------------------------------------------------ job_log
    def job_log(self, node: _Leaf, tree: exp.Insert, on: str) -> PostLoadAction:
        doc = self.doc
        schema_node = tree.this
        parts = _table_parts(schema_node.this if isinstance(schema_node, exp.Schema) else None)
        rows = tree.expression.expressions if isinstance(tree.expression, exp.Values) else []
        if parts is None or parts[2] or not parts[0] or "log" not in parts[1].lower() or len(rows) != 1:
            raise _NotRecognized(
                "insert", "INSERT ... VALUES is only recognised as a single-row write to a log table"
            )
        assert isinstance(schema_node, exp.Schema)
        cols = [c.name for c in schema_node.expressions]
        values = rows[0].expressions if isinstance(rows[0], exp.Tuple) else []
        hi = doc.strip_semicolon(node.lo, node.hi)
        values_idx = doc.find0(node.lo, hi, "VALUES")
        if values_idx is None or not doc.is_punct(values_idx + 1, "(") or len(cols) != len(values):
            raise _NotRecognized("insert", "Could not read the VALUES list")
        close = doc.matching_paren(values_idx + 1, hi)
        segments = doc.split_commas(values_idx + 2, close)
        if len(segments) != len(values):
            raise _NotRecognized("insert", "Could not align the VALUES list")
        columns: dict[str, str] = {}
        for col, value, (seg_lo, seg_hi) in zip(cols, values, segments, strict=True):
            text = doc.text(seg_lo, seg_hi)
            var = tsql_expr.variable_name(value)
            inner = value.this if isinstance(value, exp.Coalesce) and len(value.expressions) == 1 else None
            inner_var = tsql_expr.variable_name(inner) if inner is not None else None
            if var is not None:
                if not self.var_known(var):
                    raise _NotRecognized("insert", f"Undeclared variable {var}")
                if self.is_run_ts(var):
                    columns[col] = "run_ts"
                elif var.lower() in self.count_vars:
                    columns[col] = f"MERGE OUTPUT $action count of {self.count_vars[var.lower()]}"
                else:
                    columns[col] = text
            elif inner_var is not None and _is_scalar_literal(value.expressions[0]):
                if inner_var.lower() in self.count_vars:
                    columns[col] = f"MERGE OUTPUT $action count of {self.count_vars[inner_var.lower()]}"
                elif self.var_known(inner_var):
                    columns[col] = text
                else:
                    raise _NotRecognized("insert", f"Undeclared variable {inner_var}")
            elif _is_scalar_literal(value):
                columns[col] = text
            elif (
                isinstance(value, exp.Anonymous)
                and value.name.upper().startswith("ERROR_")
                and not value.expressions
            ):
                columns[col] = text
            else:
                raise _NotRecognized(
                    "insert", f"Log value {text!r} is not a variable, literal or ERROR_*() call"
                )
        return PostLoadAction(
            kind="job_log",
            on=on,
            target_table=f"{parts[0]}.{parts[1]}",
            columns=columns,  # type: ignore[arg-type]
            original_snippet=doc.node_text(node),
        )

    def catch_block(self, node: _Block) -> None:
        doc = self.doc
        children = node.children
        if (
            not children
            or not isinstance(children[-1], _Leaf)
            or doc.up(children[-1].lo) != "THROW"
            or doc.strip_semicolon(children[-1].lo, children[-1].hi) != children[-1].lo + 1
        ):
            raise _NotRecognized(
                "error_handling", "BEGIN CATCH is only recognised as job_log INSERT(s) followed by THROW;"
            )
        body_text = doc.text(children[0].lo, children[-1].hi)
        actions: list[PostLoadAction] = []
        for child in children[:-1]:
            tree = _parse_sql(doc.node_text(child)) if isinstance(child, _Leaf) else None
            if not isinstance(child, _Leaf) or not isinstance(tree, exp.Insert):
                raise _NotRecognized(
                    "error_handling", "BEGIN CATCH contains a statement other than a job_log INSERT"
                )
            action = self.job_log(child, tree, on="failure")
            actions.append(action.model_copy(update={"original_snippet": body_text}))
        self.post_actions.extend(actions)
        tables = ", ".join(sorted({a.target_table for a in actions})) or "no log table"
        self.add_rule(
            f"On failure (BEGIN CATCH) the error is logged to {tables} and then re-raised with THROW; "
            "the job must fail after writing the failure log row."
        )

    # ------------------------------------------------------------------ IF / WHILE
    def conditional(self, node: _Cond) -> None:  # noqa: C901 - one linear pattern check
        doc = self.doc
        cond_text = doc.text(node.cond_lo, node.cond_hi)
        if node.keyword == "WHILE":
            if any(doc.toks[j].text.upper() == "@@FETCH_STATUS" for j in range(node.cond_lo, node.cond_hi)):
                raise _NotRecognized("cursor", "WHILE @@FETCH_STATUS cursor loop; row-by-row processing")
            raise _NotRecognized("while_loop", "WHILE loops have no IR equivalent")
        cond = tsql_expr.parse_condition(cond_text)
        compute = self.wm_compute
        read = self.wm_read
        if (
            compute is None
            or read is None
            or node.otherwise is not None
            or not (
                isinstance(cond, exp.Not)
                and isinstance(cond.this, exp.Is)
                and isinstance(cond.this.expression, exp.Null)
            )
            or (tsql_expr.variable_name(cond.this.this) or "").lower() != compute["var"].lower()
            or not isinstance(node.then, _Block)
            or node.then.kind != "begin"
        ):
            raise _NotRecognized(
                "conditional_logic", "IF block outside the recognised watermark-advance pattern"
            )
        children = node.then.children
        if not children or not isinstance(children[0], _Leaf) or len(children) > 2:
            raise _NotRecognized("conditional_logic", "Watermark IF block has an unexpected body")
        update = _parse_sql(doc.node_text(children[0]))
        new_var, job_var = compute["var"].lower(), read["job_var"].lower()
        store = (read["schema"].lower(), read["table"].lower())

        def is_store(table: exp.Expression | None) -> bool:
            parts = _table_parts(table)
            return parts is not None and ((parts[0] or "").lower(), parts[1].lower()) == store

        ok = (
            isinstance(update, exp.Update)
            and is_store(update.this)
            and len(update.expressions) == 1
            and isinstance(update.expressions[0], exp.EQ)
            and update.expressions[0].this.name.lower() == read["column"].lower()
            and (tsql_expr.variable_name(update.expressions[0].expression) or "").lower() == new_var
            and isinstance(update.args.get("where"), exp.Where)
            and isinstance(update.args["where"].this, exp.EQ)
            and update.args["where"].this.this.name.lower() == read["key"].lower()
            and (tsql_expr.variable_name(update.args["where"].this.expression) or "").lower() == job_var
        )
        upsert = False
        if ok and len(children) == 2:
            second = children[1]
            ok = False
            if (
                isinstance(second, _Cond)
                and second.keyword == "IF"
                and second.otherwise is None
                and isinstance(second.then, _Leaf)
            ):
                rc = tsql_expr.parse_condition(doc.text(second.cond_lo, second.cond_hi))
                insert = _parse_sql(doc.node_text(second.then))
                if (
                    isinstance(rc, exp.EQ)
                    and (tsql_expr.variable_name(rc.this) or "").upper() == "@@ROWCOUNT"
                    and _is_number(rc.expression, "0")
                    and isinstance(insert, exp.Insert)
                    and isinstance(insert.this, exp.Schema)
                    and is_store(insert.this.this)
                    and isinstance(insert.expression, exp.Values)
                    and len(insert.expression.expressions) == 1
                ):
                    cols = [c.name.lower() for c in insert.this.expressions]
                    vals = [
                        (tsql_expr.variable_name(v) or "").lower()
                        for v in insert.expression.expressions[0].expressions
                    ]
                    ok = (
                        dict(zip(cols, vals, strict=False))
                        == {read["key"].lower(): job_var, read["column"].lower(): new_var}
                        and len(cols) == 2
                    )
                    upsert = ok
        if not ok:
            raise _NotRecognized(
                "conditional_logic", "IF block outside the recognised watermark-advance pattern"
            )
        self.wm_advance = True
        self.wm_snippets.append(doc.node_text(node))
        self.add_rule(
            f"Watermark row for '{read['job_name']}' in {read['store']} is updated"
            + (", or inserted if missing (@@ROWCOUNT = 0)," if upsert else "")
            + f" only when rows were processed ({compute['var']} IS NOT NULL) (T19)."
        )

    # ------------------------------------------------------------------ finalize
    def finalize(self) -> tuple[list[Target], list[RunParameter], list[DataQualityRule]]:
        watermark: WatermarkSpec | None = None
        pieces = {
            "read": self.wm_read,
            "initial value": self.wm_init,
            "compute": self.wm_compute,
            "advance": self.wm_advance or None,
        }
        if any(v is not None for v in pieces.values()):
            missing = [k for k, v in pieces.items() if v is None]
            if missing or self.pending_merge is None:
                self.add_unsupported(
                    "watermark_logic",
                    "\n".join(self.wm_snippets),
                    f"Incomplete watermark pattern (missing: {', '.join(missing) or 'MERGE target'}).",
                )
            else:
                assert self.wm_read is not None and self.wm_compute is not None and self.wm_init is not None
                watermark = WatermarkSpec(
                    store_table=self.wm_read["store"],
                    job_name=self.wm_read["job_name"],
                    column=self.wm_compute["column"],
                    initial_value=self.wm_init,
                    computed_from=self.wm_compute["step"],
                    advance_only_if_rows=True,
                )
        if self.pending_merge is not None:
            m = self.pending_merge
            strategy = LoadStrategy(
                mode=LoadMode.MERGE_UPSERT,
                business_keys=m["keys"],  # type: ignore[arg-type]
                update_condition=m["condition"],  # type: ignore[arg-type]
                insert_only_columns=m["insert_only"],
                update_only_columns=m["update_only"],  # type: ignore[arg-type]
                watermark_column=None if watermark is None else watermark.column,
                watermark=watermark,
            )
            self.targets.insert(
                0, Target(input=str(m["input"]), dataset=m["dataset"], load_strategy=strategy)
            )  # type: ignore[arg-type]

        for var in self.vars.values():
            if var.is_table and var.name.lower() not in self.merge_out_vars:
                self.add_unsupported(
                    "table_variable",
                    var.snippet,
                    f"Table variable {var.name} is not a recognised MERGE OUTPUT target",
                )
        for param in self.header.params:
            if not self.is_run_ts(param.name):
                self.add_unsupported(
                    "procedure_parameter",
                    param.snippet,
                    "IR v1.1 RunParameter covers only run_ts and watermark arguments",
                )
        if self.header.options:
            self.add_unsupported(
                "procedure_option", self.header.options, "Procedure options have no IR equivalent"
            )

        parameters: list[RunParameter] = []
        refs = set(self.run_ts_vars) | self.runtime_spellings
        if refs:
            parameters.append(
                RunParameter(name="run_ts", kind=RunParameterKind.RUN_TS, legacy_refs=sorted(refs))
            )
        if watermark is not None and self.wm_read is not None:
            column = self.catalog.column(
                self.wm_read["schema"], self.wm_read["table"], self.wm_read["column"]
            )
            parameters.append(
                RunParameter(
                    name=self.wm_read["column"],
                    kind=RunParameterKind.WATERMARK,
                    data_type=DataType.TIMESTAMP if column is None else column.data_type,
                    legacy_refs=[self.wm_read["var"]],
                )
            )
        dq: list[DataQualityRule] = []
        for target in self.targets:
            for column in target.dataset.columns:
                if column.is_key:
                    dq.append(
                        DataQualityRule(
                            dataset_alias=target.dataset.alias,
                            column=column.name,
                            rule_type=DQRuleType.UNIQUE,
                            severity=Severity.ERROR,
                            derived_from="PRIMARY KEY",
                        )
                    )
        return self.targets, parameters, dq


def doc_var_spelling(doc: _Doc, node: _Leaf, name: str) -> str:
    """Spelling of a variable as written in the statement (first match), e.g. ``@RunTs``."""
    for i in range(node.lo, node.hi):
        tok = doc.toks[i]
        if tok.kind == "var" and tok.text.lower() == name.lower():
            return tok.text
    return name


def _count_of_action(value: exp.Expression) -> str | None:
    """``'INSERT'`` for ``SUM(CASE WHEN col = 'INSERT' THEN 1 ELSE 0 END)``, else None."""
    if not isinstance(value, exp.Sum) or not isinstance(value.this, exp.Case):
        return None
    case = value.this
    ifs = case.args.get("ifs") or []
    if len(ifs) != 1 or not _is_number(case.args.get("default"), "0") or case.this is not None:
        return None
    cond = ifs[0].this
    if (
        isinstance(cond, exp.EQ)
        and isinstance(cond.this, exp.Column)
        and isinstance(cond.expression, exp.Literal)
        and cond.expression.is_string
        and _is_number(ifs[0].args.get("true"), "1")
    ):
        return str(cond.expression.this).upper()
    return None


def _resolver(types: dict[str, dict[str, DataType | None]]) -> tsql_expr.ColumnTypeResolver:
    """Column type resolver over ``{alias: {column: type}}`` (lower-case keys)."""
    frozen = {a: dict(cols) for a, cols in types.items()}

    def resolve(qualifier: str | None, name: str) -> DataType | None:
        if qualifier:
            return frozen.get(qualifier.lower(), {}).get(name.lower())
        found = {cols[name.lower()] for cols in frozen.values() if name.lower() in cols}
        return found.pop() if len(found) == 1 else None

    return resolve


# ---------------------------------------------------------------------------
# Trigger (minimal shell, T21/T22)
# ---------------------------------------------------------------------------
class _TriggerBuilder(_Builder):
    """Build the minimal-shell ``MigrationUnit`` for a DML trigger."""

    def build(self, nodes: list[_Node]) -> tuple[list[DatasetRef], list[Target]]:  # noqa: C901
        doc, header = self.doc, self.header
        trig_schema, trig_table = header.trigger_schema or "dbo", header.trigger_table or ""
        targets: list[Target] = []
        mutations: list[UnsupportedConstruct] = []
        audited: list[str] = []
        src_alias = f"src_{slug(trig_table)}"
        prev_end = doc.toks[header.body_lo].start
        for node in nodes:
            start = doc.toks[node.lo].start
            comments = self.rule_comments(prev_end, start)
            prev_end = doc.toks[node.hi - 1].end
            text = doc.node_text(node)
            try:
                if isinstance(node, _Cond):
                    words = [doc.toks[j] for j in range(node.cond_lo, node.cond_hi)]
                    if (
                        node.keyword == "IF"
                        and node.otherwise is None
                        and len(words) == 5
                        and words[0].up == "NOT"
                        and words[1].up == "UPDATE"
                        and doc.is_punct(node.cond_lo + 2, "(")
                        and doc.is_punct(node.cond_lo + 4, ")")
                        and isinstance(node.then, _Leaf)
                        and doc.up(node.then.lo) == "RETURN"
                        and doc.strip_semicolon(node.then.lo, node.then.hi) == node.then.lo + 1
                    ):
                        audited.append(words[3].name)
                        logger.debug("Trigger guard recognised (covered by the trigger construct): %s", text)
                        continue
                    raise _NotRecognized("conditional_logic", "IF block inside the trigger not recognised")
                if isinstance(node, _Block):
                    raise _NotRecognized(
                        "unrecognized_statement", "Nested block inside the trigger not recognised"
                    )
                body_hi = doc.strip_semicolon(node.lo, node.hi)
                first = doc.up(node.lo)
                if first == "SET" and doc.up(node.lo + 1) == "NOCOUNT" and body_hi == node.lo + 3:
                    logger.debug("Ignored: %s", text)
                    continue
                tree = _parse_sql(text)
                if (
                    first == "INSERT"
                    and isinstance(tree, exp.Insert)
                    and isinstance(tree.expression, exp.Select)
                ):
                    targets.append(self.audit_insert(node, tree, src_alias, comments, audited))
                    continue
                if first == "UPDATE" and isinstance(tree, exp.Update):
                    mutated = self.updated_table(tree)
                    if mutated == (trig_schema.lower(), trig_table.lower()):
                        set_cols = [e.this.name for e in tree.expressions if isinstance(e, exp.EQ)]
                        for br, br_text in comments:
                            self.add_rule(
                                f"OLTP-side (not migrated): {br}: {br_text} This mutates the source row "
                                f"({trig_schema}.{trig_table}.{', '.join(set_cols)}) and stays in the OLTP "
                                "system."
                            )
                        if not comments:
                            self.add_rule(
                                f"OLTP-side (not migrated): the trigger updates {trig_schema}.{trig_table}."
                                f"{', '.join(set_cols)} on the source row; this stays in the OLTP system."
                            )
                        label = comments[0][0] if comments else "The trigger"
                        mutations.append(
                            UnsupportedConstruct(
                                kind="source_row_mutation",
                                original_snippet=text,
                                reason=f"{label} updates the OLTP source row inside the trigger.",
                                suggested_pattern=(
                                    f"Keep {label} in the OLTP system (application logic or the existing "
                                    f"trigger); the warehouse only reads {', '.join(set_cols)}."
                                ),
                            )
                        )
                        continue
                raise _NotRecognized("unrecognized_statement", "Statement inside the trigger not recognised")
            except _NotRecognized as err:
                self.add_unsupported(err.kind, text, err.reason)
            except TsqlSyntaxError as err:
                self.add_unsupported("unrecognized_statement", text, str(err))
        self.scan_runtime_spellings(header.body_lo, header.body_hi)
        self.add_rule(
            f"IR wiring for this unit ({src_alias} -> "
            + (", ".join(t.dataset.alias for t in targets) or "no target")
            + ", no steps) is a placeholder required by the IR schema; codegen is skipped (manual path)."
        )
        columns = self.catalog.table(trig_schema, trig_table)
        if columns is None:
            raise ValueError(f"Trigger table {trig_schema}.{trig_table} is not in the catalog")
        source = DatasetRef(
            alias=src_alias,
            system=self.system_of(trig_schema),
            schema_name=trig_schema,
            table=trig_table,
            columns=columns,
        )
        trigger_text = doc.text(header.create_lo, header.body_hi)
        events = ", ".join(header.trigger_events)
        target_desc = (
            ", ".join(f"{t.dataset.schema_name}.{t.dataset.table}" for t in targets) or "the audit table"
        )
        cols_desc = ", ".join(c.name for t in targets for c in t.dataset.columns) or "the audit columns"
        audited_desc = ", ".join(audited) or "the audited columns"
        suggested = (
            f"Do not emulate the trigger in Spark. Enable Delta Change Data Feed on the migrated "
            f"{trig_table} table (or SQL Server CDC on {trig_schema}.{trig_table} at the source). "
            "A scheduled audit job reads the update_preimage/update_postimage pairs per key where "
            f"{audited_desc} changed and appends ({cols_desc}) to {target_desc}; changed_at comes from the "
            "change commit timestamp "
            f"(_commit_timestamp / CDC commit time); changed_by comes from the CDC/application user, since "
            "SUSER_SNAME() has no Spark equivalent."
        )
        self.unsupported.insert(
            0,
            UnsupportedConstruct(
                kind="trigger",
                original_snippet=trigger_text,
                reason=(
                    f"Spark has no row-level triggers ({header.trigger_timing} {events} on "
                    f"{trig_schema}.{trig_table})."
                ),
                suggested_pattern=suggested,
            ),
        )
        self.unsupported[1:1] = mutations
        return [source], [
            Target(input=src_alias, dataset=t.dataset, load_strategy=t.load_strategy) for t in targets
        ]

    @staticmethod
    def updated_table(tree: exp.Update) -> tuple[str, str] | None:
        target = tree.this
        if not isinstance(target, exp.Table):
            return None
        alias = target.name.lower()
        candidates: list[exp.Expression] = []
        from_ = tree.args.get("from_") or tree.args.get("from")
        if isinstance(from_, exp.From):
            candidates.append(from_.this)
        candidates.extend(j.this for j in tree.args.get("joins") or [])
        if isinstance(from_, exp.From):
            candidates.extend(j.this for j in from_.args.get("joins") or [])
        for table in candidates:
            if isinstance(table, exp.Table) and _table_alias(table).lower() == alias and table.db:
                return table.db.lower(), table.name.lower()
        if target.db:
            return target.db.lower(), target.name.lower()
        return None

    def audit_insert(
        self,
        node: _Leaf,
        tree: exp.Insert,
        src_alias: str,
        comments: list[tuple[str, str]],
        audited: list[str],
    ) -> Target:
        doc = self.doc
        schema_node = tree.this
        parts = _table_parts(schema_node.this if isinstance(schema_node, exp.Schema) else None)
        if parts is None or parts[2] or not parts[0] or not isinstance(schema_node, exp.Schema):
            raise _NotRecognized(
                "insert", "Trigger INSERT must target a schema-qualified table with a column list"
            )
        schema, table, _ = parts
        cols = [c.name for c in schema_node.expressions]
        columns = self.catalog_columns(schema, table, cols)
        hi = doc.strip_semicolon(node.lo, node.hi)
        select_idx = next(j for j in doc.depth0(node.lo, hi) if doc.up(j) == "SELECT")
        from_idx = doc.find0(select_idx, hi, "FROM")
        where_idx = doc.find0(select_idx, hi, "WHERE")
        values = [doc.text(a, b) for a, b in doc.split_commas(select_idx + 1, from_idx or hi)]
        dataset = DatasetRef(
            alias=f"tgt_{slug(table)}",
            system=self.system_of(schema),
            schema_name=schema,
            table=table,
            columns=columns,
        )
        details = (
            f"audited column(s): {', '.join(audited) or 'all'}; {schema}.{table} ({', '.join(cols)}) = "
            f"({', '.join(values)})"
        )
        if where_idx is not None:
            details += (
                f", only where {_collapse_ws(doc.text(where_idx + 1, hi))} (string comparison is "
                "case-insensitive under the SQL Server default collation, X2)"
            )
        for br, text in comments:
            self.add_rule(f"{br}: {text} ({details}).")
        if not comments:
            self.add_rule(f"Trigger audit insert: {details}.")
        return Target(input=src_alias, dataset=dataset, load_strategy=LoadStrategy(mode=LoadMode.APPEND))


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------
class TsqlParser:
    """Parse a T-SQL stored procedure or trigger script into a ``MigrationUnit``.

    Args:
        catalog: Table catalog (from the DDL) for column types, nullability and keys. T-SQL code carries no
            column metadata of its own, so the catalog is required.
    """

    technology = SourceTechnology.TSQL

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    @staticmethod
    def _display_path(path: Path) -> str:
        if not path.is_absolute():
            return path.as_posix()
        try:
            return path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return path.as_posix()

    def parse(self, path: Path) -> MigrationUnit:
        """Parse one ``.sql`` file holding one ``CREATE PROCEDURE`` or ``CREATE TRIGGER``.

        Args:
            path: Path to the T-SQL script.

        Returns:
            The validated ``MigrationUnit``.

        Raises:
            TsqlSyntaxError: If the script cannot be tokenized or its blocks are unbalanced.
            ValueError: If the script has no procedure/trigger, or no source or target can be built.
        """
        doc = _Doc(path.read_text(encoding="utf-8"))
        header: _Header | None = None
        extra: list[tuple[int, int]] = []
        for lo, hi in _batches(doc):
            found = _parse_header(doc, lo, hi) if header is None else None
            if found is not None:
                found.body_hi = hi
                header = found
            else:
                extra.append((lo, hi))
        if header is None:
            raise ValueError(f"{path}: no CREATE PROCEDURE or CREATE TRIGGER found")

        nodes = _Splitter(doc, header.body_lo, header.body_hi).statements()
        if len(nodes) == 1 and isinstance(nodes[0], _Block) and nodes[0].kind == "begin":
            nodes = nodes[0].children
        object_name = f"{header.schema}.{header.name}" if header.schema else header.name
        steps: list[Step] = []
        references: list[DatasetRef] = []
        post_actions: list[PostLoadAction] = []
        dq: list[DataQualityRule] = []
        parameters: list[RunParameter] = []
        builder: _Builder
        if header.kind == "trigger":
            builder = _TriggerBuilder(doc, header, self.catalog)
            try:
                sources, targets = builder.build(nodes)
            except _NotRecognized as err:
                raise ValueError(f"{path}: {err.reason}") from None
            if builder.runtime_spellings:
                parameters.append(
                    RunParameter(
                        name="run_ts",
                        kind=RunParameterKind.RUN_TS,
                        legacy_refs=sorted(builder.runtime_spellings),
                    )
                )
        else:
            builder = _ProcBuilder(doc, header, self.catalog)
            builder.build(nodes)
            builder.scan_runtime_spellings(header.body_lo, header.body_hi)
            targets, parameters, dq = builder.finalize()
            sources, references, steps, post_actions = (
                builder.sources,
                builder.references,
                builder.steps,
                builder.post_actions,
            )
        for lo, hi in extra:
            builder.add_unsupported(
                "batch_statement", doc.text(lo, hi), "Statement outside the procedure/trigger batch"
            )

        if not targets:
            kinds = [u.kind for u in builder.unsupported]
            raise ValueError(f"{path}: no target could be built; unsupported constructs: {kinds}")
        if not sources:
            kinds = [u.kind for u in builder.unsupported]
            raise ValueError(f"{path}: no source could be built; unsupported constructs: {kinds}")

        return MigrationUnit(
            unit_id=f"tsql.{header.name}",
            name=header.name,
            source_artifact=SourceArtifact(
                technology=SourceTechnology.TSQL,
                file_path=self._display_path(path),
                object_name=object_name,
                sha256=base.sha256_file(path),
                parser_version=base.PARSER_VERSION,
            ),
            sources=sources,
            references=references,
            steps=steps,
            targets=targets,
            dq_rules=dq,
            business_rules=builder.rules,
            unsupported_constructs=builder.unsupported,
            complexity=None,
            parameters=parameters,
            post_load_actions=post_actions,
        )
