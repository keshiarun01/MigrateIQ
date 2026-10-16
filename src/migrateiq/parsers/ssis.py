"""Deterministic SSIS (.dtsx) parser: one package -> one ``MigrationUnit``.

Supports one data flow per package with OLE DB sources/destinations, Derived Column, Lookup,
Conditional Split, Multicast and Union All, plus the hand-built SCD2 pattern (Lookup current row ->
Conditional Split -> Multicast -> OLE DB Command expire + Union All insert). Everything else is recorded
in ``unsupported_constructs``.
"""

from __future__ import annotations

import heapq
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from lxml import etree
from sqlglot import exp

from migrateiq.ir.models import (
    Column,
    ColumnMapping,
    DataQualityRule,
    DatasetRef,
    DataType,
    DerivedColumnStep,
    DQRuleType,
    Expression,
    ExpressionDialect,
    FilterStep,
    LoadMode,
    LoadStrategy,
    LookupNoMatchPolicy,
    LookupStep,
    MigrationUnit,
    RouteGroup,
    RouterStep,
    RunParameter,
    RunParameterKind,
    Severity,
    SourceArtifact,
    SourceTechnology,
    Step,
    Target,
    TranslationStatus,
    UnionStep,
    UnsupportedConstruct,
)
from migrateiq.parsers import base
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.expr_rules import ssis_expr

logger = logging.getLogger(__name__)

DTS_NS = "www.microsoft.com/SqlServer/Dts"
SQLTASK_NS = "www.microsoft.com/sqlserver/dts/tasks/sqltask"
NS = {"DTS": DTS_NS, "SQLTask": SQLTASK_NS}

NOLOCK_NOTE = "WITH (NOLOCK) table hint dropped; no Spark equivalent needed for snapshot reads."
SQL_PARSE_NOTE = "Could not parse SQL with sqlglot (tsql); left for the translator."
SQL_NOT_SELECT_NOTE = "SQL is not a single SELECT statement; left for the translator."
SQL_HINT_NOTE = "Table hint(s) other than NOLOCK present ({hints}); left for the translator."
LINEAGE_NOTE = "FriendlyExpression absent; lineage references in Expression resolved to column names."

_ENDPOINT = re.compile(r"^(?P<path>.*)\.(?P<kind>Outputs|Inputs)\[(?P<port>[^\]]*)\]$")
_UPDATE = re.compile(
    r"^\s*UPDATE\s+(?P<table>[\w\[\].]+)\s+SET\s+(?P<to>[\w\[\]]+)\s*=\s*\?\s*,\s*(?P<flag>[\w\[\]]+)\s*=\s*0"
    r"\s+WHERE\s+(?P<sk>[\w\[\]]+)\s*=\s*\?\s*;?\s*$",
    re.IGNORECASE,
)
_TRUNCATE = re.compile(r"^\s*TRUNCATE\s+TABLE\s+(?P<table>[\w\[\].]+)\s*;?\s*$", re.IGNORECASE)

_SOURCE = "Microsoft.OLEDBSource"
_DERIVED = "Microsoft.DerivedColumn"
_LOOKUP = "Microsoft.Lookup"
_SPLIT = "Microsoft.ConditionalSplit"
_MULTICAST = "Microsoft.Multicast"
_UNION = "Microsoft.UnionAll"
_DEST = "Microsoft.OLEDBDestination"
_COMMAND = "Microsoft.OLEDBCommand"
_LKP_MATCH = "Lookup Match Output"
_LKP_NO_MATCH = "Lookup No Match Output"
_SNIPPET_LIMIT = 2000


def slug(name: str) -> str:
    """Lower-case ``name``, turn runs of non-alphanumerics into ``_`` and strip leading/trailing ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _split_table(name: str) -> tuple[str | None, str]:
    """``[edw].[dim_policy]`` or ``edw.dim_policy`` -> ``("edw", "dim_policy")``."""
    parts = [p.strip("[]") for p in name.strip().split(".")]
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


def _strip_brackets(name: str) -> str:
    return name.strip().strip("[]")


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _props(elem: etree._Element | None) -> dict[str, str]:
    """``{name: text}`` of ``<properties><property name=...>`` children (comments ignored)."""
    if elem is None:
        return {}
    holder = elem.find("properties")
    if holder is None:
        return {}
    return {p.get("name", ""): (p.text or "") for p in holder.findall("property")}


@dataclass
class _Comp:
    name: str
    cls: str
    elem: etree._Element
    index: int
    props: dict[str, str]

    def outputs(self) -> list[etree._Element]:
        holder = self.elem.find("outputs")
        return [] if holder is None else holder.findall("output")

    def inputs(self) -> list[etree._Element]:
        holder = self.elem.find("inputs")
        return [] if holder is None else holder.findall("input")

    def output(self, name: str) -> etree._Element | None:
        return next((o for o in self.outputs() if o.get("name") == name), None)

    def snippet(self) -> str:
        return etree.tostring(self.elem, encoding="unicode", with_tail=False)[:_SNIPPET_LIMIT]


@dataclass(frozen=True)
class _Edge:
    src: str
    src_out: str
    dst: str
    dst_in: str


@dataclass
class _SqlInfo:
    expression: Expression
    tables: list[tuple[str | None, str]]
    select: exp.Select | None


@dataclass
class _Scd2:
    command: str
    multicast: str
    lookup: str
    split: str
    union: str
    derived: str
    destination: str
    update_text: str
    business_keys: list[str]
    tracked_columns: list[str]
    effective_from: str
    effective_to: str
    current_flag: str
    surrogate_key: str
    end_date_sentinel: str
    source_variables: list[str]


@dataclass
class _State:
    """Mutable state while one package is parsed."""

    sources: list[DatasetRef] = field(default_factory=list)
    references: list[DatasetRef] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    targets: list[tuple[str, Target]] = field(default_factory=list)  # (component name, target)
    business_rules: list[str] = field(default_factory=list)
    unsupported: list[UnsupportedConstruct] = field(default_factory=list)
    runtime_refs: set[str] = field(default_factory=set)
    streams: dict[str, dict[str, str | None]] = field(default_factory=dict)
    known_types: dict[str, Column] = field(default_factory=dict)


class SsisParser:
    """Parse an SSIS package (.dtsx) into a ``MigrationUnit``.

    Args:
        catalog: Table catalog used for nullability, keys and target types. Without it, IR defaults
            (``nullable=True``, ``is_key=False``) are kept and a warning is logged.
    """

    technology = SourceTechnology.SSIS

    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog
        self._runtime_vars: frozenset[str] = ssis_expr.RUNTIME_VARIABLES
        self._var_exprs: dict[str, str] = {}

    # ------------------------------------------------------------------ entry point
    def parse(self, path: Path) -> MigrationUnit:
        """Parse one package.

        Args:
            path: Path to the ``.dtsx`` file.

        Returns:
            The validated ``MigrationUnit``.

        Raises:
            ValueError: If the package has no data flow or no target can be built.
        """
        root = etree.parse(str(path)).getroot()
        object_name = root.get(f"{{{DTS_NS}}}ObjectName") or path.stem
        state = _State()
        self._load_variables(root)
        connections = {
            cm.get(f"{{{DTS_NS}}}refId", ""): cm.get(f"{{{DTS_NS}}}ObjectName", "")
            for cm in root.findall("DTS:ConnectionManagers/DTS:ConnectionManager", NS)
        }

        executables = [e for e in root.iter(f"{{{DTS_NS}}}Executable") if e is not root]
        flows = [e for e in executables if e.get(f"{{{DTS_NS}}}ExecutableType") == "Microsoft.Pipeline"]
        if not flows:
            raise ValueError(f"{path}: no data flow (Microsoft.Pipeline) found")
        if len(flows) > 1:
            names = [f.get(f"{{{DTS_NS}}}ObjectName", "") for f in flows]
            logger.warning("%s: %d data flows; only the first (%s) is parsed", path, len(flows), names[0])
            state.unsupported.append(
                UnsupportedConstruct(
                    kind="multiple_data_flows",
                    original_snippet=", ".join(names),
                    reason="Only one data flow per package is supported; the first one was parsed.",
                )
            )
        flow = flows[0]
        pipeline = flow.find("DTS:ObjectData/pipeline", NS)
        if pipeline is None:
            raise ValueError(f"{path}: data flow has no pipeline element")

        dataflow_rules: list[str] = []
        state.business_rules = dataflow_rules
        self._parse_pipeline(pipeline, connections, state)
        control_rules = self._parse_control_flow(root, executables, flow, state)
        state.business_rules = control_rules + dataflow_rules

        if not state.sources:
            raise ValueError(f"{path}: no supported source could be built")
        if not state.targets:
            kinds = [u.kind for u in state.unsupported]
            raise ValueError(f"{path}: no target could be built; unsupported constructs: {kinds}")

        targets = [t for _, t in state.targets]
        parameters: list[RunParameter] = []
        if state.runtime_refs:
            parameters.append(
                RunParameter(
                    name="run_ts", kind=RunParameterKind.RUN_TS, legacy_refs=sorted(state.runtime_refs)
                )
            )
        unit = MigrationUnit(
            unit_id=f"ssis.{object_name}",
            name=object_name,
            source_artifact=SourceArtifact(
                technology=SourceTechnology.SSIS,
                file_path=self._display_path(path),
                object_name=object_name,
                sha256=base.sha256_file(path),
                parser_version=base.PARSER_VERSION,
            ),
            sources=state.sources,
            references=state.references,
            steps=state.steps,
            targets=targets,
            dq_rules=self._dq_rules(state),
            business_rules=state.business_rules,
            unsupported_constructs=state.unsupported,
            complexity=None,
            parameters=parameters,
        )
        return unit

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _display_path(path: Path) -> str:
        if not path.is_absolute():
            return path.as_posix()
        try:
            return path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return path.as_posix()

    def _load_variables(self, root: etree._Element) -> None:
        """Find package variables that hold a runtime value (e.g. ``@[System::StartTime]``)."""
        self._var_exprs = {}
        runtime = set(ssis_expr.RUNTIME_VARIABLES)
        for var in root.findall("DTS:Variables/DTS:Variable", NS):
            name = f"{var.get(f'{{{DTS_NS}}}Namespace', 'User')}::{var.get(f'{{{DTS_NS}}}ObjectName', '')}"
            expression = var.get(f"{{{DTS_NS}}}Expression")
            if expression and var.get(f"{{{DTS_NS}}}EvaluateAsExpression", "").lower() == "true":
                self._var_exprs[name] = expression
                if ssis_expr.is_bare_runtime_value(expression):
                    runtime.add(name)
        self._runtime_vars = frozenset(runtime)

    def _note_runtime(self, spellings: list[str], state: _State) -> None:
        """Record runtime-value spellings, plus the definition of any package variable among them."""
        for spelling in spellings:
            state.runtime_refs.add(spelling)
            var = spelling[2:-1] if spelling.startswith("@[") else spelling
            definition = self._var_exprs.get(var)
            if definition is not None:
                state.runtime_refs.update(ssis_expr.runtime_spellings(definition, self._runtime_vars))

    def _translate(self, original: str, state: _State) -> Expression:
        self._note_runtime(ssis_expr.runtime_spellings(original, self._runtime_vars), state)
        return ssis_expr.translate(original, self._runtime_vars)

    def _expression_of(self, props: dict[str, str], state: _State) -> Expression | None:
        """Expression from FriendlyExpression, else from Expression with lineage refs resolved."""
        if "FriendlyExpression" in props:
            return self._translate(props["FriendlyExpression"], state)
        if "Expression" in props:
            expression = self._translate(ssis_expr.resolve_lineage_refs(props["Expression"]), state)
            notes = f"{expression.notes} {LINEAGE_NOTE}" if expression.notes else LINEAGE_NOTE
            return expression.model_copy(update={"notes": notes})
        return None

    @staticmethod
    def _translate_sql(sql: str) -> _SqlInfo:
        """Translate a T-SQL ``SqlCommand`` with sqlglot (tsql -> spark)."""
        try:
            parsed = sqlglot.parse(sql, read="tsql")
        except sqlglot.errors.ParseError:
            logger.warning("sqlglot could not parse SQL: %r", sql)
            return _SqlInfo(
                Expression(original=sql, dialect=ExpressionDialect.TSQL, notes=SQL_PARSE_NOTE), [], None
            )
        statements = [p for p in parsed if p is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return _SqlInfo(
                Expression(original=sql, dialect=ExpressionDialect.TSQL, notes=SQL_NOT_SELECT_NOTE), [], None
            )
        select = statements[0]
        tables = [(t.db or None, t.name) for t in select.find_all(exp.Table)]
        columns: list[tuple[int, int, str]] = []
        for order, col in enumerate(c for c in select.walk(bfs=False) if isinstance(c, exp.Column)):
            start = col.this.meta.get("start") if isinstance(col.this, exp.Expression) else None
            columns.append((start if isinstance(start, int) else 10**9, order, col.name))
        referenced: list[str] = []
        for _, _, name in sorted(columns):
            if name and name not in referenced:
                referenced.append(name)
        hints: list[str] = []
        for table in select.find_all(exp.Table):
            for hint in table.args.get("hints") or []:
                hints.extend(e.sql(dialect="tsql").upper() for e in hint.expressions)
        other = [h for h in hints if h != "NOLOCK"]
        if other:
            note = SQL_HINT_NOTE.format(hints=", ".join(other))
            return _SqlInfo(
                Expression(
                    original=sql, dialect=ExpressionDialect.TSQL, referenced_columns=referenced, notes=note
                ),
                tables,
                select,
            )
        spark = sqlglot.transpile(sql, read="tsql", write="spark")[0]
        expression = Expression(
            original=sql,
            dialect=ExpressionDialect.TSQL,
            spark_sql=spark,
            translation_status=TranslationStatus.DETERMINISTIC,
            referenced_columns=referenced,
            notes=NOLOCK_NOTE if "NOLOCK" in hints else None,
        )
        return _SqlInfo(expression, tables, select)

    def _catalog_column(self, schema: str | None, table: str | None, name: str) -> Column | None:
        if self.catalog is None or schema is None or table is None:
            logger.warning(
                "No catalog entry for %s.%s.%s (catalog %s)",
                schema,
                table,
                name,
                "missing" if self.catalog is None else "lookup impossible",
            )
            return None
        column = self.catalog.column(schema, table, name)
        if column is None:
            logger.warning("Column %s.%s.%s not in catalog; keeping IR defaults", schema, table, name)
        return column

    @staticmethod
    def _ssis_column(col: etree._Element, state: _State) -> Column | None:
        """IR column from an SSIS ``outputColumn`` (type, length/precision, scale)."""
        name = col.get("name", "")
        try:
            data_type = base.map_type(SourceTechnology.SSIS, col.get("dataType", ""))
        except base.UnknownLegacyType as err:
            state.unsupported.append(
                UnsupportedConstruct(
                    kind="unknown_data_type",
                    original_snippet=etree.tostring(col, encoding="unicode", with_tail=False)[
                        :_SNIPPET_LIMIT
                    ],
                    reason=str(err),
                )
            )
            return None
        precision = col.get("length") or col.get("precision")
        scale = col.get("scale")
        column = Column(
            name=name,
            data_type=data_type,
            precision=int(precision) if precision else None,
            scale=int(scale) if scale and data_type == DataType.DECIMAL else None,
        )
        state.known_types.setdefault(name, column)
        return column

    # ------------------------------------------------------------------ data flow
    def _parse_pipeline(self, pipeline: etree._Element, connections: dict[str, str], state: _State) -> None:
        comps: dict[str, _Comp] = {}
        holder = pipeline.find("components")
        for index, elem in enumerate([] if holder is None else holder.findall("component")):
            name = elem.get("name", "")
            comps[name] = _Comp(name, elem.get("componentClassID", ""), elem, index, _props(elem))

        edges: list[_Edge] = []
        paths = pipeline.find("paths")
        for path_elem in [] if paths is None else paths.findall("path"):
            start = _ENDPOINT.match(path_elem.get("startId", ""))
            end = _ENDPOINT.match(path_elem.get("endId", ""))
            if not start or not end or start["kind"] != "Outputs" or end["kind"] != "Inputs":
                raise ValueError(
                    f"Cannot resolve path {path_elem.get('startId')} -> {path_elem.get('endId')}"
                )
            src = start["path"].rsplit("\\", 1)[-1]
            dst = end["path"].rsplit("\\", 1)[-1]
            if src not in comps or dst not in comps:
                raise ValueError(f"Path references unknown component: {src!r} -> {dst!r}")
            edges.append(_Edge(src, start["port"], dst, end["port"]))

        self._comps, self._edges, self._connections = comps, edges, connections
        self._schemas = self._component_schemas()
        self._scd2 = self._detect_scd2()

        for name in self._topological_order():
            comp = comps[name]
            inbound = self._inbound(name)
            streams = [self._stream_of(e, state) for e in inbound]
            if any(s is None for s in streams):
                logger.warning("Skipping component %r: an upstream component is unsupported", name)
                state.streams[name] = {}
                continue
            handler = {
                _SOURCE: self._source,
                _DERIVED: self._derived,
                _LOOKUP: self._lookup,
                _SPLIT: self._split,
                _MULTICAST: self._multicast,
                _UNION: self._union,
                _DEST: self._destination,
                _COMMAND: self._command,
            }.get(comp.cls)
            if handler is None:
                kind = slug(comp.cls.removeprefix("Microsoft."))
                logger.warning("Unsupported SSIS component %r (%s)", name, comp.cls)
                state.unsupported.append(
                    UnsupportedConstruct(
                        kind=kind, original_snippet=comp.snippet(), reason="Unsupported SSIS component"
                    )
                )
                state.streams[name] = {}
                continue
            state.streams[name] = handler(comp, [s for s in streams if s is not None], state)

    def _topological_order(self) -> list[str]:
        indegree = {name: 0 for name in self._comps}
        for edge in self._edges:
            indegree[edge.dst] += 1
        heap = [(c.index, c.name) for c in self._comps.values() if indegree[c.name] == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            _, name = heapq.heappop(heap)
            order.append(name)
            for edge in self._edges:
                if edge.src == name:
                    indegree[edge.dst] -= 1
                    if indegree[edge.dst] == 0:
                        heapq.heappush(heap, (self._comps[edge.dst].index, edge.dst))
        if len(order) != len(self._comps):
            raise ValueError("Data flow graph has a cycle")
        return order

    def _inbound(self, name: str) -> list[_Edge]:
        """Edges into a component, in the order of its declared inputs (then path order)."""
        edges = [e for e in self._edges if e.dst == name]
        declared = [i.get("name", "") for i in self._comps[name].inputs()]
        return sorted(
            edges, key=lambda e: declared.index(e.dst_in) if e.dst_in in declared else len(declared)
        )

    def _outbound(self, name: str) -> list[_Edge]:
        return [e for e in self._edges if e.src == name]

    def _stream_of(self, edge: _Edge, state: _State) -> str | None:
        comp = self._comps[edge.src]
        out = comp.output(edge.src_out)
        is_error = out is not None and out.get("isErrorOut", "").lower() == "true"
        if is_error or edge.src_out.endswith("Error Output"):
            snippet = f"{edge.src}.Outputs[{edge.src_out}] -> {edge.dst}"
            if not any(u.original_snippet == snippet for u in state.unsupported):
                state.unsupported.append(
                    UnsupportedConstruct(
                        kind="error_output",
                        original_snippet=snippet,
                        reason="SSIS error outputs are not supported",
                    )
                )
            return None
        streams = state.streams.get(edge.src, {})
        return streams.get(edge.src_out, streams.get("*"))

    def _component_schemas(self) -> dict[str, str]:
        """Schema each source/lookup/destination component reads or writes (for connection inference)."""
        schemas: dict[str, str] = {}
        for comp in self._comps.values():
            schema: str | None = None
            if comp.cls in (_SOURCE, _LOOKUP) and "SqlCommand" in comp.props:
                tables = self._translate_sql(comp.props["SqlCommand"]).tables
                found = {t[0] for t in tables if t[0]}
                schema = found.pop() if len(found) == 1 else None
            elif comp.cls == _DEST and comp.props.get("OpenRowset"):
                schema = _split_table(comp.props["OpenRowset"])[0]
            if schema:
                schemas[comp.name] = schema.lower()
        return schemas

    def _system_of(self, comp: _Comp) -> str | None:
        conns = comp.elem.find("connections")
        for conn in [] if conns is None else conns.findall("connection"):
            ref = conn.get("connectionManagerRefId", "")
            if ref in self._connections:
                return self._connections[ref]
        return None

    # ------------------------------------------------------------------ handlers
    def _source(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        sql = comp.props.get("SqlCommand")
        system = self._system_of(comp)
        if not sql or system is None:
            state.unsupported.append(
                UnsupportedConstruct(
                    kind="oledbsource",
                    original_snippet=comp.snippet(),
                    reason="OLE DB Source without SqlCommand or connection manager is not supported",
                )
            )
            return {}
        info = self._translate_sql(sql)
        schema, table = info.tables[0] if len(info.tables) == 1 else (None, None)
        if schema is None:
            schemas = {t[0] for t in info.tables if t[0]}
            schema = schemas.pop() if len(schemas) == 1 else ""
            logger.warning("Source %r reads %d tables; table left unset", comp.name, len(info.tables))
        columns: list[Column] = []
        main = next((o for o in comp.outputs() if o.get("isErrorOut", "").lower() != "true"), None)
        for col in [] if main is None else main.findall("outputColumns/outputColumn"):
            column = self._ssis_column(col, state)
            if column is None:
                continue
            known = self._catalog_column(schema, table, column.name)
            if known is not None:
                column = column.model_copy(update={"nullable": known.nullable, "is_key": known.is_key})
            columns.append(column)
        alias = slug(comp.name)
        state.sources.append(
            DatasetRef(
                alias=alias,
                system=system,
                schema_name=schema or "",
                table=table,
                sql_override=info.expression,
                columns=columns,
            )
        )
        return {"*": alias}

    def _derived(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        mappings: list[ColumnMapping] = []
        # "Replace column" mappings live on input columns.
        for inp in comp.inputs():
            for col in inp.findall("inputColumns/inputColumn"):
                expression = self._expression_of(_props(col), state)
                if expression is not None:
                    mappings.append(
                        ColumnMapping(target_column=col.get("cachedName", ""), expression=expression)
                    )
        for out in comp.outputs():
            if out.get("isErrorOut", "").lower() == "true":
                continue
            for col in out.findall("outputColumns/outputColumn"):
                expression = self._expression_of(_props(col), state)
                if expression is None:
                    state.unsupported.append(
                        UnsupportedConstruct(
                            kind="derived_column_without_expression",
                            original_snippet=etree.tostring(col, encoding="unicode", with_tail=False),
                            reason=f"Derived Column '{comp.name}' output column has no expression",
                        )
                    )
                    continue
                column = self._ssis_column(col, state)
                mappings.append(
                    ColumnMapping(
                        target_column=col.get("name", ""),
                        expression=expression,
                        target_type=None if column is None else column.data_type,
                    )
                )
        if not mappings or len(inputs) != 1:
            state.unsupported.append(
                UnsupportedConstruct(
                    kind="derivedcolumn",
                    original_snippet=comp.snippet(),
                    reason="Derived Column needs one input and at least one mapping",
                )
            )
            return {}
        step_id = slug(comp.name)
        state.steps.append(
            DerivedColumnStep(
                id=step_id,
                inputs=inputs,
                source_component=comp.name,
                mappings=mappings,
                keep_input_columns=True,
            )
        )
        return {"*": step_id}

    def _reference_columns(self, info: _SqlInfo, schema: str | None, table: str | None) -> list[Column]:
        columns: list[Column] = []
        if info.select is None:
            return columns
        for projection in info.select.expressions:
            if isinstance(projection, exp.Star):
                if self.catalog is not None and schema and table:
                    columns.extend(self.catalog.table(schema, table) or [])
                continue
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(inner, exp.Column):
                logger.warning("Reference projection %r is not a column; skipped", projection.sql())
                continue
            known = self._catalog_column(schema, table, inner.name)
            if known is None:
                logger.warning("Reference column %s has no catalog type; skipped", inner.name)
                continue
            columns.append(known.model_copy(update={"name": projection.alias_or_name}))
        return columns

    def _lookup(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        def unsupported(reason: str) -> dict[str, str | None]:
            state.unsupported.append(
                UnsupportedConstruct(kind="lookup", original_snippet=comp.snippet(), reason=reason)
            )
            return {}

        behavior = comp.props.get("NoMatchBehavior", "").strip()
        policy = {"0": LookupNoMatchPolicy.FAIL, "1": LookupNoMatchPolicy.REDIRECT}.get(behavior)
        if policy is None:
            return unsupported(f"Unsupported Lookup NoMatchBehavior={behavior!r}")
        sql = comp.props.get("SqlCommand")
        if not sql:
            return unsupported("Lookup without SqlCommand is not supported")
        info = self._translate_sql(sql)
        if len(info.tables) != 1 or info.tables[0][0] is None:
            return unsupported("Lookup reference query must read exactly one schema-qualified table")
        schema, table = info.tables[0]
        system = self._system_of(comp)
        if system is None:
            candidates = [
                (other.name, self._system_of(other))
                for other in self._comps.values()
                if other.name != comp.name
                and self._schemas.get(other.name) == schema.lower()
                and self._system_of(other) is not None
            ]
            systems = sorted({s for _, s in candidates if s})
            if len(systems) != 1:
                return unsupported(
                    "Lookup has no connection manager and its system cannot be inferred "
                    f"(candidates: {systems})"
                )
            system = systems[0]
            donor = candidates[0][0]
            logger.warning(
                "Lookup %r has no connection manager; system %r inferred from %r (schema %r)",
                comp.name,
                system,
                donor,
                schema,
            )
            state.business_rules.append(
                f"Lookup '{comp.name}' has no connection manager; system '{system}' inferred from "
                f"'{donor}', which uses the same schema '{schema}'."
            )
        join_keys: dict[str, str] = {}
        for inp in comp.inputs():
            for col in inp.findall("inputColumns/inputColumn"):
                ref_col = _props(col).get("JoinToReferenceColumn")
                if ref_col:
                    join_keys[col.get("cachedName", "")] = ref_col
        return_columns: dict[str, str] = {}
        match = comp.output(_LKP_MATCH)
        for col in [] if match is None else match.findall("outputColumns/outputColumn"):
            ref_col = _props(col).get("CopyFromReferenceColumn")
            if ref_col:
                return_columns[col.get("name", "")] = ref_col
                self._ssis_column(col, state)
        if not join_keys or not return_columns:
            return unsupported("Lookup needs join keys and return columns")

        alias = f"ref_{slug(table)}"
        existing = next((r for r in state.references if r.alias == alias), None)
        if existing is None:
            state.references.append(
                DatasetRef(
                    alias=alias,
                    system=system,
                    schema_name=schema,
                    table=table,
                    sql_override=info.expression,
                    columns=self._reference_columns(info, schema, table),
                )
            )
        elif existing.sql_override is None or existing.sql_override.original != sql:
            raise ValueError(
                f"Two lookups read {schema}.{table} with different queries; alias {alias} clashes"
            )

        step = LookupStep(
            id=slug(comp.name),
            inputs=inputs,
            source_component=comp.name,
            reference=alias,
            join_keys=join_keys,
            return_columns=return_columns,
            on_no_match=policy,
            on_multiple_match="any",
        )
        state.steps.append(step)
        if policy == LookupNoMatchPolicy.REDIRECT:
            return {_LKP_MATCH: f"{step.id}.match", _LKP_NO_MATCH: f"{step.id}.no_match"}
        return {_LKP_MATCH: step.id}

    def _split(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        step_id = slug(comp.name)
        groups: list[tuple[int, int, RouteGroup]] = []
        default: str | None = None
        streams: dict[str, str | None] = {}
        connected = {e.src_out for e in self._outbound(comp.name)}
        for position, out in enumerate(comp.outputs()):
            out_name = out.get("name", "")
            if out.get("isErrorOut", "").lower() == "true":
                continue
            streams[out_name] = f"{step_id}.{slug(out_name)}"
            if out_name not in connected:
                state.business_rules.append(
                    f"Conditional Split '{comp.name}' output '{out_name}' is not connected: "
                    "those rows are discarded."
                )
            if out.get("isDefaultOut", "").lower() == "true":
                default = slug(out_name)
                continue
            props = _props(out)
            condition = self._expression_of(props, state)
            if condition is None:
                state.unsupported.append(
                    UnsupportedConstruct(
                        kind="conditionalsplit",
                        original_snippet=comp.snippet(),
                        reason=f"Conditional Split output '{out_name}' has no condition",
                    )
                )
                return {}
            order = int(props.get("EvaluationOrder", position))
            groups.append((order, position, RouteGroup(name=slug(out_name), condition=condition)))
        if not groups:
            state.unsupported.append(
                UnsupportedConstruct(
                    kind="conditionalsplit",
                    original_snippet=comp.snippet(),
                    reason="Conditional Split has no conditions",
                )
            )
            return {}
        state.steps.append(
            RouterStep(
                id=step_id,
                inputs=inputs,
                source_component=comp.name,
                groups=[g for _, _, g in sorted(groups, key=lambda t: (t[0], t[1]))],
                default_group=default,
                first_match_only=True,
            )
        )
        return streams

    def _multicast(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        return {"*": inputs[0]} if len(inputs) == 1 else {}

    def _union(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        step_id = slug(comp.name)
        state.steps.append(UnionStep(id=step_id, inputs=inputs, source_component=comp.name, by_name=True))
        return {"*": step_id}

    def _command(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        variables = [
            _props(c)["SourceVariable"]
            for i in comp.inputs()
            for c in i.findall("inputColumns/inputColumn")
            if _props(c).get("SourceVariable")
        ]
        self._note_runtime([v for v in variables if v in self._runtime_vars], state)
        scd2 = self._scd2.get(comp.name)
        if scd2 is not None:
            state.business_rules.append(
                f"Row-by-row OLE DB Command '{comp.name}' (fed by Multicast '{scd2.multicast}') expires "
                f"the current version; replaced by a set-based SCD2 MERGE. Original: {scd2.update_text}"
            )
            return {}
        state.unsupported.append(
            UnsupportedConstruct(
                kind="ole_db_command",
                original_snippet=comp.props.get("SqlCommand", comp.snippet()),
                reason="Row-by-row OLE DB Command outside the recognised SCD2 expire pattern",
            )
        )
        return {}

    def _destination(self, comp: _Comp, inputs: list[str], state: _State) -> dict[str, str | None]:
        rowset = comp.props.get("OpenRowset")
        system = self._system_of(comp)
        if not rowset or system is None or len(inputs) != 1:
            state.unsupported.append(
                UnsupportedConstruct(
                    kind="oledbdestination",
                    original_snippet=comp.snippet(),
                    reason="OLE DB Destination needs OpenRowset, a connection manager and one input",
                )
            )
            return {}
        schema, table = _split_table(rowset)
        columns: list[Column] = []
        for inp in comp.inputs():
            for col in inp.findall("inputColumns/inputColumn"):
                name = _strip_brackets(col.get("externalMetadataColumnId", "") or col.get("cachedName", ""))
                known = self._catalog_column(schema, table, name)
                if known is None:
                    fallback = state.known_types.get(col.get("cachedName", ""))
                    if fallback is None:
                        raise ValueError(f"Destination '{comp.name}' column {name!r}: type unknown")
                    known = Column(
                        name=name,
                        data_type=fallback.data_type,
                        precision=fallback.precision,
                        scale=fallback.scale,
                    )
                columns.append(known)
        alias = f"tgt_{slug(table)}"
        dataset = DatasetRef(
            alias=alias, system=system, schema_name=schema or "", table=table, columns=columns
        )
        strategy = self._scd2_strategy(comp, schema, table)
        if strategy is None:
            strategy = LoadStrategy(mode=LoadMode.APPEND)  # the control flow may turn it into full_overwrite
        state.targets.append((comp.name, Target(input=inputs[0], dataset=dataset, load_strategy=strategy)))
        return {}

    # ------------------------------------------------------------------ SCD2 (T6, T7)
    def _scd2_strategy(self, comp: _Comp, schema: str | None, table: str) -> LoadStrategy | None:
        scd2 = next((s for s in self._scd2.values() if s.destination == comp.name), None)
        if scd2 is None:
            return None
        identity = self.catalog.identity_columns(schema, table) if self.catalog and schema else []
        return LoadStrategy(
            mode=LoadMode.SCD2,
            business_keys=scd2.business_keys,
            tracked_columns=scd2.tracked_columns,
            effective_from_column=scd2.effective_from,
            effective_to_column=scd2.effective_to,
            current_flag_column=scd2.current_flag,
            surrogate_key_column=scd2.surrogate_key,
            end_date_sentinel=scd2.end_date_sentinel,
            surrogate_key_strategy="identity" if scd2.surrogate_key in identity else None,
        )

    def _detect_scd2(self) -> dict[str, _Scd2]:
        found: dict[str, _Scd2] = {}
        for comp in self._comps.values():
            if comp.cls == _COMMAND:
                match = self._match_scd2(comp)
                if match is not None:
                    found[comp.name] = match
                else:
                    logger.debug("OLE DB Command %r does not match the SCD2 expire pattern", comp.name)
        return found

    def _single(self, edges: list[_Edge], cls: str) -> _Edge | None:
        return edges[0] if len(edges) == 1 and self._comps[edges[0].src].cls == cls else None

    def _match_scd2(self, cmd: _Comp) -> _Scd2 | None:  # noqa: C901 - one linear pattern check
        sql = cmd.props.get("SqlCommand", "")
        update = _UPDATE.match(sql)
        if update is None:
            return None
        upd_table = _split_table(update["table"])
        to_col, flag_col, sk_col = (_strip_brackets(update[k]) for k in ("to", "flag", "sk"))

        mc_edge = self._single(self._inbound(cmd.name), _MULTICAST)
        if mc_edge is None:
            return None
        cs_edge = self._single(self._inbound(mc_edge.src), _SPLIT)
        if cs_edge is None:
            return None
        split = self._comps[cs_edge.src]
        changed = split.output(cs_edge.src_out)
        if changed is None or changed.get("isDefaultOut", "").lower() == "true":
            return None
        lk_edge = self._single(self._inbound(split.name), _LOOKUP)
        if lk_edge is None or lk_edge.src_out != _LKP_MATCH:
            return None
        lookup = self._comps[lk_edge.src]
        if lookup.props.get("NoMatchBehavior", "").strip() != "1" or "SqlCommand" not in lookup.props:
            return None
        ref_tables = self._translate_sql(lookup.props["SqlCommand"]).tables
        if len(ref_tables) != 1 or not _same_table(ref_tables[0], upd_table):
            return None
        match_output = lookup.output(_LKP_MATCH)
        return_columns = {
            c.get("name", ""): _props(c).get("CopyFromReferenceColumn", "")
            for c in ([] if match_output is None else match_output.findall("outputColumns/outputColumn"))
        }
        join_keys = [
            c.get("cachedName", "")
            for i in lookup.inputs()
            for c in i.findall("inputColumns/inputColumn")
            if _props(c).get("JoinToReferenceColumn")
        ]
        props = _props(changed)
        condition = props.get("FriendlyExpression")
        if condition is None and "Expression" in props:
            condition = ssis_expr.resolve_lineage_refs(props["Expression"])
        pairs = ssis_expr.not_equal_pairs(condition or "")
        if not pairs or any(return_columns.get(old) != new for new, old in pairs):
            return None

        mc_out = self._outbound(mc_edge.src)
        if len(mc_out) != 2 or not any(e.dst == cmd.name for e in mc_out):
            return None
        union_edge = next(e for e in mc_out if e.dst != cmd.name)
        union = self._comps[union_edge.dst]
        if union.cls != _UNION:
            return None
        union_in = self._inbound(union.name)
        others = [e for e in union_in if e.src != mc_edge.src]
        if len(union_in) != 2 or len(others) != 1:
            return None
        if others[0].src != lookup.name or others[0].src_out != _LKP_NO_MATCH:
            return None
        union_out = self._outbound(union.name)
        if len(union_out) != 1 or self._comps[union_out[0].dst].cls != _DERIVED:
            return None
        derived = self._comps[union_out[0].dst]
        der_out = self._outbound(derived.name)
        if len(der_out) != 1 or self._comps[der_out[0].dst].cls != _DEST:
            return None
        dest = self._comps[der_out[0].dst]
        if not dest.props.get("OpenRowset") or not _same_table(
            _split_table(dest.props["OpenRowset"]), upd_table
        ):
            return None

        exprs: dict[str, str] = {}
        for out in derived.outputs():
            for col in out.findall("outputColumns/outputColumn"):
                p = _props(col)
                text = p.get("FriendlyExpression")
                if text is None and "Expression" in p:
                    text = ssis_expr.resolve_lineage_refs(p["Expression"])
                if text is not None:
                    exprs[col.get("name", "")] = text
        sentinel = ssis_expr.literal_value(exprs.get(to_col, ""))
        if sentinel is None or not ssis_expr.is_literal_only(exprs.get(flag_col, "")):
            return None
        from_cols = [
            n
            for n, t in exprs.items()
            if n not in (to_col, flag_col) and ssis_expr.is_bare_runtime_value(t, self._runtime_vars)
        ]
        if len(from_cols) != 1:
            return None

        params = {
            _props(c).get("ParameterIndex", ""): c
            for i in cmd.inputs()
            for c in i.findall("inputColumns/inputColumn")
        }
        sk_param = params.get("1")
        if sk_param is None or return_columns.get(sk_param.get("cachedName", "")) != sk_col:
            return None
        variables = [_props(c)["SourceVariable"] for c in params.values() if _props(c).get("SourceVariable")]

        return _Scd2(
            command=cmd.name,
            multicast=mc_edge.src,
            lookup=lookup.name,
            split=split.name,
            union=union.name,
            derived=derived.name,
            destination=dest.name,
            update_text=_collapse_ws(sql),
            business_keys=sorted(join_keys),
            tracked_columns=sorted(new for new, _ in pairs),
            effective_from=from_cols[0],
            effective_to=to_col,
            current_flag=flag_col,
            surrogate_key=sk_col,
            end_date_sentinel=sentinel,
            source_variables=variables,
        )

    # ------------------------------------------------------------------ control flow
    def _parse_control_flow(
        self, root: etree._Element, executables: list[etree._Element], flow: etree._Element, state: _State
    ) -> list[str]:
        rules: list[str] = []
        flow_ref = flow.get(f"{{{DTS_NS}}}refId", "")
        constraints = {
            (pc.get(f"{{{DTS_NS}}}From", ""), pc.get(f"{{{DTS_NS}}}To", ""))
            for pc in root.iter(f"{{{DTS_NS}}}PrecedenceConstraint")
        }
        for task in executables:
            task_type = task.get(f"{{{DTS_NS}}}ExecutableType", "")
            if task_type == "Microsoft.Pipeline":
                continue
            task_name = task.get(f"{{{DTS_NS}}}ObjectName", "")
            snippet = etree.tostring(task, encoding="unicode", with_tail=False)[:_SNIPPET_LIMIT]
            if task_type != "Microsoft.ExecuteSQLTask":
                state.unsupported.append(
                    UnsupportedConstruct(
                        kind=slug(task_type.removeprefix("Microsoft.")),
                        original_snippet=snippet,
                        reason="Unsupported SSIS control-flow task",
                    )
                )
                continue
            data = task.find("DTS:ObjectData/SQLTask:SqlTaskData", NS)
            statement = "" if data is None else data.get(f"{{{SQLTASK_NS}}}SqlStatementSource", "")
            truncate = _TRUNCATE.match(statement)
            linked = (task.get(f"{{{DTS_NS}}}refId", ""), flow_ref) in constraints
            target = None
            if truncate and linked:
                table = _split_table(truncate["table"])
                target = next(
                    (
                        t
                        for _, t in state.targets
                        if _same_table((t.dataset.schema_name, t.dataset.table or ""), table)
                        and t.load_strategy.mode == LoadMode.APPEND
                    ),
                    None,
                )
            if target is None:
                state.unsupported.append(
                    UnsupportedConstruct(
                        kind="execute_sql_task",
                        original_snippet=statement or snippet,
                        reason="Execute SQL Task is not a TRUNCATE of the data flow target run before it",
                    )
                )
                continue
            target.load_strategy = LoadStrategy(mode=LoadMode.FULL_OVERWRITE)
            rules.append(f"Target is truncated and fully reloaded each run (Execute SQL Task '{task_name}').")
        return rules

    # ------------------------------------------------------------------ data quality
    def _dq_rules(self, state: _State) -> list[DataQualityRule]:
        rules: list[DataQualityRule] = []
        derived: dict[str, Expression] = {}
        for step in state.steps:
            if isinstance(step, DerivedColumnStep):
                for mapping in step.mappings:
                    derived[mapping.target_column] = mapping.expression
        for comp_name, target in state.targets:
            alias = target.dataset.alias
            names = {
                _strip_brackets(c.get("externalMetadataColumnId", "") or c.get("cachedName", "")): c.get(
                    "cachedName", ""
                )
                for i in self._comps[comp_name].inputs()
                for c in i.findall("inputColumns/inputColumn")
            }
            for column in target.dataset.columns:
                if column.is_key:
                    rules.append(
                        DataQualityRule(
                            dataset_alias=alias,
                            column=column.name,
                            rule_type=DQRuleType.UNIQUE,
                            severity=Severity.ERROR,
                            derived_from="PRIMARY KEY",
                        )
                    )
            for column in target.dataset.columns:
                expression = derived.get(names.get(column.name, column.name))
                if (
                    expression is not None
                    and not column.nullable
                    and not ssis_expr.is_literal_only(expression.original)
                    and not ssis_expr.is_bare_runtime_value(expression.original, self._runtime_vars)
                ):
                    rules.append(
                        DataQualityRule(
                            dataset_alias=alias,
                            column=column.name,
                            rule_type=DQRuleType.NOT_NULL,
                            severity=Severity.ERROR,
                            derived_from="NOT NULL constraint",
                        )
                    )
            for column in target.dataset.columns:
                expression = derived.get(names.get(column.name, column.name))
                domain = None if expression is None else ssis_expr.string_literal_domain(expression.original)
                if domain:
                    rules.append(
                        DataQualityRule(
                            dataset_alias=alias,
                            column=column.name,
                            rule_type=DQRuleType.ACCEPTED_VALUES,
                            params={"values": domain},
                            severity=Severity.ERROR,
                            derived_from="derived column domain",
                        )
                    )
            gating = any(
                isinstance(s, (FilterStep, RouterStep))
                or (
                    isinstance(s, LookupStep)
                    and s.on_no_match in (LookupNoMatchPolicy.FAIL, LookupNoMatchPolicy.REDIRECT)
                )
                for s in state.steps
            )
            if (
                target.load_strategy.mode == LoadMode.FULL_OVERWRITE
                and len(state.sources) == 1
                and not gating
            ):
                rules.append(
                    DataQualityRule(
                        dataset_alias=alias,
                        rule_type=DQRuleType.ROW_COUNT_MATCH,
                        params={"against": state.sources[0].alias},
                        severity=Severity.ERROR,
                        derived_from="full reload, no filters",
                    )
                )
        return rules


def _same_table(a: tuple[str | None, str], b: tuple[str | None, str]) -> bool:
    """Compare ``(schema, table)`` pairs case-insensitively; a missing schema means ``dbo``."""
    return ((a[0] or "dbo").lower(), a[1].lower()) == ((b[0] or "dbo").lower(), b[1].lower())
