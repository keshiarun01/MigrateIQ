"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root."""
    return REPO_ROOT


@pytest.fixture
def artifacts_dir() -> Path:
    """Return the read-only legacy fixtures directory."""
    return REPO_ROOT / "sample_artifacts"
