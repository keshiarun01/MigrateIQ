"""Logging configuration for the CLI. Logs go to stderr so stdout stays machine-readable."""

from __future__ import annotations

import logging
import sys


def configure(verbose: bool = False) -> None:
    """Send log records to stderr at WARNING, or INFO when ``verbose``."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
