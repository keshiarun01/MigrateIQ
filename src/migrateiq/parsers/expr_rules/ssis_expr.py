"""Deterministic rules for SSIS expressions (Derived Column, Conditional Split).

A small tokenizer and recursive-descent parser build an AST for the SSIS expression language. Only a
narrow subset is translated to Spark SQL (column refs, string/number literals, comparisons, ``TRIM``,
``UPPER``, one-argument ``ISNULL``, and ``&& || !``). A rule applies only when it covers the whole
expression; anything else stays ``pending`` with ``spark_sql=None`` for the translator agent.

Grammar, lowest precedence first::

    ternary   := or ('?' ternary ':' ternary)?          (right-associative)
    or        := and ('||' and)*
    and       := equality ('&&' equality)*
    equality  := relational (('==' | '!=') relational)*
    relational:= additive (('<' | '>' | '<=' | '>=') additive)*
    additive  := multiplicative (('+' | '-') multiplicative)*
    multiplicative := unary (('*' | '/' | '%') unary)*
    unary     := ('!' | '-') unary | primary
    primary   := STRING | NUMBER | NULL(DT_X[,n...]) | (DT_X[,n...]) unary | NAME(args)
               | @[Ns::Var] | IDENT | '(' ternary ')'
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from migrateiq.ir.models import Expression, ExpressionDialect, TranslationStatus
from migrateiq.parsers.base import run_ts_note

logger = logging.getLogger(__name__)

PARSE_FAILURE_NOTE = "Could not parse SSIS expression; left for the translator."
GETDATE_NOTE = "Non-deterministic; excluded from reconciliation checksums."
PACKAGE_TIME_NOTE = "Package start time ({var}); codegen pins it to the run_ts job argument."

#: Package variables that hold the package start time (resolved to the pinned run_ts).
RUNTIME_VARIABLES: frozenset[str] = frozenset({"User::RunStartTime", "System::StartTime"})

_RUNTIME_FUNCTIONS = frozenset({"GETDATE"})
_LINEAGE_REF = re.compile(r"#\{[^}]*?\.Columns\[([^\]]+)\]\}")
_SIMPLE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PLAIN_NUMBER = re.compile(r"\d+(\.\d+)?\Z")
# Text-level fallback for expressions the parser cannot read (string literals are not excluded, so this
# can over-report; it is only used to add the run_ts note and legacy_refs, never to translate).
_RUNTIME_TEXT = re.compile(r"\b(GETDATE)\s*\(\s*\)|@\[\s*([A-Za-z_]\w*::[A-Za-z_]\w*)\s*\]", re.IGNORECASE)


def _runtime_spellings_in_text(original: str, runtime_variables: frozenset[str]) -> list[str]:
    """Runtime spellings found by a text scan, in order of appearance (fallback for unparseable text)."""
    spellings: list[str] = []
    for match in _RUNTIME_TEXT.finditer(original):
        if match.group(1):
            text = f"{match.group(1).upper()}()"
        elif match.group(2) in runtime_variables:
            text = f"@[{match.group(2)}]"
        else:
            continue
        if text not in spellings:
            spellings.append(text)
    return spellings


class SsisExprError(ValueError):
    """Raised when an SSIS expression cannot be tokenized or parsed."""


class _NotCovered(Exception):  # noqa: N818 - internal control flow, never escapes this module
    """The expression uses something outside the deterministic rule set."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    """Base class of SSIS expression AST nodes."""


@dataclass(frozen=True)
class StringLit(Node):
    """A string literal; ``value`` is unescaped."""

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
class TypedNull(Node):
    """``NULL(DT_X[,n...])``."""

    type_name: str
    params: tuple[str, ...]


@dataclass(frozen=True)
class Cast(Node):
    """``(DT_X[,n...])expr``."""

    type_name: str
    params: tuple[str, ...]
    operand: Node


@dataclass(frozen=True)
class Call(Node):
    """``NAME(args)``; ``name`` as written."""

    name: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Variable(Node):
    """``@[Ns::Var]``; ``name`` is ``Ns::Var``."""

    name: str


@dataclass(frozen=True)
class ColumnRef(Node):
    """A column identifier; ``bracketed`` if written as ``[name]``."""

    name: str
    bracketed: bool = False


@dataclass(frozen=True)
class Unary(Node):
    """``!x`` or ``-x``."""

    op: str
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    """``left op right``."""

    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Ternary(Node):
    """``cond ? then : else``."""

    cond: Node
    then: Node
    otherwise: Node


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Tok:
    kind: str  # str, num, ident, var, bracket, op, end
    text: str


_OPERATORS = (
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "<",
    ">",
    "!",
    "+",
    "-",
    "*",
    "/",
    "%",
    "?",
    ":",
    "(",
    ")",
    ",",
)
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == '"':
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise SsisExprError("unterminated string literal")
                c = text[i]
                if c == "\\":
                    if i + 1 >= n or text[i + 1] not in _ESCAPES:
                        raise SsisExprError(f"unsupported escape at {i}")
                    buf.append(_ESCAPES[text[i + 1]])
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            tokens.append(_Tok("str", "".join(buf)))
        elif ch.isdigit():
            m = re.match(r"\d+(\.\d+)?", text[i:])
            assert m is not None
            end = i + m.end()
            if end < n and (text[end].isalpha() or text[end] == "_"):
                raise SsisExprError(f"unsupported numeric literal at {i}")
            tokens.append(_Tok("num", m.group(0)))
            i = end
        elif ch == "@":
            m = re.match(r"@\[([A-Za-z_]\w*::[A-Za-z_]\w*)\]", text[i:])
            if not m:
                raise SsisExprError(f"unsupported variable reference at {i}")
            tokens.append(_Tok("var", m.group(1)))
            i += m.end()
        elif ch == "[":
            end = text.find("]", i)
            if end < 0:
                raise SsisExprError("unterminated [identifier]")
            tokens.append(_Tok("bracket", text[i + 1 : end]))
            i = end + 1
        elif ch.isalpha() or ch == "_":
            m = re.match(r"[A-Za-z_]\w*", text[i:])
            assert m is not None
            tokens.append(_Tok("ident", m.group(0)))
            i += m.end()
        else:
            for op in _OPERATORS:
                if text.startswith(op, i):
                    tokens.append(_Tok("op", op))
                    i += len(op)
                    break
            else:
                raise SsisExprError(f"unexpected character {ch!r} at {i}")
    tokens.append(_Tok("end", ""))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = _tokenize(text)
        self.pos = 0

    def peek(self, offset: int = 0) -> _Tok:
        return self.toks[min(self.pos + offset, len(self.toks) - 1)]

    def take(self) -> _Tok:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def accept(self, op: str) -> bool:
        tok = self.peek()
        if tok.kind == "op" and tok.text == op:
            self.pos += 1
            return True
        return False

    def expect(self, op: str) -> None:
        if not self.accept(op):
            raise SsisExprError(f"expected {op!r}, got {self.peek().text!r}")

    def parse(self) -> Node:
        node = self.ternary()
        if self.peek().kind != "end":
            raise SsisExprError(f"unexpected trailing token {self.peek().text!r}")
        return node

    def ternary(self) -> Node:
        cond = self.binary(0)
        if self.accept("?"):
            then = self.ternary()
            self.expect(":")
            otherwise = self.ternary()
            return Ternary(cond, then, otherwise)
        return cond

    _LEVELS: tuple[tuple[str, ...], ...] = (
        ("||",),
        ("&&",),
        ("==", "!="),
        ("<", ">", "<=", ">="),
        ("+", "-"),
        ("*", "/", "%"),
    )

    def binary(self, level: int) -> Node:
        if level == len(self._LEVELS):
            return self.unary()
        node = self.binary(level + 1)
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.text in self._LEVELS[level]:
                self.pos += 1
                node = Binary(tok.text, node, self.binary(level + 1))
            else:
                return node

    def unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "op" and tok.text in ("!", "-"):
            self.pos += 1
            return Unary(tok.text, self.unary())
        return self.primary()

    def type_spec(self) -> tuple[str, tuple[str, ...]]:
        """Parse ``DT_X[,n...])`` (the opening parenthesis is already consumed)."""
        type_name = self.take().text
        params: list[str] = []
        while self.accept(","):
            tok = self.take()
            if tok.kind != "num":
                raise SsisExprError(f"expected a number in type spec, got {tok.text!r}")
            params.append(tok.text)
        self.expect(")")
        return type_name, tuple(params)

    def primary(self) -> Node:
        tok = self.peek()
        if tok.kind == "str":
            self.pos += 1
            return StringLit(tok.text)
        if tok.kind == "num":
            self.pos += 1
            return NumberLit(tok.text)
        if tok.kind == "var":
            self.pos += 1
            return Variable(tok.text)
        if tok.kind == "bracket":
            self.pos += 1
            return ColumnRef(tok.text, bracketed=True)
        if tok.kind == "ident":
            upper = tok.text.upper()
            nxt = self.peek(1)
            is_call = nxt.kind == "op" and nxt.text == "("
            if upper == "NULL" and is_call:
                self.pos += 2
                if self.peek().kind != "ident" or not self.peek().text.upper().startswith("DT_"):
                    raise SsisExprError("NULL() needs a DT_ type")
                type_name, params = self.type_spec()
                return TypedNull(type_name, params)
            if is_call:
                self.pos += 2
                args: list[Node] = []
                if not self.accept(")"):
                    args.append(self.ternary())
                    while self.accept(","):
                        args.append(self.ternary())
                    self.expect(")")
                return Call(tok.text, tuple(args))
            self.pos += 1
            if upper in ("TRUE", "FALSE"):
                return BoolLit(upper == "TRUE")
            return ColumnRef(tok.text)
        if tok.kind == "op" and tok.text == "(":
            nxt = self.peek(1)
            if nxt.kind == "ident" and nxt.text.upper().startswith("DT_"):
                self.pos += 1
                type_name, params = self.type_spec()
                return Cast(type_name, params, self.unary())
            self.pos += 1
            node = self.ternary()
            self.expect(")")
            return node
        raise SsisExprError(f"unexpected token {tok.text!r}")


def parse_expression(text: str) -> Node:
    """Parse an SSIS expression into an AST.

    Args:
        text: SSIS expression text (FriendlyExpression form).

    Returns:
        The root AST node.

    Raises:
        SsisExprError: If the text cannot be tokenized or parsed.
    """
    return _Parser(text).parse()


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, Cast):
        return (node.operand,)
    if isinstance(node, Call):
        return node.args
    if isinstance(node, Unary):
        return (node.operand,)
    if isinstance(node, Binary):
        return (node.left, node.right)
    if isinstance(node, Ternary):
        return (node.cond, node.then, node.otherwise)
    return ()


def _walk(node: Node) -> list[Node]:
    out = [node]
    for child in _children(node):
        out.extend(_walk(child))
    return out


def _is_runtime(node: Node, runtime_variables: frozenset[str]) -> bool:
    if isinstance(node, Call):
        return node.name.upper() in _RUNTIME_FUNCTIONS and not node.args
    return isinstance(node, Variable) and node.name in runtime_variables


def _result_branches(node: Node) -> list[Node]:
    if isinstance(node, Ternary):
        return _result_branches(node.then) + _result_branches(node.otherwise)
    return [node]


def runtime_spellings(original: str, runtime_variables: frozenset[str] = RUNTIME_VARIABLES) -> list[str]:
    """Return the runtime-value spellings used in an expression, in order of appearance.

    Args:
        original: SSIS expression text.
        runtime_variables: Variable names (``Ns::Var``) that hold the package start time.

    Returns:
        e.g. ``["GETDATE()"]`` or ``["@[User::RunStartTime]"]``. If the text does not parse, a text
        scan is used instead.
    """
    try:
        root = parse_expression(original)
    except SsisExprError:
        return _runtime_spellings_in_text(original, runtime_variables)
    spellings: list[str] = []
    for node in _walk(root):
        if _is_runtime(node, runtime_variables):
            if isinstance(node, Call):
                text = f"{node.name}()"
            elif isinstance(node, Variable):
                text = f"@[{node.name}]"
            else:
                continue
            if text not in spellings:
                spellings.append(text)
    return spellings


# ---------------------------------------------------------------------------
# Spark emission (deterministic subset only)
# ---------------------------------------------------------------------------
_P_OR, _P_AND, _P_NOT, _P_CMP, _P_ATOM = 1, 2, 3, 4, 10
_CMP_OPS = {"==": "=", "!=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
_FUNCS = {"TRIM": "trim", "UPPER": "upper"}


def _wrap(sql: str, needed: bool) -> str:
    return f"({sql})" if needed else sql


@dataclass
class _Emitter:
    columns: set[str] = field(default_factory=set)

    def emit(self, node: Node) -> tuple[str, int]:
        if isinstance(node, ColumnRef):
            if node.bracketed or not _SIMPLE_IDENT.match(node.name):
                raise _NotCovered
            self.columns.add(node.name)
            return node.name, _P_ATOM
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
            if node.op != "!":
                raise _NotCovered
            sql, prec = self.emit(node.operand)
            return f"NOT {_wrap(sql, prec < _P_ATOM)}", _P_NOT
        if isinstance(node, Binary):
            if node.op in ("||", "&&"):
                prec = _P_OR if node.op == "||" else _P_AND
                keyword = "OR" if node.op == "||" else "AND"
                left, lp = self.emit(node.left)
                right, rp = self.emit(node.right)
                return f"{_wrap(left, lp < prec)} {keyword} {_wrap(right, rp <= prec)}", prec
            if node.op in _CMP_OPS:
                left, lp = self.emit(node.left)
                right, rp = self.emit(node.right)
                return (
                    f"{_wrap(left, lp <= _P_CMP)} {_CMP_OPS[node.op]} {_wrap(right, rp <= _P_CMP)}",
                    _P_CMP,
                )
            raise _NotCovered
        raise _NotCovered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def translate(original: str, runtime_variables: frozenset[str] = RUNTIME_VARIABLES) -> Expression:
    """Build an ``Expression`` (dialect ``ssis_expr``) for an SSIS expression.

    Args:
        original: Verbatim SSIS expression text.
        runtime_variables: Variable names (``Ns::Var``) that hold the package start time.

    Returns:
        A ``deterministic`` expression when a rule covers the whole text, otherwise ``pending``
        with ``spark_sql=None``.
    """
    try:
        root = parse_expression(original)
    except SsisExprError as err:
        logger.debug("SSIS expression parse failed (%s): %r", err, original)
        notes = PARSE_FAILURE_NOTE
        if _runtime_spellings_in_text(original, runtime_variables):
            notes = f"{PARSE_FAILURE_NOTE} {run_ts_note(original)}"
        return Expression(
            original=original,
            dialect=ExpressionDialect.SSIS_EXPR,
            translation_status=TranslationStatus.PENDING,
            notes=notes,
        )
    nodes = _walk(root)
    columns = sorted({n.name for n in nodes if isinstance(n, ColumnRef)})

    if _is_runtime(root, runtime_variables):
        if isinstance(root, Call):
            note = GETDATE_NOTE
        else:
            note = PACKAGE_TIME_NOTE.format(var=root.name if isinstance(root, Variable) else "")
        return Expression(
            original=original,
            dialect=ExpressionDialect.SSIS_EXPR,
            spark_sql="current_timestamp()",
            translation_status=TranslationStatus.DETERMINISTIC,
            referenced_columns=[],
            notes=note,
        )
    if any(_is_runtime(n, runtime_variables) for n in nodes):
        return Expression(
            original=original,
            dialect=ExpressionDialect.SSIS_EXPR,
            translation_status=TranslationStatus.PENDING,
            referenced_columns=columns,
            notes=run_ts_note(original),
        )
    try:
        spark_sql, _ = _Emitter().emit(root)
    except _NotCovered:
        return Expression(
            original=original,
            dialect=ExpressionDialect.SSIS_EXPR,
            translation_status=TranslationStatus.PENDING,
            referenced_columns=columns,
        )
    return Expression(
        original=original,
        dialect=ExpressionDialect.SSIS_EXPR,
        spark_sql=spark_sql,
        translation_status=TranslationStatus.DETERMINISTIC,
        referenced_columns=columns,
    )


def string_literal_domain(original: str) -> list[str] | None:
    """Return the string literals of a ``?:`` chain whose every result branch is a string literal.

    Args:
        original: SSIS expression text.

    Returns:
        The distinct literal values in order of appearance, or None if the expression is not such a chain.
    """
    try:
        root = parse_expression(original)
    except SsisExprError:
        return None
    if not isinstance(root, Ternary):
        return None
    values: list[str] = []
    for branch in _result_branches(root):
        if not isinstance(branch, StringLit):
            return None
        if branch.value not in values:
            values.append(branch.value)
    return values


def _is_literal(node: Node) -> bool:
    if isinstance(node, Cast):
        return isinstance(node.operand, (StringLit, NumberLit, BoolLit))
    return isinstance(node, (StringLit, NumberLit, BoolLit))


def is_literal_only(original: str) -> bool:
    """True if every result branch is a literal or a cast of a literal (no columns in the results).

    Args:
        original: SSIS expression text.

    Returns:
        True for a plain literal, a cast literal, or a ``?:`` chain of them; False otherwise or if the
        text does not parse.
    """
    try:
        root = parse_expression(original)
    except SsisExprError:
        return False
    return all(_is_literal(b) for b in _result_branches(root))


def literal_value(original: str) -> str | None:
    """Return the value of a string literal, or of a cast of a string literal, else None.

    Args:
        original: SSIS expression text, e.g. ``(DT_DBTIMESTAMP2,7)"9999-12-31 00:00:00"``.

    Returns:
        The unescaped string value, or None.
    """
    try:
        root = parse_expression(original)
    except SsisExprError:
        return None
    if isinstance(root, Cast):
        root = root.operand
    return root.value if isinstance(root, StringLit) else None


def is_bare_runtime_value(original: str, runtime_variables: frozenset[str] = RUNTIME_VARIABLES) -> bool:
    """True if the whole expression is one runtime value (``GETDATE()`` or a start-time variable).

    Args:
        original: SSIS expression text.
        runtime_variables: Variable names (``Ns::Var``) that hold the package start time.

    Returns:
        Whether the expression is a bare runtime value.
    """
    try:
        return _is_runtime(parse_expression(original), runtime_variables)
    except SsisExprError:
        return False


def not_equal_pairs(original: str) -> list[tuple[str, str]] | None:
    """Return ``(x, y)`` pairs if the expression is ``x != y`` comparisons of columns joined by ``||``.

    Args:
        original: SSIS expression text.

    Returns:
        The pairs in order of appearance, or None if the expression has any other shape.
    """
    try:
        root = parse_expression(original)
    except SsisExprError:
        return None
    pairs: list[tuple[str, str]] = []

    def collect(node: Node) -> bool:
        if isinstance(node, Binary) and node.op == "||":
            return collect(node.left) and collect(node.right)
        if (
            isinstance(node, Binary)
            and node.op == "!="
            and isinstance(node.left, ColumnRef)
            and isinstance(node.right, ColumnRef)
        ):
            pairs.append((node.left.name, node.right.name))
            return True
        return False

    return pairs if collect(root) else None


def resolve_lineage_refs(expression_text: str) -> str:
    """Replace ``#{...Columns[x]}`` lineage references with the column name ``x``.

    Args:
        expression_text: Text of an SSIS ``Expression`` property.

    Returns:
        The text with each lineage reference replaced by its column name.
    """
    return _LINEAGE_REF.sub(lambda m: m.group(1), expression_text)
