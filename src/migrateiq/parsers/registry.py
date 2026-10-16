"""Single entry point: detect an artifact's technology, parse it, and score it."""

from __future__ import annotations

import logging
from pathlib import Path

from migrateiq.inventory.scanner import detect
from migrateiq.ir.models import MigrationUnit, SourceTechnology
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.complexity import score
from migrateiq.parsers.informatica import InformaticaParser
from migrateiq.parsers.ssis import SsisParser
from migrateiq.parsers.tsql import TsqlParser

logger = logging.getLogger(__name__)


def parse_artifact(path: Path, catalog: Catalog) -> MigrationUnit:
    """Parse one legacy artifact with the parser for its technology and set ``complexity``.

    Args:
        path: Artifact file (.dtsx, PowerCenter .xml, or T-SQL procedure/trigger .sql).
        catalog: Table catalog built from the DDL.

    Returns:
        The parsed unit with ``complexity`` filled in.

    Raises:
        ValueError: If the file is not a recognized artifact, or the parser cannot build a unit.
    """
    detected = detect(path)
    if detected is None:
        raise ValueError(
            f"{path}: not a recognized legacy artifact (SSIS .dtsx, PowerCenter XML, T-SQL proc/trigger)"
        )
    technology, object_type = detected
    logger.info("parsing %s as %s %s", path, technology.value, object_type)
    if technology == SourceTechnology.SSIS:
        unit = SsisParser(catalog).parse(path)
    elif technology == SourceTechnology.INFORMATICA:
        unit = InformaticaParser(catalog).parse(path)
    else:
        unit = TsqlParser(catalog).parse(path)
    unit.complexity = score(unit)
    return unit
