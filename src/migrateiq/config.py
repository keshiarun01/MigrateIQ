"""Settings read from the environment. No credentials live here."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

DEFAULT_RUN_TS = "2026-01-01T00:00:00"
DEFAULT_CATALOG = "sample_artifacts/schema/insurance_ddl.sql"


class Settings(BaseModel):
    """Runtime settings for MigrateIQ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ts: datetime
    catalog_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``MIGRATEIQ_*`` environment variables, with documented defaults."""
        return cls(
            run_ts=datetime.fromisoformat(os.environ.get("MIGRATEIQ_RUN_TS", DEFAULT_RUN_TS)),
            catalog_path=Path(os.environ.get("MIGRATEIQ_CATALOG", DEFAULT_CATALOG)),
        )
