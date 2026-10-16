"""Deterministic Informatica PowerCenter (XML export) parser: one mapping -> one ``MigrationUnit``.

Supports Source Definition + Source Qualifier, Aggregator, Joiner, connected and unconnected Lookup
Procedures, Expression, Router and Target Definitions, with load modes read from the ``<SESSION>`` that runs
the mapping (T15). Connectors give the topology; because exports are often abridged, ports with no connector
take the same-named port of their upstream instance, and each such inferred link is logged at WARNING.
Everything else is recorded in ``unsupported_constructs``.
"""

from __future__ import annotations

import heapq
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from migrateiq.ir.models import (
    AggregateStep,
    Aggregation,
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
    JoinStep,
    JoinType,
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
    UnsupportedConstruct,
)
from migrateiq.parsers import base
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.expr_rules import informatica_expr

logger = logging.getLogger(__name__)

_SNIPPET_LIMIT = 2000
_SOURCE_DEF = "Source Definition"
_TARGET_DEF = "Target Definition"
_SQ = "Source Qualifier"
_AGG = "Aggregator"
_JOINER = "Joiner"
_LOOKUP = "Lookup Procedure"
_EXPRESSION = "Expression"
_ROUTER = "Router"

_JOIN_TYPES = {
    "Normal Join": JoinType.INNER,
    "Master Outer Join": JoinType.LEFT,
    "Detail Outer Join": JoinType.RIGHT,
    "Full Outer Join": JoinType.FULL,
}
_MULTIPLE_MATCH: dict[str, str] = {
    "Use Any Value": "any",
    "Use First Value": "first",
    "Use Last Value": "last",
    "Report Error": "error",
}
_LOOKUP_EQ = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*$")
_ROUTER_FIELD = re.compile(r"^(?P<base>.+?)(?P<n>\d+)$")
_SQL_RUNTIME = re.compile(r"\b(SESSSTARTTIME|SYSDATE)\b", re.IGNORECASE)
_PASSTHROUGH_TYPES = (_EXPRESSION, _ROUTER)


def slug(name: str) -> str:
    """Lower-case ``name``, turn runs of non-alphanumerics into ``_`` and strip leading/trailing ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _snippet(elem: etree._Element) -> str:
    return etree.tostring(elem, encoding="unicode", with_tail=False)[:_SNIPPET_LIMIT]


@dataclass
class _Port:
    name: str
    datatype: str
    porttype: str
    expression: str | None
    expressiontype: str
    precision: str | None
    scale: str | None
    nullable: bool
    is_key: bool
    elem: etree._Element

    @property
    def is_input(self) -> bool:
        return "INPUT" in self.porttype.split("/")

    @property
    def is_output(self) -> bool:
        return "OUTPUT" in self.porttype.split("/")

    @property
    def is_variable(self) -> bool:
        return self.porttype == "LOCAL VARIABLE"


@dataclass
class _Trans:
    name: str
    type: str
    elem: etree._Element
    ports: list[_Port]
    attrs: dict[str, str]

    def port(self, name: str) -> _Port | None:
        return next((p for p in self.ports if p.name == name), None)


@dataclass(frozen=True)
class _Conn:
    src: str
    src_field: str
    dst: str
    dst_field: str


@dataclass
class _Instance:
    name: str
    trans_type: str
    index: int
    trans: _Trans | None


@dataclass
class _Feed:
    """Where a target column comes from: ``port`` of ``instance`` (after pass-through walks)."""

    column: str
    instance: str
    port: str
    explicit: bool
    passed_through: bool


@dataclass
class _State:
    """Mutable state while one mapping is parsed."""

    sources: list[DatasetRef] = field(default_factory=list)
    references: list[DatasetRef] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    targets: list[tuple[str, Target, list[_Feed]]] = field(default_factory=list)
    business_rules: list[str] = field(default_factory=list)
    unsupported: list[UnsupportedConstruct] = field(default_factory=list)
    runtime_refs: set[str] = field(default_factory=set)
    # Per instance: the IR stream it produces (None = unsupported / skipped), its output ports and types.
    streams: dict[str, str | None] = field(default_factory=dict)
    ports_out: dict[str, list[str]] = field(default_factory=dict)
    port_types: dict[str, dict[str, DataType | None]] = field(default_factory=dict)
    ancestors: dict[str, set[str]] = field(default_factory=dict)
    logged_links: set[tuple[str, str, str, str]] = field(default_factory=set)
    # Router consumers: instance name -> router stream ("<id>.<group>") it reads.
    router_feeds: dict[str, str] = field(default_factory=dict)

    def rule(self, text: str) -> None:
        if text not in self.business_rules:
            self.business_rules.append(text)


class InformaticaParser:
    """Parse an Informatica PowerCenter XML export (one mapping) into a ``MigrationUnit``.

    Args:
        catalog: Table catalog used to resolve lookup tables to a schema and to type lookup source filters.
            Without it every lookup is recorded as unsupported.
    """

    technology = SourceTechnology.INFORMATICA

    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog

    # ------------------------------------------------------------------ entry point
    def parse(self, path: Path) -> MigrationUnit:
        """Parse one PowerCenter export.

        Args:
            path: Path to the XML export.

        Returns:
            The validated ``MigrationUnit``.

        Raises:
            ValueError: If the file does not hold exactly one mapping, or no source or target can be built.
        """
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.parse(str(path), parser).getroot()
        mappings = list(root.iter("MAPPING"))
        if len(mappings) != 1:
            raise ValueError(f"{path}: expected exactly one MAPPING, found {len(mappings)}")
        mapping = mappings[0]
        folder = mapping.getparent()
        if folder is None:
            raise ValueError(f"{path}: MAPPING has no enclosing FOLDER")
        name = mapping.get("NAME", "") or path.stem

        self._folder = folder
        self._mapping_elem = mapping
        self._load(folder, mapping)
        state = _State()
        self._state = state
        self._lkp_users = self._find_lookup_users()
        for inst_name in self._topological_order():
            self._process(self._instances[inst_name], state)

        if not state.sources:
            raise ValueError(f"{path}: no supported source could be built")
        self._apply_session(name, state)
        if not state.targets:
            kinds = [u.kind for u in state.unsupported]
            raise ValueError(f"{path}: no target could be built; unsupported constructs: {kinds}")

        parameters: list[RunParameter] = []
        if state.runtime_refs:
            parameters.append(
                RunParameter(
                    name="run_ts", kind=RunParameterKind.RUN_TS, legacy_refs=sorted(state.runtime_refs)
                )
            )
        return MigrationUnit(
            unit_id=f"informatica.{name}",
            name=name,
            source_artifact=SourceArtifact(
                technology=SourceTechnology.INFORMATICA,
                file_path=self._display_path(path),
                object_name=name,
                sha256=base.sha256_file(path),
                parser_version=base.PARSER_VERSION,
            ),
            sources=state.sources,
            references=state.references,
            steps=state.steps,
            targets=[t for _, t, _ in state.targets],
            dq_rules=self._dq_rules(state),
            business_rules=state.business_rules,
            unsupported_constructs=state.unsupported,
            complexity=None,
            parameters=parameters,
        )

    # ------------------------------------------------------------------ loading
    @staticmethod
    def _display_path(path: Path) -> str:
        if not path.is_absolute():
            return path.as_posix()
        try:
            return path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _ports(elem: etree._Element, tag: str) -> list[_Port]:
        ports: list[_Port] = []
        for f in elem.findall(tag):
            ports.append(
                _Port(
                    name=f.get("NAME", ""),
                    datatype=f.get("DATATYPE", ""),
                    porttype=f.get("PORTTYPE", "").upper(),
                    expression=f.get("EXPRESSION"),
                    expressiontype=f.get("EXPRESSIONTYPE", "").upper(),
                    precision=f.get("PRECISION"),
                    scale=f.get("SCALE"),
                    nullable=f.get("NULLABLE", "") != "NOTNULL",
                    is_key=f.get("KEYTYPE", "") == "PRIMARY KEY",
                    elem=f,
                )
            )
        return ports

    @staticmethod
    def _attrs(elem: etree._Element) -> dict[str, str]:
        return {a.get("NAME", ""): a.get("VALUE", "") for a in elem.findall("TABLEATTRIBUTE")}

    def _load(self, folder: etree._Element, mapping: etree._Element) -> None:
        sources = {
            s.get("NAME", ""): _Trans(s.get("NAME", ""), _SOURCE_DEF, s, self._ports(s, "SOURCEFIELD"), {})
            for s in folder.findall("SOURCE")
        }
        targets = {
            t.get("NAME", ""): _Trans(t.get("NAME", ""), _TARGET_DEF, t, self._ports(t, "TARGETFIELD"), {})
            for t in folder.findall("TARGET")
        }
        reusable = {
            t.get("NAME", ""): _Trans(
                t.get("NAME", ""), t.get("TYPE", ""), t, self._ports(t, "TRANSFORMFIELD"), self._attrs(t)
            )
            for t in folder.findall("TRANSFORMATION")
        }
        local = {
            t.get("NAME", ""): _Trans(
                t.get("NAME", ""), t.get("TYPE", ""), t, self._ports(t, "TRANSFORMFIELD"), self._attrs(t)
            )
            for t in mapping.findall("TRANSFORMATION")
        }
        self._instances: dict[str, _Instance] = {}
        for index, inst in enumerate(mapping.findall("INSTANCE")):
            inst_name = inst.get("NAME", "")
            trans_name = inst.get("TRANSFORMATION_NAME", inst_name)
            trans_type = inst.get("TRANSFORMATION_TYPE", "")
            if trans_type == _SOURCE_DEF:
                trans = sources.get(trans_name)
            elif trans_type == _TARGET_DEF:
                trans = targets.get(trans_name)
            else:
                trans = local.get(trans_name) or reusable.get(trans_name)
            self._instances[inst_name] = _Instance(inst_name, trans_type, index, trans)
        self._conns = [
            _Conn(
                c.get("FROMINSTANCE", ""),
                c.get("FROMFIELD", ""),
                c.get("TOINSTANCE", ""),
                c.get("TOFIELD", ""),
            )
            for c in mapping.findall("CONNECTOR")
        ]
        for conn in self._conns:
            for end in (conn.src, conn.dst):
                if end not in self._instances:
                    raise ValueError(f"Connector references unknown instance {end!r}")

    def _topological_order(self) -> list[str]:
        edges = sorted({(c.src, c.dst) for c in self._conns})
        indegree = {name: 0 for name in self._instances}
        for _, dst in edges:
            indegree[dst] += 1
        heap = [(i.index, i.name) for i in self._instances.values() if indegree[i.name] == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            _, name = heapq.heappop(heap)
            order.append(name)
            for src, dst in edges:
                if src == name:
                    indegree[dst] -= 1
                    if indegree[dst] == 0:
                        heapq.heappush(heap, (self._instances[dst].index, dst))
        if len(order) != len(self._instances):
            raise ValueError("Mapping graph has a cycle")
        return order

    def _upstream(self, name: str) -> list[str]:
        """Upstream instances of ``name``, in instance declaration order."""
        found = {c.src for c in self._conns if c.dst == name}
        return sorted(found, key=lambda n: self._instances[n].index)

    def _downstream(self, name: str) -> list[str]:
        found = {c.dst for c in self._conns if c.src == name}
        return sorted(found, key=lambda n: self._instances[n].index)

    def _find_lookup_users(self) -> dict[str, list[str]]:
        """Lookup instance name -> Expression instances that call it with ``:LKP.``."""
        users: dict[str, list[str]] = {}
        for inst in self._instances.values():
            if inst.trans is None:
                continue
            for port in inst.trans.ports:
                for use in informatica_expr.lookup_calls(port.expression or ""):
                    users.setdefault(use.name, [])
                    if inst.name not in users[use.name]:
                        users[use.name].append(inst.name)
        return users

    # ------------------------------------------------------------------ helpers
    def _unsupported(self, kind: str, snippet: str, reason: str, state: _State) -> None:
        construct = UnsupportedConstruct(kind=kind, original_snippet=snippet, reason=reason)
        if construct in state.unsupported:
            return
        logger.warning("Unsupported %s: %s", kind, reason)
        state.unsupported.append(construct)

    def _map_type(self, port: _Port, state: _State) -> DataType | None:
        try:
            return base.map_type(SourceTechnology.INFORMATICA, port.datatype)
        except base.UnknownLegacyType as err:
            self._unsupported("unknown_data_type", _snippet(port.elem), str(err), state)
            return None

    def _column(self, port: _Port, state: _State) -> Column | None:
        data_type = self._map_type(port, state)
        if data_type is None:
            return None
        precision = None
        if data_type in (DataType.STRING, DataType.DECIMAL) and port.precision:
            precision = int(port.precision)
        scale = int(port.scale) if data_type == DataType.DECIMAL and port.scale else None
        return Column(
            name=port.name.lower(),
            data_type=data_type,
            precision=precision,
            scale=scale,
            nullable=port.nullable,
            is_key=port.is_key,
        )

    def _types_of(self, trans: _Trans, state: _State) -> dict[str, DataType | None]:
        return {p.name.lower(): self._map_type(p, state) for p in trans.ports}

    @staticmethod
    def _known(types: dict[str, DataType | None]) -> dict[str, DataType]:
        return {k: v for k, v in types.items() if v is not None}

    def _translate(
        self,
        original: str,
        types: dict[str, DataType | None],
        state: _State,
        *,
        normalized: frozenset[str] = frozenset(),
        lookups: dict[str, str] | None = None,
    ) -> Expression:
        state.runtime_refs.update(informatica_expr.runtime_spellings(original))
        return informatica_expr.translate(
            original, self._known(types), normalized_columns=normalized, lookups=lookups
        )

    def _set_stream(
        self, inst: str, stream: str | None, parents: list[str], ports: list[str], state: _State
    ) -> None:
        state.streams[inst] = stream
        state.ports_out[inst] = ports
        if stream is not None and stream not in state.ancestors:
            anc: set[str] = set()
            for parent in parents:
                anc.add(parent)
                anc.update(state.ancestors.get(parent, set()))
            state.ancestors[stream] = anc

    def _input_stream(self, inst: str, upstream: list[str], state: _State) -> str | None:
        """The single IR stream an instance reads; None if an upstream is unsupported or they diverge."""
        streams: list[str] = []
        for up in upstream:
            if self._instances[up].trans_type == _ROUTER and state.streams.get(up) is not None:
                stream = state.router_feeds.get(inst)
            else:
                stream = state.streams.get(up)
            if stream is None:
                logger.warning("Skipping %r: upstream instance %r is unsupported or skipped", inst, up)
                return None
            if stream not in streams:
                streams.append(stream)
        if not streams:
            return None
        if len(streams) == 1:
            return streams[0]
        covering = [s for s in streams if all(o == s or o in state.ancestors.get(s, set()) for o in streams)]
        if len(covering) != 1:
            self._unsupported(
                "multiple_inputs",
                f"{inst} <- {', '.join(upstream)}",
                "Instance reads several independent streams; only a Joiner can combine streams",
                state,
            )
            return None
        return covering[0]

    def _log_link(self, src: str, src_port: str, dst: str, dst_port: str, state: _State) -> None:
        key = (src, src_port, dst, dst_port)
        if key in state.logged_links:
            return
        state.logged_links.add(key)
        logger.warning("inferred link %s.%s -> %s.%s (abridged connectors)", src, src_port, dst, dst_port)

    def _router_base(self, router: str, src_field: str, state: _State) -> str:
        """Port name of a router output field (``POLICY_KEY1`` -> ``POLICY_KEY``)."""
        ports = state.ports_out.get(router, [])
        m = _ROUTER_FIELD.match(src_field)
        if m and m["base"] in ports:
            return m["base"]
        return src_field

    def _resolve_inputs(
        self, inst: str, ports: list[str], candidates: list[str], state: _State
    ) -> dict[str, tuple[str, str]]:
        """Map each input port of ``inst`` to ``(upstream instance, upstream port)``.

        Explicit connectors win; otherwise the same-named output port of exactly one candidate upstream is
        used and the inferred link is logged.
        """
        resolved: dict[str, tuple[str, str]] = {}
        for port in ports:
            conn = next((c for c in self._conns if c.dst == inst and c.dst_field == port), None)
            if conn is not None:
                src_port = conn.src_field
                if self._instances[conn.src].trans_type == _ROUTER:
                    src_port = self._router_base(conn.src, conn.src_field, state)
                resolved[port] = (conn.src, src_port)
                continue
            found = [u for u in candidates if port in state.ports_out.get(u, [])]
            if len(found) == 1:
                self._log_link(found[0], port, inst, port, state)
                resolved[port] = (found[0], port)
            elif not found:
                self._unsupported(
                    "unresolved_port",
                    f"{inst}.{port}",
                    f"Input port {inst}.{port} has no connector and no same-named upstream port",
                    state,
                )
            else:
                self._unsupported(
                    "ambiguous_link",
                    f"{inst}.{port} <- {', '.join(found)}",
                    f"Input port {inst}.{port} matches ports of several upstream instances",
                    state,
                )
        return resolved

    # ------------------------------------------------------------------ dispatch
    def _process(self, inst: _Instance, state: _State) -> None:
        if inst.trans is None:
            self._unsupported(
                slug(inst.trans_type) or "instance",
                inst.name,
                f"Instance '{inst.name}' references a definition that is not in the export",
                state,
            )
            self._set_stream(inst.name, None, [], [], state)
            return
        ttype = inst.trans.type
        if ttype == _SOURCE_DEF:
            self._source(inst, state)
            return
        if ttype == _TARGET_DEF:
            return  # built once the session is read
        if ttype == _LOOKUP and not self._upstream(inst.name):
            if inst.name not in self._lkp_users:
                logger.warning("Lookup %r is neither connected nor called with :LKP.", inst.name)
                state.rule(
                    f"Lookup '{inst.name}' is neither connected nor called with :LKP.; it has no effect."
                )
            return  # unconnected: built where it is called
        handler = {
            _SQ: self._source_qualifier,
            _AGG: self._aggregator,
            _JOINER: self._joiner,
            _LOOKUP: self._connected_lookup,
            _EXPRESSION: self._expression,
            _ROUTER: self._router,
        }.get(ttype)
        if handler is None:
            self._unsupported(
                slug(ttype) or "transformation",
                _snippet(inst.trans.elem),
                "Unsupported Informatica transformation",
                state,
            )
            self._set_stream(inst.name, None, [], [], state)
            return
        handler(inst, state)

    # ------------------------------------------------------------------ sources
    def _source(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        columns = [c for c in (self._column(p, state) for p in trans.ports) if c is not None]
        alias = f"src_{slug(trans.name)}"
        state.sources.append(
            DatasetRef(
                alias=alias,
                system=trans.elem.get("DBDNAME", ""),
                schema_name=trans.elem.get("OWNERNAME", ""),
                table=trans.name.lower(),
                columns=columns,
            )
        )
        state.port_types[inst.name] = self._types_of(trans, state)
        self._set_stream(inst.name, alias, [], [p.name for p in trans.ports], state)

    def _source_qualifier(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        upstream = self._upstream(inst.name)
        if len(upstream) != 1 or self._instances[upstream[0]].trans_type != _SOURCE_DEF:
            self._unsupported(
                "source_qualifier",
                _snippet(trans.elem),
                "Source Qualifier must read exactly one Source Definition",
                state,
            )
            self._set_stream(inst.name, None, [], [], state)
            return
        src = upstream[0]
        src_alias = state.streams.get(src)
        ports = [p.name for p in trans.ports if p.is_output]
        if src_alias is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        self._resolve_inputs(inst.name, [p.name for p in trans.ports if p.is_input], [src], state)
        state.port_types[inst.name] = self._types_of(trans, state)

        distinct = trans.attrs.get("Select Distinct", "NO").strip().upper()
        if distinct == "YES":
            self._unsupported(
                "select_distinct", _snippet(trans.elem), "Select Distinct is not supported", state
            )
            self._set_stream(inst.name, None, [], ports, state)
            return
        others = [k for k in ("User Defined Join", "Pre SQL", "Post SQL") if trans.attrs.get(k, "").strip()]
        query = trans.attrs.get("Sql Query", "").strip()
        filt = trans.attrs.get("Source Filter", "").strip()
        if others or (query and filt):
            reason = (
                f"Source Qualifier attributes not supported: {', '.join(others)}"
                if others
                else "Source Qualifier has both a Sql Query override and a Source Filter"
            )
            self._unsupported("source_qualifier", _snippet(trans.elem), reason, state)
            self._set_stream(inst.name, None, [], ports, state)
            return
        if query:
            dataset = next(d for d in state.sources if d.alias == src_alias)
            state.runtime_refs.update(m.upper() for m in _SQL_RUNTIME.findall(query))
            dataset.sql_override = Expression(
                original=trans.attrs["Sql Query"], dialect=ExpressionDialect.TSQL
            )
        if not filt:
            self._set_stream(inst.name, src_alias, [], ports, state)
            return
        step_id = slug(trans.name)
        condition = self._translate(trans.attrs["Source Filter"], state.port_types[src], state)
        state.steps.append(
            FilterStep(
                id=step_id,
                inputs=[src_alias],
                source_component=trans.name,
                description="Source Qualifier filter",
                condition=condition,
            )
        )
        self._set_stream(inst.name, step_id, [src_alias], ports, state)

    # ------------------------------------------------------------------ aggregator
    def _aggregator(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        upstream = self._upstream(inst.name)
        ports = [p.name for p in trans.ports if p.is_output]
        stream = self._input_stream(inst.name, upstream, state)
        if stream is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        self._resolve_inputs(inst.name, [p.name for p in trans.ports if p.is_input], upstream, state)
        types = self._types_of(trans, state)
        state.port_types[inst.name] = types
        group_by = [p.name.lower() for p in trans.ports if p.expressiontype == "GROUPBY"]
        aggregations: list[Aggregation] = []
        ok = True
        for port in trans.ports:
            if port.expressiontype == "GROUPBY" or not port.is_output:
                continue
            call = informatica_expr.aggregate_call(port.expression or "")
            if port.is_input or call is None:
                ok = False
                self._unsupported(
                    "aggregate_expression",
                    _snippet(port.elem),
                    f"Aggregator '{trans.name}' port {port.name} is not a single top-level aggregate call",
                    state,
                )
                continue
            function, inner = call
            aggregations.append(
                Aggregation(
                    output_column=port.name.lower(),
                    function=function.lower(),  # type: ignore[arg-type]
                    expression=self._translate(inner, types, state),
                )
            )
        if not ok or not group_by or not aggregations:
            if ok:
                self._unsupported(
                    "aggregator",
                    _snippet(trans.elem),
                    "Aggregator needs group-by ports and aggregates",
                    state,
                )
            self._set_stream(inst.name, None, [], ports, state)
            return
        step_id = slug(trans.name)
        state.steps.append(
            AggregateStep(
                id=step_id,
                inputs=[stream],
                source_component=trans.name,
                group_by=group_by,
                aggregations=aggregations,
            )
        )
        self._set_stream(inst.name, step_id, [stream], ports, state)

    # ------------------------------------------------------------------ joiner
    def _joiner(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        ports = [p.name for p in trans.ports if p.is_output]
        master_ports = [p.name for p in trans.ports if p.is_input and "MASTER" in p.porttype.split("/")]
        detail_ports = [p.name for p in trans.ports if p.is_input and p.name not in master_ports]
        masters = sorted({c.src for c in self._conns if c.dst == inst.name and c.dst_field in master_ports})
        details = sorted({c.src for c in self._conns if c.dst == inst.name and c.dst_field in detail_ports})
        join_type = _JOIN_TYPES.get(trans.attrs.get("Join Type", "").strip())
        condition_text = trans.attrs.get("Join Condition", "")
        if len(masters) != 1 or len(details) != 1 or join_type is None or not condition_text.strip():
            self._unsupported(
                "joiner",
                _snippet(trans.elem),
                "Joiner needs one master and one detail upstream, a supported Join Type and a condition",
                state,
            )
            self._set_stream(inst.name, None, [], ports, state)
            return
        master, detail = masters[0], details[0]
        detail_stream = self._input_stream(inst.name, [detail], state)
        master_stream = self._input_stream(inst.name, [master], state)
        if detail_stream is None or master_stream is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        resolved = self._resolve_inputs(inst.name, master_ports, [master], state)
        resolved.update(self._resolve_inputs(inst.name, detail_ports, [detail], state))
        types = self._types_of(trans, state)
        state.port_types[inst.name] = types
        condition = self._translate(condition_text, types, state)
        in_condition = set(condition.referenced_columns)
        renamed = [
            f"{port} is master port fed by {master_stream}.{resolved[port][1].lower()}"
            for port in master_ports
            if port in resolved and resolved[port][1] != port and port.lower() in in_condition
        ]
        sides = f"detail = {detail_stream} (left), master = {master_stream} (right)."
        note = "; ".join([*renamed, sides])
        notes = f"{condition.notes} {note}" if condition.notes else note
        condition = condition.model_copy(
            update={"spark_sql": None, "translation_status": TranslationStatus.PENDING, "notes": notes}
        )
        step_id = slug(trans.name)
        state.steps.append(
            JoinStep(
                id=step_id,
                inputs=[detail_stream, master_stream],
                source_component=trans.name,
                join_type=join_type,
                condition=condition,
            )
        )
        if trans.attrs.get("Join Type", "").strip() == "Master Outer Join":
            nulls = [p for p in master_ports if p.lower() not in in_condition]
            handled = self._null_handlers(nulls)
            text = (
                f"Joiner '{trans.name}' is a Master Outer Join: all DETAIL rows ({detail_stream}) are kept; "
                f"rows with no MASTER match ({master_stream}) get NULL {', '.join(nulls) or 'master columns'}"
            )
            if handled:
                text += f" (handled by {', '.join(handled)})"
            state.rule(text + ".")
        self._set_stream(inst.name, step_id, [detail_stream, master_stream], ports, state)

    def _null_handlers(self, columns: list[str]) -> list[str]:
        """Variable ports anywhere in the mapping whose expression tests one of ``columns`` with ISNULL."""
        found: list[str] = []
        for inst in sorted(self._instances.values(), key=lambda i: i.index):
            if inst.trans is None or inst.trans.type != _EXPRESSION:
                continue
            for port in inst.trans.ports:
                text = (port.expression or "").upper()
                if port.is_variable and any(f"ISNULL({c.upper()})" in text.replace(" ", "") for c in columns):
                    found.append(port.name)
        return found

    # ------------------------------------------------------------------ lookups
    def _lookup_table(self, trans: _Trans, state: _State) -> tuple[str, str] | None:
        """``(schema, table)`` of a lookup by unique catalog name match; records unsupported otherwise."""
        table = trans.attrs.get("Lookup table name", "").strip()
        matches = (
            []
            if self.catalog is None or not table
            else sorted(k for k in self.catalog.tables if k.split(".", 1)[1] == table.lower())
        )
        if len(matches) != 1:
            self._unsupported(
                "lookup",
                _snippet(trans.elem),
                f"Lookup table {table!r} matches {len(matches)} catalog tables "
                f"({', '.join(matches) or 'none'}); a unique match is required",
                state,
            )
            return None
        schema, name = matches[0].split(".", 1)
        state.rule(
            f"Lookup '{trans.name}' table {table} resolved to {schema}.{name} by unique name match in "
            "insurance_ddl.sql."
        )
        return schema, name

    def _lookup_common(
        self, trans: _Trans, state: _State
    ) -> tuple[str, list[tuple[str, str]], dict[str, str], Expression | None, str] | None:
        """Reference alias, ``[(lookup port, input port)]``, return columns, filter, multiple-match policy."""
        unsupported_attrs = [k for k in ("Lookup Sql Override",) if trans.attrs.get(k, "").strip()]
        connection = trans.attrs.get("Connection Information", "").strip()
        policy = _MULTIPLE_MATCH.get(trans.attrs.get("Lookup policy on multiple match", "").strip())
        pairs: list[tuple[str, str]] = []
        lookup_ports = {p.name for p in trans.ports if "LOOKUP" in p.porttype.split("/")}
        input_ports = {p.name for p in trans.ports if p.is_input}
        for part in re.split(r"\s+AND\s+", trans.attrs.get("Lookup condition", ""), flags=re.IGNORECASE):
            m = _LOOKUP_EQ.match(part)
            if m is None:
                pairs = []
                break
            a, b = m.group(1), m.group(2)
            if a in lookup_ports and b in input_ports:
                pairs.append((a, b))
            elif b in lookup_ports and a in input_ports:
                pairs.append((b, a))
            else:
                pairs = []
                break
        if unsupported_attrs or connection != "$Target" or policy is None or not pairs:
            self._unsupported(
                "lookup",
                _snippet(trans.elem),
                "Lookup needs Connection Information $Target, an equality Lookup condition on lookup/input "
                "ports, a supported multiple-match policy and no SQL override",
                state,
            )
            return None
        resolved = self._lookup_table(trans, state)
        if resolved is None:
            return None
        schema, table = resolved
        system = base.SCHEMA_SYSTEMS.get(schema)
        if system is None:
            self._unsupported("lookup", _snippet(trans.elem), f"No system known for schema {schema!r}", state)
            return None
        state.rule(base.SYSTEM_RULE)
        returns = {
            p.name.lower(): p.name.lower()
            for p in trans.ports
            if p.is_output and "LOOKUP" in p.porttype.split("/")
        }
        if not returns:
            self._unsupported("lookup", _snippet(trans.elem), "Lookup has no lookup/output port", state)
            return None
        alias = f"ref_{slug(table)}"
        columns = [
            c
            for c in (self._column(p, state) for p in trans.ports if "LOOKUP" in p.porttype.split("/"))
            if c is not None
        ]
        existing = next((r for r in state.references if r.alias == alias), None)
        if existing is None:
            state.references.append(
                DatasetRef(alias=alias, system=system, schema_name=schema, table=table, columns=columns)
            )
        filt_text = trans.attrs.get("Lookup Source Filter", "")
        reference_filter = None
        if filt_text.strip():
            catalog_cols = self.catalog.table(schema, table) if self.catalog else None
            types: dict[str, DataType | None] = {c.name.lower(): c.data_type for c in catalog_cols or []}
            reference_filter = self._translate(filt_text, types, state)
        return alias, pairs, returns, reference_filter, policy

    def _connected_lookup(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        ports = [p.name for p in trans.ports if p.is_output]
        if inst.name in self._lkp_users:
            self._unsupported(
                "lookup", _snippet(trans.elem), "Lookup is both connected and called with :LKP.", state
            )
            self._set_stream(inst.name, None, [], ports, state)
            return
        upstream = self._upstream(inst.name)
        stream = self._input_stream(inst.name, upstream, state)
        if stream is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        resolved = self._resolve_inputs(
            inst.name, [p.name for p in trans.ports if p.is_input], upstream, state
        )
        common = self._lookup_common(trans, state)
        if common is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        alias, pairs, returns, reference_filter, policy = common
        join_keys: dict[str, str] = {}
        for lookup_port, input_port in pairs:
            if input_port not in resolved:
                self._unsupported(
                    "lookup", _snippet(trans.elem), f"Lookup input port {input_port} is not fed", state
                )
                self._set_stream(inst.name, None, [], ports, state)
                return
            join_keys[resolved[input_port][1].lower()] = lookup_port.lower()
        state.port_types[inst.name] = self._types_of(trans, state)
        step_id = slug(trans.name)
        state.steps.append(
            LookupStep(
                id=step_id,
                inputs=[stream],
                source_component=trans.name,
                reference=alias,
                join_keys=join_keys,
                return_columns=returns,
                reference_filter=reference_filter,
                on_no_match=LookupNoMatchPolicy.RETURN_NULL,
                on_multiple_match=policy,  # type: ignore[arg-type]
            )
        )
        self._set_stream(inst.name, step_id, [stream], ports, state)

    def _unconnected_lookups(
        self, exp: _Trans, stream: str, state: _State
    ) -> tuple[str, dict[str, str]] | None:
        """Emit one unconnected LookupStep per distinct ``:LKP.`` call of an Expression (T10).

        Returns:
            The stream after the lookups and the ``lookups`` mapping for ``translate``; None if a call
            cannot be resolved.
        """
        uses: list[informatica_expr.LookupUse] = []
        for port in exp.ports:
            for use in informatica_expr.lookup_calls(port.expression or ""):
                if all(u.key != use.key for u in uses):
                    uses.append(use)
        providers: dict[str, str] = {}
        per_lookup: dict[str, int] = {}
        for use in uses:
            inst = self._instances.get(use.name)
            trans = None if inst is None else inst.trans
            if trans is None or trans.type != _LOOKUP or self._upstream(use.name):
                self._unsupported(
                    "lookup", use.text, f":LKP.{use.name} does not name an unconnected Lookup instance", state
                )
                return None
            common = self._lookup_common(trans, state)
            if common is None:
                return None
            alias, pairs, _, reference_filter, policy = common
            inputs = [p.name for p in trans.ports if p.is_input]
            returns = [p.name.lower() for p in trans.ports if "RETURN" in p.porttype.split("/")]
            if len(use.arg_columns) != len(inputs) or None in use.arg_columns or len(returns) != 1:
                self._unsupported(
                    "lookup",
                    use.text,
                    "Unconnected lookup call needs one plain column argument per input port "
                    "and one return port",
                    state,
                )
                return None
            arg_by_input = dict(zip(inputs, use.arg_columns, strict=True))
            join_keys = {str(arg_by_input[inp]): lkp.lower() for lkp, inp in pairs}
            count = per_lookup.get(use.name, 0) + 1
            per_lookup[use.name] = count
            step_id = slug(use.name) if count == 1 else f"{slug(use.name)}_{count}"
            if count > 1:
                state.rule(
                    f"Lookup '{use.name}' is called with different arguments; "
                    f"call {use.text} is step {step_id}."
                )
            state.steps.append(
                LookupStep(
                    id=step_id,
                    inputs=[stream],
                    source_component=trans.name,
                    description=f"unconnected lookup called from {exp.name} as {use.text}",
                    reference=alias,
                    join_keys=join_keys,
                    return_columns={returns[0]: returns[0]},
                    reference_filter=reference_filter,
                    on_no_match=LookupNoMatchPolicy.RETURN_NULL,
                    on_multiple_match=policy,  # type: ignore[arg-type]
                    is_unconnected=True,
                )
            )
            state.ancestors[step_id] = {stream} | state.ancestors.get(stream, set())
            stream = step_id
            providers[use.key] = f"{step_id}.{returns[0]}"
        return stream, providers

    # ------------------------------------------------------------------ expression
    def _expression(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        ports = [p.name for p in trans.ports if p.is_output]
        upstream = self._upstream(inst.name)
        stream = self._input_stream(inst.name, upstream, state)
        if stream is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        self._resolve_inputs(inst.name, [p.name for p in trans.ports if p.is_input], upstream, state)
        types = self._types_of(trans, state)
        state.port_types[inst.name] = types
        start = stream
        lookups = self._unconnected_lookups(trans, stream, state)
        if lookups is None:
            self._set_stream(inst.name, None, [], ports, state)
            return
        stream, providers = lookups
        inputs = {p.name.lower() for p in trans.ports if p.is_input}
        normalized = frozenset(
            col
            for p in trans.ports
            if p.is_output and p.expression
            for col in informatica_expr.normalized_columns(p.expression)
            if col in inputs
        )
        base_id = slug(trans.name)
        for port in trans.ports:
            if not port.is_variable:
                continue
            step_id = f"{base_id}_{slug(port.name)}"
            state.steps.append(
                DerivedColumnStep(
                    id=step_id,
                    inputs=[stream],
                    source_component=trans.name,
                    description="variable port (evaluated before outputs, in declared order)",
                    mappings=[self._mapping(port, types, normalized, providers, state)],
                    keep_input_columns=True,
                )
            )
            state.ancestors[step_id] = {stream} | state.ancestors.get(stream, set())
            stream = step_id
        mappings: list[ColumnMapping] = []
        for port in trans.ports:
            if not port.is_output:
                continue
            text = (port.expression or "").strip()
            if port.is_input and (not text or text.upper() == port.name.upper()):
                continue  # pass-through
            if not text:
                self._unsupported(
                    "expression", _snippet(port.elem), f"Output port {port.name} has no expression", state
                )
                continue
            mappings.append(self._mapping(port, types, normalized, providers, state))
        if mappings:
            state.steps.append(
                DerivedColumnStep(
                    id=base_id,
                    inputs=[stream],
                    source_component=trans.name,
                    mappings=mappings,
                    keep_input_columns=True,
                )
            )
            state.ancestors[base_id] = {stream} | state.ancestors.get(stream, set())
            stream = base_id
        if stream == start:
            logger.warning("Expression %r has only pass-through ports; no step emitted", inst.name)
        state.streams[inst.name] = stream
        state.ports_out[inst.name] = ports

    def _mapping(
        self,
        port: _Port,
        types: dict[str, DataType | None],
        normalized: frozenset[str],
        providers: dict[str, str],
        state: _State,
    ) -> ColumnMapping:
        return ColumnMapping(
            target_column=port.name.lower(),
            expression=self._translate(
                port.expression or "", types, state, normalized=normalized, lookups=providers
            ),
            target_type=types.get(port.name.lower()),
        )

    # ------------------------------------------------------------------ router
    def _router(self, inst: _Instance, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        upstream = self._upstream(inst.name)
        stream = self._input_stream(inst.name, upstream, state)
        if stream is None:
            self._set_stream(inst.name, None, [], [], state)
            return
        declared = [p.name for p in trans.ports if p.is_input]
        if declared:
            ports = declared
        else:
            ports = []
            for up in upstream:
                ports.extend(p for p in state.ports_out.get(up, []) if p not in ports)
        self._resolve_inputs(inst.name, ports, upstream, state)
        types: dict[str, DataType | None] = {}
        for up in upstream:
            for k, v in state.port_types.get(up, {}).items():
                types.setdefault(k, v)
        types.update(self._types_of(trans, state))
        state.port_types[inst.name] = types

        groups = [
            (int(g.get("ORDER", "0")), g.get("NAME", ""), g.get("TYPE", "").upper(), g.get("EXPRESSION"))
            for g in trans.elem.findall("GROUP")
        ]
        outputs = sorted((g for g in groups if g[2].startswith("OUTPUT")), key=lambda g: g[0])
        defaults = [g for g in outputs if g[2] == "OUTPUT/DEFAULT"]
        named = [g for g in outputs if g[2] == "OUTPUT"]
        if not named or len(defaults) > 1 or any(not (g[3] or "").strip() for g in named):
            self._unsupported(
                "router", _snippet(trans.elem), "Router needs named output groups with conditions", state
            )
            self._set_stream(inst.name, None, [], ports, state)
            return
        step_id = slug(trans.name)
        route_groups = [
            RouteGroup(name=g[1].lower(), condition=self._translate(g[3] or "", types, state)) for g in named
        ]
        default = defaults[0][1] if defaults else None
        state.steps.append(
            RouterStep(
                id=step_id,
                inputs=[stream],
                source_component=trans.name,
                groups=route_groups,
                default_group=default.lower() if default else None,
                first_match_only=False,
            )
        )
        self._set_stream(inst.name, step_id, [stream], ports, state)
        self._wire_router(trans, step_id, [g[1] for g in outputs], default, named, state)

    def _wire_router(
        self,
        trans: _Trans,
        step_id: str,
        output_names: list[str],
        default: str | None,
        named: list[tuple[int, str, str, str | None]],
        state: _State,
    ) -> None:
        """Decode connector suffixes into router groups and record which group each consumer reads."""
        ports = state.ports_out[trans.name]
        consumers: dict[str, set[str]] = {}
        fields: dict[str, list[tuple[str, str]]] = {}
        for conn in sorted(
            (c for c in self._conns if c.src == trans.name), key=lambda c: (c.dst, c.dst_field, c.src_field)
        ):
            decodings = []
            for split in range(1, len(conn.src_field)):
                port, suffix = conn.src_field[:split], conn.src_field[split:]
                if port in ports and suffix.isdigit() and 1 <= int(suffix) <= len(output_names):
                    decodings.append((port, suffix))
            if len(decodings) != 1:
                self._unsupported(
                    "router_wiring",
                    f"{conn.src}.{conn.src_field} -> {conn.dst}.{conn.dst_field}",
                    f"Router connector field {conn.src_field} does not decode to one port and group",
                    state,
                )
                continue
            port, suffix = decodings[0]
            group = output_names[int(suffix) - 1]
            consumers.setdefault(conn.dst, set()).add(group)
            fields.setdefault(conn.dst, []).append((conn.src_field, suffix))
        for dst, groups in sorted(consumers.items()):
            if len(groups) != 1:
                self._unsupported(
                    "router_wiring",
                    f"{trans.name} -> {dst}",
                    f"Instance {dst} reads several router groups: {sorted(groups)}",
                    state,
                )
                consumers[dst] = set()
        fed = {next(iter(g)) for g in consumers.values() if g}
        if default is not None and default in fed:
            unfed = [g[1] for g in named if g[1] not in fed]
            default_consumers = sorted(d for d, g in consumers.items() if g == {default})
            if len(unfed) > 1:
                self._unsupported(
                    "router_wiring",
                    f"{trans.name}: default group {default} -> {', '.join(default_consumers)}",
                    f"Default group is consumed while several named groups have no consumer: {unfed}",
                    state,
                )
                for dst in default_consumers:
                    consumers[dst] = set()
            elif len(unfed) == 1:
                target_group = unfed[0]
                rows = (
                    "which receives no rows"
                    if self._complementary(named)
                    else "which receives only rows that match no other group"
                )
                for dst in default_consumers:
                    consumers[dst] = {target_group}
                    used = fields[dst]
                    suffixes = ", ".join(sorted({s for _, s in used}))
                    names = ", ".join(f for f, _ in used)
                    logger.warning(
                        "Router %s: connector suffix %s (%s) selects default group %s; wired to %s instead",
                        trans.name,
                        suffixes,
                        names,
                        default,
                        target_group,
                    )
                    state.rule(
                        f"Router '{trans.name}': connector suffix '{suffixes}' ({names}) would select the "
                        f"default group {default} under PowerCenter numbering, {rows}; wired to group "
                        f"{target_group} {self._description_basis(dst)}."
                    )
        for dst, groups in consumers.items():
            if groups:
                state.router_feeds[dst] = f"{step_id}.{next(iter(groups)).lower()}"

    @staticmethod
    def _complementary(named: list[tuple[int, str, str, str | None]]) -> bool:
        conds = [" ".join((g[3] or "").upper().split()) for g in named]
        return any(f"NOT {a}" == b for a in conds for b in conds)

    def _description_basis(self, consumer: str) -> str:
        """Clause of the mapping description that names a target reached from ``consumer``."""
        reached: list[str] = []
        todo = [consumer]
        while todo:
            name = todo.pop(0)
            if self._instances[name].trans_type == _TARGET_DEF and name not in reached:
                reached.append(name)
            todo.extend(self._downstream(name))
        description = self._mapping_elem.get("DESCRIPTION", "")
        clauses = [
            c.strip()
            for c in description.split(";")
            if any(t.upper() in c.upper() for t in reached) and c.strip()
        ]
        if clauses:
            return f"per the mapping description ({'; '.join(clauses)})"
        return "per the connector topology (no mapping description clause names its target)"

    # ------------------------------------------------------------------ targets + session (T15)
    def _apply_session(self, mapping_name: str, state: _State) -> None:
        sessions = [s for s in self._folder.iter("SESSION") if s.get("MAPPINGNAME") == mapping_name]
        targets = [
            i for i in sorted(self._instances.values(), key=lambda i: i.index) if i.trans_type == _TARGET_DEF
        ]
        if len(sessions) != 1:
            self._unsupported(
                "session_writer",
                mapping_name,
                f"Expected one SESSION for mapping {mapping_name}, found {len(sessions)}; load mode unknown",
                state,
            )
            return
        session = sessions[0]
        treat = next(
            (
                a.get("VALUE", "")
                for a in session.findall("ATTRIBUTE")
                if a.get("NAME") == "Treat source rows as"
            ),
            "Insert",
        )
        if treat.strip() != "Insert":
            self._unsupported(
                "session_writer",
                _snippet(session),
                f"Session treats source rows as {treat!r}; only Insert is supported",
                state,
            )
            return
        described: list[str] = []
        for inst in targets:
            writer = next(
                (
                    e
                    for e in session.findall("SESSIONEXTENSION")
                    if e.get("TYPE") == "WRITER" and e.get("SINSTANCENAME") == inst.name
                ),
                None,
            )
            attrs = {} if writer is None else {a.get("NAME", ""): a.get("VALUE", "") for a in writer}
            mode = None
            if writer is not None and not any(
                attrs.get(k, "NO").strip().upper() == "YES"
                for k in ("Update as Update", "Update as Insert", "Update else Insert", "Delete")
            ):
                truncate = attrs.get("Truncate target table option", "NO").strip().upper()
                insert = attrs.get("Insert", "NO").strip().upper()
                if insert == "YES" and truncate == "YES":
                    mode = LoadMode.FULL_OVERWRITE
                    described.append(f"{inst.name} truncate+insert (full_overwrite)")
                elif insert == "YES" and truncate == "NO":
                    mode = LoadMode.APPEND
                    described.append(f"{inst.name} insert without truncate (append)")
            if mode is None:
                self._unsupported(
                    "session_writer",
                    inst.name if writer is None else _snippet(writer),
                    f"Session writer for target {inst.name} is missing or not plain insert / truncate+insert",
                    state,
                )
                continue
            self._target(inst, mode, state)
        if described:
            state.rule(
                f"Load modes from session '{session.get('NAME', '')}' (not the mapping): "
                f"{', '.join(described)}."
            )

    def _feed(self, target: str, column: str, upstream: str, state: _State) -> _Feed | None:
        conn = next((c for c in self._conns if c.dst == target and c.dst_field == column), None)
        if conn is not None:
            port = conn.src_field
            if self._instances[conn.src].trans_type == _ROUTER:
                port = self._router_base(conn.src, conn.src_field, state)
            return _Feed(column, conn.src, port, explicit=True, passed_through=False)
        current, passed = upstream, False
        while True:
            available = state.ports_out.get(current, [])
            found = [p for p in (column, f"O_{column}") if p in available]
            if len(found) > 1:
                self._unsupported(
                    "ambiguous_target_column",
                    f"{target}.{column}",
                    f"Both {current}.{column} and {current}.O_{column} could feed {target}.{column}",
                    state,
                )
                return None
            if found:
                self._log_link(current, found[0], target, column, state)
                return _Feed(column, current, found[0], explicit=False, passed_through=passed)
            ups = self._upstream(current)
            if self._instances[current].trans_type not in _PASSTHROUGH_TYPES or len(ups) != 1:
                return None
            current, passed = ups[0], True

    def _target(self, inst: _Instance, mode: LoadMode, state: _State) -> None:
        trans = inst.trans
        assert trans is not None
        upstream = self._upstream(inst.name)
        if len(upstream) != 1:
            self._unsupported(
                "target",
                _snippet(trans.elem),
                f"Target {inst.name} needs exactly one upstream instance",
                state,
            )
            return
        stream = self._input_stream(inst.name, upstream, state)
        if stream is None:
            return
        schema = trans.elem.get("OWNERNAME", "")
        system = base.SCHEMA_SYSTEMS.get(schema.lower())
        if system is None:
            self._unsupported("target", _snippet(trans.elem), f"No system known for schema {schema!r}", state)
            return
        state.rule(base.SYSTEM_RULE)
        feeds: list[_Feed] = []
        for port in trans.ports:
            feed = self._feed(inst.name, port.name, upstream[0], state)
            if feed is None:
                self._unsupported(
                    "unmapped_target_column",
                    f"{inst.name}.{port.name}",
                    f"No port {port.name} or O_{port.name} upstream of target {inst.name}",
                    state,
                )
                continue
            feeds.append(feed)
        renamed = [f for f in feeds if f.port != f.column]
        if renamed:
            state.rule(
                f"Target '{inst.name}' columns fed by differently named ports (IR targets carry no column "
                f"mapping): {', '.join(f'{f.column.lower()} <- {f.port.lower()}' for f in renamed)}."
            )
        passed = [f for f in feeds if f.passed_through]
        if passed:
            renames = [f"{f.port} for {f.column}" for f in passed if f.port != f.column]
            extra = f" ({', '.join(renames)})" if renames else ""
            state.rule(
                f"{inst.name} requires {', '.join(f.column for f in passed)}, which {upstream[0]} does not "
                f"output; passed through by name from upstream{extra} because connectors are abridged (X5)."
            )
        columns = [c for c in (self._column(p, state) for p in trans.ports) if c is not None]
        dataset = DatasetRef(
            alias=f"tgt_{slug(trans.name)}",
            system=system,
            schema_name=schema,
            table=trans.name.lower(),
            columns=columns,
        )
        state.targets.append(
            (inst.name, Target(input=stream, dataset=dataset, load_strategy=LoadStrategy(mode=mode)), feeds)
        )

    # ------------------------------------------------------------------ data quality
    def _origin_expression(self, feed: _Feed) -> str | None:
        """Expression text that defines a fed port, following router pass-through."""
        current, port = feed.instance, feed.port
        while True:
            inst = self._instances[current]
            if inst.trans_type == _ROUTER:
                ups = self._upstream(current)
                if len(ups) != 1:
                    return None
                current = ups[0]
                continue
            if inst.trans is None or inst.trans.type != _EXPRESSION:
                return None
            p = inst.trans.port(port)
            if p is None or not p.expression or p.expression.strip().upper() == port.upper():
                return None
            return p.expression

    def _dq_rules(self, state: _State) -> list[DataQualityRule]:
        rules: list[DataQualityRule] = []
        for _, target, feeds in state.targets:
            alias = target.dataset.alias
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
            by_column = {f.column.lower(): f for f in feeds}
            for column in target.dataset.columns:
                feed = by_column.get(column.name)
                text = None if feed is None else self._origin_expression(feed)
                domain = None if text is None else informatica_expr.string_literal_domain(text)
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
        return rules
