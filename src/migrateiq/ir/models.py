"""
MigrateIQ Intermediate Representation (IR) — v1.1 (see docs/ir_v1_1_proposal.md).

The IR is the single contract of the system:

    legacy artifact --(parser, deterministic)--> MigrationUnit (IR)
    MigrationUnit --(translator agent)--> Expression.spark_sql filled in
    MigrationUnit --(codegen agent)--> PySpark job + tests + DQ suite

Rules:
  * Parsers NEVER call an LLM. They capture the original expression text verbatim
    and set translation_status=PENDING when they cannot translate deterministically.
  * Agents NEVER see raw legacy XML/SQL when generating code — only the IR.
  * Steps form a DAG. Every step consumes named inputs (source aliases or earlier
    step ids) and produces exactly one named output (its id).
  * Anything the parser does not understand goes to `unsupported_constructs`,
    never silently dropped.

Requires: pydantic>=2.6
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

IR_VERSION = "1.1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class SourceTechnology(str, Enum):
    SSIS = "ssis"
    INFORMATICA = "informatica"
    TSQL = "tsql"


class ExpressionDialect(str, Enum):
    """Language the ORIGINAL expression was written in."""
    SSIS_EXPR = "ssis_expr"            # e.g. (DT_WSTR,50)UPPER(TRIM(x))
    INFORMATICA_EXPR = "informatica_expr"  # e.g. IIF(ISNULL(x), 'N', 'Y')
    TSQL = "tsql"
    SPARK_SQL = "spark_sql"            # already target dialect


class TranslationStatus(str, Enum):
    PENDING = "pending"                # parser could not translate
    DETERMINISTIC = "deterministic"    # translated by sqlglot / rule table
    LLM = "llm"                        # translated by translator agent
    VERIFIED = "verified"              # passed reconciliation
    NEEDS_REVIEW = "needs_review"      # failed retries / low confidence


class DataType(str, Enum):
    STRING = "string"
    INT = "int"
    BIGINT = "bigint"
    DECIMAL = "decimal"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BINARY = "binary"


class JoinType(str, Enum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    # NOTE: Informatica "Master Outer" == keep all DETAIL rows == LEFT join
    # with detail on the left. Parsers must normalize, never pass it through.


class LookupNoMatchPolicy(str, Enum):
    RETURN_NULL = "return_null"        # Informatica default, SSIS "ignore failure"
    DEFAULT_VALUE = "default_value"
    REDIRECT = "redirect"              # SSIS "redirect rows to no match output"
    FAIL = "fail"                      # SSIS "fail component"


class LoadMode(str, Enum):
    FULL_OVERWRITE = "full_overwrite"
    APPEND = "append"
    MERGE_UPSERT = "merge_upsert"      # SCD1 / T-SQL MERGE
    SCD2 = "scd2"


class DQRuleType(str, Enum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    ACCEPTED_VALUES = "accepted_values"
    RANGE = "range"
    REFERENTIAL = "referential"        # FK exists in another dataset
    ROW_COUNT_MATCH = "row_count_match"
    CUSTOM_SQL = "custom_sql"


class Severity(str, Enum):
    WARN = "warn"
    ERROR = "error"


class MigrationPath(str, Enum):
    TEMPLATE = "template"              # low complexity: template-only codegen
    LLM = "llm"                        # agent-assisted codegen
    MANUAL = "manual"                  # route to human


class RunParameterKind(str, Enum):
    """IR v1.1 (G1): kind of runtime value a job takes as an argument."""
    RUN_TS = "run_ts"                  # pinned run timestamp (MIGRATEIQ_RUN_TS)
    WATERMARK = "watermark"            # incremental-load high-water mark


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False,
                              validate_assignment=True)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
class Column(_Base):
    name: str
    data_type: DataType
    precision: Optional[int] = None
    scale: Optional[int] = None
    nullable: bool = True
    is_key: bool = False
    description: Optional[str] = None


class Expression(_Base):
    """An expression in its original dialect plus its Spark SQL translation."""
    original: str = Field(..., description="Verbatim text from the legacy artifact")
    dialect: ExpressionDialect
    spark_sql: Optional[str] = Field(
        None, description="Spark SQL expression usable in F.expr(); None until translated")
    translation_status: TranslationStatus = TranslationStatus.PENDING
    referenced_columns: list[str] = Field(default_factory=list)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _status_consistent(self) -> "Expression":
        translated = {TranslationStatus.DETERMINISTIC, TranslationStatus.LLM,
                      TranslationStatus.VERIFIED}
        if self.translation_status in translated and not self.spark_sql:
            raise ValueError(
                f"status={self.translation_status.value} but spark_sql is empty "
                f"for expression: {self.original!r}")
        return self


class DatasetRef(_Base):
    """A physical source or target table/query."""
    alias: str = Field(..., description="Unique name used by steps to reference this dataset")
    system: str = Field(..., description="Logical system, e.g. 'PolicyAdmin' or 'EDW'")
    schema_name: str
    table: Optional[str] = None
    sql_override: Optional[Expression] = Field(
        None, description="Source query override (SSIS SqlCommand / Informatica SQ override)")
    columns: list[Column] = Field(default_factory=list)

    @model_validator(mode="after")
    def _table_or_query(self) -> "DatasetRef":
        if not self.table and not self.sql_override:
            raise ValueError(f"Dataset '{self.alias}' needs a table or a sql_override")
        return self


class ColumnMapping(_Base):
    """target_column = expression(source_columns)."""
    target_column: str
    expression: Expression
    target_type: Optional[DataType] = None


# ---------------------------------------------------------------------------
# Transformation steps (discriminated union on `type`)
# ---------------------------------------------------------------------------
class _StepBase(_Base):
    id: str = Field(..., description="Unique step id; also the name of this step's output")
    inputs: list[str] = Field(..., min_length=1)
    source_component: Optional[str] = Field(
        None, description="Original component/transformation name, for lineage & audit")
    description: Optional[str] = None


class FilterStep(_StepBase):
    type: Literal["filter"] = "filter"
    condition: Expression


class DerivedColumnStep(_StepBase):
    """SSIS Derived Column / Data Conversion, Informatica Expression, SQL SELECT list."""
    type: Literal["derived_column"] = "derived_column"
    mappings: list[ColumnMapping] = Field(..., min_length=1)
    keep_input_columns: bool = True


class JoinStep(_StepBase):
    type: Literal["join"] = "join"
    join_type: JoinType
    condition: Expression

    @model_validator(mode="after")
    def _two_inputs(self) -> "JoinStep":
        if len(self.inputs) != 2:
            raise ValueError(f"Join '{self.id}' needs exactly 2 inputs [left, right]")
        return self


class LookupStep(_StepBase):
    """Connected or unconnected lookup. inputs=[main]; reference given separately."""
    type: Literal["lookup"] = "lookup"
    reference: str = Field(..., description="Alias of the reference DatasetRef")
    join_keys: dict[str, str] = Field(..., min_length=1,
                                      description="{input_column: reference_column}")
    return_columns: dict[str, str] = Field(..., min_length=1,
                                           description="{output_name: reference_column}")
    reference_filter: Optional[Expression] = None   # e.g. is_current = 1
    on_no_match: LookupNoMatchPolicy = LookupNoMatchPolicy.RETURN_NULL
    default_values: dict[str, str] = Field(default_factory=dict)
    on_multiple_match: Literal["first", "last", "any", "error"] = "any"
    is_unconnected: bool = False

    def output_names(self) -> list[str]:
        """IR v1.1 (G5): a redirect lookup emits '<id>.match' and '<id>.no_match'."""
        if self.on_no_match == LookupNoMatchPolicy.REDIRECT:
            return [f"{self.id}.match", f"{self.id}.no_match"]
        return [self.id]


class Aggregation(_Base):
    output_column: str
    function: Literal["sum", "count", "count_distinct", "min", "max", "avg", "first", "last"]
    expression: Expression


class AggregateStep(_StepBase):
    type: Literal["aggregate"] = "aggregate"
    group_by: list[str] = Field(..., min_length=1)
    aggregations: list[Aggregation] = Field(..., min_length=1)


class DeduplicateStep(_StepBase):
    """ROW_NUMBER() OVER (PARTITION BY .. ORDER BY ..) = 1, SSIS Sort w/ remove dups."""
    type: Literal["deduplicate"] = "deduplicate"
    partition_by: list[str] = Field(..., min_length=1)
    order_by: list[str] = Field(default_factory=list,
                                description="e.g. ['updated_at DESC']")


class RouteGroup(_Base):
    name: str
    condition: Expression


class RouterStep(_StepBase):
    """Informatica Router / SSIS Conditional Split. Each group becomes output '<id>.<group>'."""
    type: Literal["router"] = "router"
    groups: list[RouteGroup] = Field(..., min_length=1)
    default_group: Optional[str] = "default"
    first_match_only: bool = Field(
        True, description="SSIS Conditional Split = True; Informatica Router = False (row can go to many groups)")

    def output_names(self) -> list[str]:
        names = [f"{self.id}.{g.name}" for g in self.groups]
        if self.default_group:
            names.append(f"{self.id}.{self.default_group}")
        return names


class UnionStep(_StepBase):
    type: Literal["union"] = "union"
    by_name: bool = True
    distinct: bool = False


Step = Annotated[
    Union[FilterStep, DerivedColumnStep, JoinStep, LookupStep, AggregateStep,
          DeduplicateStep, RouterStep, UnionStep],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Targets, load strategy, quality
# ---------------------------------------------------------------------------
class WatermarkSpec(_Base):
    """IR v1.1 (G4): where an incremental watermark lives and how it advances."""
    store_table: str = Field(..., description="e.g. 'etl.watermark'")
    job_name: str = Field(..., description="Key row in store_table, e.g. 'usp_Load_FactPremium'")
    column: str = Field(..., description="Source column whose MAX becomes the new watermark")
    initial_value: str = Field(..., description="Value used when no row exists, e.g. '1900-01-01'")
    computed_from: str = Field(
        ..., description="Step output MAX(column) is taken from (after joins/filters, see X4)")
    advance_only_if_rows: bool = True


class LoadStrategy(_Base):
    mode: LoadMode
    business_keys: list[str] = Field(default_factory=list)
    watermark_column: Optional[str] = None
    # SCD2
    tracked_columns: list[str] = Field(default_factory=list)
    effective_from_column: Optional[str] = None
    effective_to_column: Optional[str] = None
    current_flag_column: Optional[str] = None
    surrogate_key_column: Optional[str] = None
    # IR v1.1
    update_condition: Optional[Expression] = Field(
        None, description="G2: MERGE 'WHEN MATCHED AND (...)' condition, verbatim (merge_upsert only)")
    insert_only_columns: list[str] = Field(default_factory=list, description="G3")
    update_only_columns: list[str] = Field(default_factory=list, description="G3")
    watermark: Optional[WatermarkSpec] = Field(None, description="G4")
    end_date_sentinel: Optional[str] = Field(
        None, description="G10: effective_to value of the current version, e.g. '9999-12-31 00:00:00'")
    surrogate_key_strategy: Optional[Literal["identity", "max_plus_row_number", "hash"]] = Field(
        None, description="G10")

    @model_validator(mode="after")
    def _mode_requirements(self) -> "LoadStrategy":
        if self.mode in (LoadMode.MERGE_UPSERT, LoadMode.SCD2) and not self.business_keys:
            raise ValueError(f"{self.mode.value} requires business_keys")
        if self.mode == LoadMode.SCD2:
            missing = [f for f in ("tracked_columns", "effective_from_column",
                                   "effective_to_column", "current_flag_column")
                       if not getattr(self, f)]
            if missing:
                raise ValueError(f"scd2 requires: {', '.join(missing)}")
        if self.update_condition is not None and self.mode != LoadMode.MERGE_UPSERT:
            raise ValueError(f"update_condition is only valid for merge_upsert, not {self.mode.value}")
        return self


class Target(_Base):
    input: str = Field(..., description="Step id (or router output '<id>.<group>') feeding this target")
    dataset: DatasetRef
    load_strategy: LoadStrategy
    partition_by: list[str] = Field(default_factory=list)


class DataQualityRule(_Base):
    dataset_alias: str
    column: Optional[str] = None
    rule_type: DQRuleType
    params: dict[str, Union[str, int, float, list[str]]] = Field(default_factory=dict)
    severity: Severity = Severity.ERROR
    derived_from: Optional[str] = Field(
        None, description="Where the rule came from, e.g. 'NOT NULL constraint' or 'business rule BR-3'")


class UnsupportedConstruct(_Base):
    kind: str = Field(..., description="e.g. 'trigger', 'cursor', 'ole_db_command', 'script_task'")
    original_snippet: str
    reason: str
    suggested_pattern: Optional[str] = None


class RunParameter(_Base):
    """IR v1.1 (G1): a runtime value the generated job takes as an argument."""
    name: str = Field(..., description="Job argument name, e.g. 'run_ts'")
    kind: RunParameterKind
    data_type: DataType = DataType.TIMESTAMP
    legacy_refs: list[str] = Field(
        default_factory=list, description="Legacy spellings, e.g. ['GETDATE()', '@[User::RunStartTime]']")


class PostLoadAction(_Base):
    """IR v1.1 (G6): work done after the load, e.g. job_log rows or MERGE OUTPUT counts."""
    kind: Literal["job_log", "row_counts"]
    on: Literal["success", "failure", "always"]
    target_table: str
    columns: dict[str, str] = Field(default_factory=dict,
                                    description="{target_column: source description}")
    original_snippet: str


class SourceArtifact(_Base):
    technology: SourceTechnology
    file_path: str
    object_name: str = Field(..., description="Package / mapping / procedure name")
    sha256: str
    parser_version: str
    parsed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Complexity(_Base):
    score: int = Field(..., ge=0, le=100)
    factors: dict[str, int] = Field(default_factory=dict,
                                    description="e.g. {'lookups': 2, 'pending_expressions': 5}")
    recommended_path: MigrationPath


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------
class MigrationUnit(_Base):
    """One legacy object (package/mapping/proc) → one IR document."""
    ir_version: str = IR_VERSION
    unit_id: str
    name: str
    source_artifact: SourceArtifact
    sources: list[DatasetRef] = Field(..., min_length=1)
    references: list[DatasetRef] = Field(default_factory=list,
                                         description="Lookup reference datasets")
    steps: list[Step] = Field(default_factory=list)
    targets: list[Target] = Field(..., min_length=1)
    dq_rules: list[DataQualityRule] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    unsupported_constructs: list[UnsupportedConstruct] = Field(default_factory=list)
    complexity: Optional[Complexity] = None
    # IR v1.1
    parameters: list[RunParameter] = Field(default_factory=list, description="G1")
    post_load_actions: list[PostLoadAction] = Field(default_factory=list, description="G6")

    # ---- graph validation -------------------------------------------------
    @model_validator(mode="after")
    def _validate_dag(self) -> "MigrationUnit":
        aliases = [d.alias for d in self.sources + self.references]
        if len(aliases) != len(set(aliases)):
            raise ValueError(f"Duplicate dataset aliases: {aliases}")

        step_ids = [s.id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError(f"Duplicate step ids: {step_ids}")
        if set(step_ids) & set(aliases):
            raise ValueError("Step ids must not collide with dataset aliases")

        ref_aliases = {r.alias for r in self.references}
        available: set[str] = {s.alias for s in self.sources}

        # steps must be listed in executable (topological) order
        for step in self.steps:
            for i, inp in enumerate(step.inputs):
                # G7: the right-hand input of a join may be a reference dataset
                if isinstance(step, JoinStep) and i == 1 and inp in ref_aliases:
                    continue
                if inp not in available:
                    raise ValueError(
                        f"Step '{step.id}' input '{inp}' is not defined before it. "
                        f"Available: {sorted(available)}")
            if isinstance(step, LookupStep) and step.reference not in ref_aliases:
                raise ValueError(f"Lookup '{step.id}' reference '{step.reference}' "
                                 f"not in references")
            if isinstance(step, (RouterStep, LookupStep)):
                available.update(step.output_names())
            else:
                available.add(step.id)

        for t in self.targets:
            if t.input not in available:
                raise ValueError(f"Target '{t.dataset.alias}' input '{t.input}' undefined")
            wm = t.load_strategy.watermark
            if wm is not None and wm.computed_from not in available:
                raise ValueError(f"Target '{t.dataset.alias}' watermark computed_from "
                                 f"'{wm.computed_from}' undefined")
        return self

    # ---- helpers used by agents & reporting -------------------------------
    def pending_expressions(self) -> list[Expression]:
        """Every expression still needing translation (what the translator agent sees)."""
        found: list[Expression] = []

        def walk(obj: object) -> None:
            if isinstance(obj, Expression):
                if obj.translation_status in (TranslationStatus.PENDING,
                                              TranslationStatus.NEEDS_REVIEW):
                    found.append(obj)
            elif isinstance(obj, BaseModel):
                for name in type(obj).model_fields:
                    walk(getattr(obj, name))
            elif isinstance(obj, (list, tuple)):
                for item in obj:
                    walk(item)
            elif isinstance(obj, dict):
                for item in obj.values():
                    walk(item)

        walk(self)
        return found

    def is_ready_for_codegen(self) -> bool:
        if self.complexity is not None and \
                self.complexity.recommended_path == MigrationPath.MANUAL:   # G9
            return False
        return not self.pending_expressions() and not any(
            u for u in self.unsupported_constructs if not u.suggested_pattern)
