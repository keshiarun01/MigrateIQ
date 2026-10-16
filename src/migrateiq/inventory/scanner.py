"""Discover legacy artifacts and detect their technology."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from migrateiq.ir.models import SourceTechnology
from migrateiq.parsers.base import sha256_file

logger = logging.getLogger(__name__)

ObjectType = Literal["package", "mapping", "procedure", "trigger"]

_TSQL_OBJECT = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?(PROCEDURE|PROC|TRIGGER)\b", re.IGNORECASE)


class ArtifactInfo(BaseModel):
    """One discovered legacy artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    technology: SourceTechnology
    object_type: ObjectType
    sha256: str


def detect(path: Path) -> tuple[SourceTechnology, ObjectType] | None:
    """Detect the technology and object type of a file.

    Args:
        path: File to inspect.

    Returns:
        ``(technology, object_type)``, or ``None`` if the file is not a migratable artifact
        (e.g. DDL-only SQL, READMEs).
    """
    suffix = path.suffix.lower()
    if suffix == ".dtsx":
        return SourceTechnology.SSIS, "package"
    if suffix == ".xml":
        if b"<POWERMART" in path.read_bytes():
            return SourceTechnology.INFORMATICA, "mapping"
        return None
    if suffix == ".sql":
        match = _TSQL_OBJECT.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            kind = match.group(1).upper()
            return SourceTechnology.TSQL, "trigger" if kind == "TRIGGER" else "procedure"
    return None


def scan(root: Path) -> list[ArtifactInfo]:
    """Recursively find legacy artifacts under ``root``.

    Args:
        root: Directory to scan.

    Returns:
        Artifacts sorted by path.
    """
    found: list[ArtifactInfo] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        detected = detect(path)
        if detected is None:
            logger.debug("skipping non-artifact %s", path)
            continue
        technology, object_type = detected
        found.append(
            ArtifactInfo(path=path, technology=technology, object_type=object_type, sha256=sha256_file(path))
        )
    return found
