"""Deterministic rules for Informatica PowerCenter expressions (Expression, Aggregator, Router, filters).

A small tokenizer and recursive-descent parser build an AST for the PowerCenter expression language. Only a
narrow subset is translated to Spark SQL (column refs, string/number literals, comparisons, ``AND``/``OR``/
``NOT``, one-argument ``ISNULL``/``UPPER``/``LTRIM``/``RTRIM``). A rule applies only when it covers the whole
expression; anything else stays ``pending`` with ``spark_sql=None`` for the translator agent.

Grammar, lowest precedence first::

    or      := and (OR and)*
    and     := not (AND not)*
    not     := NOT not | cmp
    cmp     := add (('=' | '<>' | '!=' | '^=' | '<' | '>' | '<=' | '>=') add)*
    add     := mul (('+' | '-' | '||') mul)*          ('||' is string concatenation, never OR)
    mul     := unary (('*' | '/' | '%') unary)*
    unary   := ('-' | '+' | NOT) unary | primary
    primary := STRING | NUMBER | TRUE | FALSE | NULL | :LKP.NAME(args) | NAME(args) | $$VAR
             | IDENT | TABLE.IDENT | '(' or ')'
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from migrateiq.ir.models import DataType, Expression, ExpressionDialect, TranslationStatus
from migrateiq.parsers.base import run_ts_note

logger = logging.getLogger(__name__)

PARSE_FAILURE_NOTE = "Could not parse Informatica expression; left for the translator."
SESSION_TIME_NOTE = "Session start time ({name}); codegen pins it to the run_ts job argument."
DECODE_NOTE = (
    "DECODE(TRUE, c1, v1, c2, v2, ..., d) = CASE WHEN c1 THEN v1 WHEN c2 THEN v2 ... ELSE d END "
    "(first matching condition wins) (T12)."
)
CONCAT_NOTE = (
    "Informatica treats NULL as an empty string in || concatenation; Spark concat() returns NULL (T14)."
)
IN_NORMALIZED_NOTE = (
    "IN() tests the raw {col} value while another output port normalizes it with UPPER/TRIM; "
    "non-normalized values (e.g. 'closed', ' CLOSED') do not match (X1)."
)
LOOKUP_NOTE = "{call} is provided by step {step} as column {column}."
LOOKUP_UNRESOLVED_NOTE = "{call} is an unconnected lookup call with no providing step."
BOOLEAN_NOTE = "Comparison of a bit/boolean column with a number; Spark boolean semantics differ."

#: Spellings of the session start time (resolved to the pinned run_ts).
RUNTIME_NAMES: frozenset[str] = frozenset({"SESSSTARTTIME", "SYSDATE"})
AGGREGATE_FUNCTIONS: frozenset[str] = frozenset({"SUM", "COUNT", "MIN", "MAX", "AVG", "FIRST", "LAST"})
NORMALIZING_FUNCTIONS: frozenset[str] = frozenset({"UPPER", "LTRIM", "RTRIM"})

_RUNTIME_TEXT = re.compile(r"\b(SESSSTARTTIME|SYSDATE)\b", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"\d+(\.\d+)?\Z")
_KEYWORDS = frozenset({"AND", "OR", "NOT", "TRUE", "FALSE", "NULL"})


class InformaticaExprError(ValueError):
    """Raised when an Informatica expression cannot be tokenized or parsed."""


class _NotCovered(Exception):  # noqa: N818 - internal control flow, never escapes this module
    """The expression uses something outside the deterministic rule set."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    """Base class of Informatica expression AST nodes."""


@dataclass(frozen=True)
class StringLit(Node):
    """A string literal; ``value`` is unescaped (``''`` -> ``'``)."""

    value: str


@dataclass(frozen=True)
class NumberLit(Node):
    """A numeric literal, kept as written."""

    text: str


@dataclass(frozen=True)
class BoolLit(Node):
    """``TRUE`` / ``FALSE``."""

    value: bool


@dataclass(frozen=True)
class NullLit(Node):
    """``NULL``."""


@dataclass(frozen=True)
class ColumnRef(Node):
    """A port / column identifier; ``qualifier`` is the ``TABLE`` of ``TABLE.COL``."""

    name: str
    qualifier: str | None = None


@dataclass(frozen=True)
class Variable(Node):
    """A mapping parameter/variable such as ``$$RUN_DATE``."""

    name: str


@dataclass(frozen=True)
class RuntimeRef(Node):
    """``SESSSTARTTIME`` or ``SYSDATE``."""

    name: str


@dataclass(frozen=True)
class Call(Node):
    """``NAME(args)``; ``name`` as written. ``inner`` is the verbatim text between the parentheses."""

    name: str
    args: tuple[Node, ...]
    inner: str = field(default="", compare=False)


@dataclass(frozen=True)
class LookupCall(Node):
    """``:LKP.NAME(args)``. ``text`` is the verbatim call; ``arg_texts`` the verbatim arguments."""

    name: str
    args: tuple[Node, ...]
    text: str = field(default="", compare=False)
    arg_texts: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class Unary(Node):
    """``-x``, ``+x`` or ``NOT x``."""

    op: str
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    """``left op right`` (``op`` is upper-case for ``AND``/``OR``)."""

    op: str
    left: Node
    right: Node


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Tok:
    kind: str  # str, num, ident, qident, lkp, var, op, end
    text: str
    start: int
    end: int
    qualifier: str | None = None


_OPERATORS = ("||", "<>", "!=", "^=", "<=", ">=", "=", "<", ">", "+", "-", "*", "/", "%", "(", ")", ",")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_LKP = re.compile(r":LKP\.([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_VAR = re.compile(r"\$\$?[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"\d+(\.\d+)?")


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "'":
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise InformaticaExprError("unterminated string literal")
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(text[j])
                j += 1
            tokens.append(_Tok("str", "".join(buf), i, j))
            i = j
        elif ch.isdigit():
            m = _NUMBER.match(text, i)
            assert m is not None
            if m.end() < n and (text[m.end()].isalpha() or text[m.end()] == "_"):
                raise InformaticaExprError(f"unsupported numeric literal at {i}")
            tokens.append(_Tok("num", m.group(0), i, m.end()))
            i = m.end()
        elif ch == ":":
            m = _LKP.match(text, i)
            if not m:
                raise InformaticaExprError(f"unsupported ':' reference at {i}")
            tokens.append(_Tok("lkp", m.group(1), i, m.end()))
            i = m.end()
        elif ch == "$":
            m = _VAR.match(text, i)
            if not m:
                raise InformaticaExprError(f"unsupported variable reference at {i}")
            tokens.append(_Tok("var", m.group(0), i, m.end()))
            i = m.end()
        elif ch.isalpha() or ch == "_":
            q = _QUALIFIED.match(text, i)
            if q:
                tokens.append(_Tok("qident", q.group(2), i, q.end(), qualifier=q.group(1)))
                i = q.end()
                continue
            m = _IDENT.match(text, i)
            assert m is not None
            tokens.append(_Tok("ident", m.group(0), i, m.end()))
            i = m.end()
        else:
            for op in _OPERATORS:
                if text.startswith(op, i):
                    tokens.append(_Tok("op", op, i, i + len(op)))
                    i += len(op)
                    break
            else:
                raise InformaticaExprError(f"unexpected character {ch!r} at {i}")
    tokens.append(_Tok("end", "", n, n))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
_CMP = ("=", "<>", "!=", "^=", "<", ">", "<=", ">=")


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = _tokenize(text)
        self.pos = 0

    def peek(self, offset: int = 0) -> _Tok:
        return self.toks[min(self.pos + offset, len(self.toks) - 1)]

    def take(self) -> _Tok:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def is_op(self, op: str) -> bool:
        tok = self.peek()
        return tok.kind == "op" and tok.text == op

    def is_keyword(self, word: str) -> bool:
        tok = self.peek()
        return tok.kind == "ident" and tok.text.upper() == word

    def expect(self, op: str) -> _Tok:
        if not self.is_op(op):
            raise InformaticaExprError(f"expected {op!r}, got {self.peek().text!r}")
        return self.take()

    def parse(self) -> Node:
        node = self.or_expr()
        if self.peek().kind != "end":
            raise InformaticaExprError(f"unexpected trailing token {self.peek().text!r}")
        return node

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.is_keyword("OR"):
            self.take()
            node = Binary("OR", node, self.and_expr())
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while self.is_keyword("AND"):
            self.take()
            node = Binary("AND", node, self.not_expr())
        return node

    def not_expr(self) -> Node:
        if self.is_keyword("NOT"):
            self.take()
            return Unary("NOT", self.not_expr())
        return self.cmp_expr()

    def cmp_expr(self) -> Node:
        node = self.add_expr()
        while self.peek().kind == "op" and self.peek().text in _CMP:
            op = self.take().text
            node = Binary(op, node, self.add_expr())
        return node

    def add_expr(self) -> Node:
        node = self.mul_expr()
        while self.peek().kind == "op" and self.peek().text in ("+", "-", "||"):
            op = self.take().text
            node = Binary(op, node, self.mul_expr())
        return node

    def mul_expr(self) -> Node:
        node = self.unary()
        while self.peek().kind == "op" and self.peek().text in ("*", "/", "%"):
            op = self.take().text
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "op" and tok.text in ("-", "+"):
            self.take()
            return Unary(tok.text, self.unary())
        if self.is_keyword("NOT"):
            self.take()
            return Unary("NOT", self.unary())
        return self.primary()

    def arguments(self) -> tuple[list[Node], list[str], tuple[int, int]]:
        """Parse ``(args)``; return the nodes, their verbatim texts, and the span of all arguments."""
        self.expect("(")
        args: list[Node] = []
        texts: list[str] = []
        spans: list[tuple[int, int]] = []
        if not self.is_op(")"):
            while True:
                start = self.peek().start
                args.append(self.or_expr())
                spans.append((start, self.toks[self.pos - 1].end))
                texts.append(self.text[start : spans[-1][1]])
                if self.is_op(","):
                    self.take()
                    continue
                break
        self.expect(")")
        span = (spans[0][0], spans[-1][1]) if spans else (0, 0)
        return args, texts, span

    def primary(self) -> Node:
        tok = self.peek()
        if tok.kind == "str":
            self.take()
            return StringLit(tok.text)
        if tok.kind == "num":
            self.take()
            return NumberLit(tok.text)
        if tok.kind == "var":
            self.take()
            return Variable(tok.text)
        if tok.kind == "qident":
            self.take()
            return ColumnRef(tok.text, tok.qualifier)
        if tok.kind == "lkp":
            self.take()
            args, texts, _ = self.arguments()
            return LookupCall(
                tok.text, tuple(args), self.text[tok.start : self.toks[self.pos - 1].end], tuple(texts)
            )
        if tok.kind == "ident":
            upper = tok.text.upper()
            nxt = self.peek(1)
            if nxt.kind == "op" and nxt.text == "(" and upper not in _KEYWORDS:
                self.take()
                args, _, span = self.arguments()
                return Call(tok.text, tuple(args), self.text[span[0] : span[1]])
            if upper in ("AND", "OR", "NOT"):
                raise InformaticaExprError(f"unexpected keyword {tok.text!r}")
            self.take()
            if upper in ("TRUE", "FALSE"):
                return BoolLit(upper == "TRUE")
            if upper == "NULL":
                return NullLit()
            if upper in RUNTIME_NAMES:
                return RuntimeRef(upper)
            return ColumnRef(tok.text)
        if tok.kind == "op" and tok.text == "(":
            self.take()
            node = self.or_expr()
            self.expect(")")
            return node
        raise InformaticaExprError(f"unexpected token {tok.text!r}")


def parse_expression(text: str) -> Node:
    """Parse an Informatica expression into an AST.

    Args:
        text: PowerCenter expression text (entity-unescaped).

    Returns:
        The root AST node.

    Raises:
        InformaticaExprError: If the text cannot be tokenized or parsed.
    """
    return _Parser(text).parse()


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, (Call, LookupCall)):
        return node.args
    if isinstance(node, Unary):
        return (node.operand,)
    if isinstance(node, Binary):
        return (node.left, node.right)
    return ()


def _walk(node: Node) -> list[Node]:
    out = [node]
    for child in _children(node):
        out.extend(_walk(child))
    return out


def _is_decode_true(node: Node) -> bool:
    return (
        isinstance(node, Call)
        and node.name.upper() == "DECODE"
        and len(node.args) >= 3
        and isinstance(node.args[0], BoolLit)
        and node.args[0].value
    )


def _result_branches(node: Node) -> list[Node] | None:
    """Result branches of an IIF / DECODE(TRUE, ...) chain; None if a branch can be an implicit NULL."""
    if isinstance(node, Call) and node.name.upper() == "IIF":
        if len(node.args) != 3:
            return None
        then = _result_branches(node.args[1])
        otherwise = _result_branches(node.args[2])
        return None if then is None or otherwise is None else then + otherwise
    if _is_decode_true(node):
        assert isinstance(node, Call)
        rest = node.args[1:]
        if len(rest) % 2 == 0:
            return None  # no default: unmatched rows get NULL
        branches: list[Node] = []
        for value in [rest[i] for i in range(1, len(rest), 2)] + [rest[-1]]:
            sub = _result_branches(value)
            if sub is None:
                return None
            branches.extend(sub)
        return branches
    return [node]


@dataclass(frozen=True)
class LookupUse:
    """One ``:LKP.NAME(args)`` call found in an expression.

    Attributes:
        name: Lookup instance name as written.
        key: Canonical key ``NAME(ARG1,ARG2)`` (upper-case, whitespace removed) used to match calls.
        text: Verbatim call text.
        arg_columns: Column name of each argument (lower-case, qualifier stripped), or None if the
            argument is not a plain column reference.
    """

    name: str
    key: str
    text: str
    arg_columns: tuple[str | None, ...]


def _lookup_key(call: LookupCall) -> str:
    args = ",".join("".join(t.split()).upper() for t in call.arg_texts)
    return f"{call.name.upper()}({args})"


def lookup_calls(original: str) -> list[LookupUse]:
    """Return the distinct ``:LKP.`` calls of an expression, in order of appearance.

    Args:
        original: Informatica expression text.

    Returns:
        One ``LookupUse`` per distinct call key. Empty if there are none or the text does not parse.
    """
    try:
        root = parse_expression(original)
    except InformaticaExprError:
        return []
    uses: list[LookupUse] = []
    seen: set[str] = set()
    for node in _walk(root):
        if isinstance(node, LookupCall):
            key = _lookup_key(node)
            if key in seen:
                continue
            seen.add(key)
            columns = tuple(a.name.lower() if isinstance(a, ColumnRef) else None for a in node.args)
            uses.append(LookupUse(node.name, key, node.text, columns))
    return uses


def runtime_spellings(original: str) -> list[str]:
    """Return the runtime-value spellings (``SESSSTARTTIME``, ``SYSDATE``) used, in order of appearance.

    Args:
        original: Informatica expression text.

    Returns:
        Upper-case spellings. If the text does not parse, a text scan is used instead.
    """
    try:
        nodes = _walk(parse_expression(original))
    except InformaticaExprError:
        found = [m.group(1).upper() for m in _RUNTIME_TEXT.finditer(original)]
        return list(dict.fromkeys(found))
    return list(dict.fromkeys(n.name for n in nodes if isinstance(n, RuntimeRef)))


def normalized_columns(original: str) -> frozenset[str]:
    """Return the columns (lower-case) that appear inside an ``UPPER``/``LTRIM``/``RTRIM`` call.

    Args:
        original: Informatica expression text.

    Returns:
        The set of normalized column names; empty if the text does not parse.
    """
    try:
        root = parse_expression(original)
    except InformaticaExprError:
        return frozenset()
    found: set[str] = set()
    for node in _walk(root):
        if isinstance(node, Call) and node.name.upper() in NORMALIZING_FUNCTIONS:
            found.update(n.name.lower() for n in _walk(node) if isinstance(n, ColumnRef))
    return frozenset(found)


def string_literal_domain(original: str) -> list[str] | None:
    """Return the string results of an IIF / DECODE(TRUE, ...) chain whose every result is a string literal.

    Args:
        original: Informatica expression text.

    Returns:
        The distinct values in order of appearance, or None if the expression is not such a chain.
    """
    try:
        root = parse_expression(original)
    except InformaticaExprError:
        return None
    if not (isinstance(root, Call) and (root.name.upper() == "IIF" or _is_decode_true(root))):
        return None
    branches = _result_branches(root)
    if branches is None:
        return None
    values: list[str] = []
    for branch in branches:
        if not isinstance(branch, StringLit):
            return None
        if branch.value not in values:
            values.append(branch.value)
    return values


def aggregate_call(original: str) -> tuple[str, str] | None:
    """Return ``(FUNCTION, inner)`` if the whole expression is one aggregate call with one argument.

    Args:
        original: Informatica expression text, e.g. ``SUM(IIF(X = 'A', Y, 0))``.

    Returns:
        The function name upper-case and the verbatim text of its argument, or None.
    """
    try:
        root = parse_expression(original)
    except InformaticaExprError:
        return None
    if isinstance(root, Call) and root.name.upper() in AGGREGATE_FUNCTIONS and len(root.args) == 1:
        return root.name.upper(), root.inner
    return None


def is_bare_runtime_value(original: str) -> bool:
    """True if the whole expression is ``SESSSTARTTIME`` or ``SYSDATE``."""
    try:
        return isinstance(parse_expression(original), RuntimeRef)
    except InformaticaExprError:
        return False


# ---------------------------------------------------------------------------
# Spark emission (deterministic subset only)
# ---------------------------------------------------------------------------
_P_OR, _P_AND, _P_NOT, _P_CMP, _P_ATOM = 1, 2, 3, 4, 10
_CMP_SPARK = {"=": "=", "<>": "<>", "!=": "<>", "^=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
_FUNCS = {"UPPER": "upper", "LTRIM": "ltrim", "RTRIM": "rtrim"}


def _wrap(sql: str, needed: bool) -> str:
    return f"({sql})" if needed else sql


class _Emitter:
    def emit(self, node: Node) -> tuple[str, int]:
        if isinstance(node, ColumnRef):
            return node.name.lower(), _P_ATOM
        if isinstance(node, StringLit):
            if "\\" in node.value:
                raise _NotCovered  # Spark treats backslash as an escape; do not guess.
            return "'" + node.value.replace("'", "''") + "'", _P_ATOM
        if isinstance(node, NumberLit):
            if not _PLAIN_NUMBER.match(node.text):
                raise _NotCovered
            return node.text, _P_ATOM
        if isinstance(node, Call):
            name = node.name.upper()
            if len(node.args) != 1:
                raise _NotCovered
            arg_sql, arg_prec = self.emit(node.args[0])
            if name in _FUNCS:
                return f"{_FUNCS[name]}({arg_sql})", _P_ATOM
            if name == "ISNULL":
                return f"{_wrap(arg_sql, arg_prec < _P_ATOM)} IS NULL", _P_CMP
            raise _NotCovered
        if isinstance(node, Unary):
            if node.op != "NOT":
                raise _NotCovered
            sql, prec = self.emit(node.operand)
            return f"NOT {_wrap(sql, prec < _P_ATOM)}", _P_NOT
        if isinstance(node, Binary):
            if node.op in ("AND", "OR"):
                prec = _P_OR if node.op == "OR" else _P_AND
                left, lp = self.emit(node.left)
                right, rp = self.emit(node.right)
                return f"{_wrap(left, lp < prec)} {node.op} {_wrap(right, rp <= prec)}", prec
            if node.op in _CMP_SPARK:
                left, lp = self.emit(node.left)
                right, rp = self.emit(node.right)
                return (
                    f"{_wrap(left, lp <= _P_CMP)} {_CMP_SPARK[node.op]} {_wrap(right, rp <= _P_CMP)}",
                    _P_CMP,
                )
            raise _NotCovered
        raise _NotCovered


def _is_number(node: Node) -> bool:
    if isinstance(node, Unary) and node.op in ("-", "+"):
        return _is_number(node.operand)
    return isinstance(node, NumberLit)


def _boolean_vs_number(nodes: list[Node], column_types: Mapping[str, DataType]) -> bool:
    for node in nodes:
        if isinstance(node, Binary) and node.op in _CMP_SPARK:
            for col, other in ((node.left, node.right), (node.right, node.left)):
                if (
                    isinstance(col, ColumnRef)
                    and column_types.get(col.name.lower()) == DataType.BOOLEAN
                    and _is_number(other)
                ):
                    return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _pending(original: str, columns: list[str], notes: list[str]) -> Expression:
    return Expression(
        original=original,
        dialect=ExpressionDialect.INFORMATICA_EXPR,
        translation_status=TranslationStatus.PENDING,
        referenced_columns=columns,
        notes=" ".join(notes) if notes else None,
    )


def translate(
    original: str,
    column_types: Mapping[str, DataType] | None = None,
    *,
    normalized_columns: frozenset[str] = frozenset(),
    lookups: Mapping[str, str] | None = None,
) -> Expression:
    """Build an ``Expression`` (dialect ``informatica_expr``) for a PowerCenter expression.

    Args:
        original: Verbatim expression text (entity-unescaped, as lxml returns it).
        column_types: Known IR types by lower-case column name; used to flag boolean-vs-number comparisons.
        normalized_columns: Lower-case input columns that another output port normalizes with
            UPPER/LTRIM/RTRIM (X1).
        lookups: Providers of ``:LKP.`` calls, keyed by the canonical call key (see ``LookupUse.key``) or by
            the upper-case lookup name, with values ``"<step_id>.<column>"``.

    Returns:
        A ``deterministic`` expression when a rule covers the whole text, otherwise ``pending`` with
        ``spark_sql=None`` and explanatory notes.
    """
    types = column_types or {}
    providers = {k.upper(): v for k, v in (lookups or {}).items()}
    try:
        root = parse_expression(original)
    except InformaticaExprError as err:
        logger.debug("Informatica expression parse failed (%s): %r", err, original)
        notes = [PARSE_FAILURE_NOTE]
        if _RUNTIME_TEXT.search(original):
            notes.append(run_ts_note(original))
        return _pending(original, [], notes)

    nodes = _walk(root)
    columns = {n.name.lower() for n in nodes if isinstance(n, ColumnRef)}

    if isinstance(root, RuntimeRef):
        return Expression(
            original=original,
            dialect=ExpressionDialect.INFORMATICA_EXPR,
            spark_sql="current_timestamp()",
            translation_status=TranslationStatus.DETERMINISTIC,
            referenced_columns=[],
            notes=SESSION_TIME_NOTE.format(name=root.name),
        )

    notes: list[str] = []
    if any(_is_decode_true(n) for n in nodes):
        notes.append(DECODE_NOTE)
    if any(isinstance(n, Binary) and n.op == "||" for n in nodes):
        notes.append(CONCAT_NOTE)
    for node in nodes:
        if (
            isinstance(node, Call)
            and node.name.upper() == "IN"
            and node.args
            and isinstance(node.args[0], ColumnRef)
            and node.args[0].name.lower() in normalized_columns
        ):
            note = IN_NORMALIZED_NOTE.format(col=node.args[0].name.lower())
            if note not in notes:
                notes.append(note)
    seen_calls: set[str] = set()
    for node in nodes:
        if isinstance(node, LookupCall):
            key = _lookup_key(node)
            if key in seen_calls:
                continue
            seen_calls.add(key)
            provider = providers.get(key) or providers.get(node.name.upper())
            if provider is None or "." not in provider:
                notes.append(LOOKUP_UNRESOLVED_NOTE.format(call=node.text))
                continue
            step, column = provider.split(".", 1)
            columns.add(column)
            notes.append(LOOKUP_NOTE.format(call=node.text, step=step, column=column))
    boolean_issue = _boolean_vs_number(nodes, types)
    if boolean_issue:
        notes.append(BOOLEAN_NOTE)
    runtime = any(isinstance(n, RuntimeRef) for n in nodes)
    if runtime:
        notes.append(run_ts_note(original))

    referenced = sorted(columns)
    if notes or runtime or boolean_issue:
        return _pending(original, referenced, notes)
    try:
        spark_sql, _ = _Emitter().emit(root)
    except _NotCovered:
        return _pending(original, referenced, notes)
    return Expression(
        original=original,
        dialect=ExpressionDialect.INFORMATICA_EXPR,
        spark_sql=spark_sql,
        translation_status=TranslationStatus.DETERMINISTIC,
        referenced_columns=referenced,
    )
