"""MigrateIQ command-line interface. The only module allowed to write to the console."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from migrateiq.config import Settings
from migrateiq.ir.io import dump_unit
from migrateiq.logging_setup import configure
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.registry import parse_artifact

app = typer.Typer(help="Migrate legacy ETL (SSIS, Informatica, T-SQL) to PySpark via a deterministic IR.")


@app.callback()
def main() -> None:
    """MigrateIQ."""


@app.command()
def parse(
    path: Annotated[Path, typer.Argument(help="Legacy artifact: .dtsx, PowerCenter .xml, or T-SQL .sql")],
    catalog: Annotated[
        Path | None, typer.Option(help="DDL file for table metadata (default: $MIGRATEIQ_CATALOG)")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Write the IR JSON here instead of stdout")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log INFO to stderr")] = False,
) -> None:
    """Parse one legacy artifact and print its IR (MigrationUnit) as JSON."""
    configure(verbose)
    catalog_path = catalog or Settings.from_env().catalog_path
    if not path.is_file():
        typer.echo(f"error: artifact not found: {path}", err=True)
        raise typer.Exit(1)
    if not catalog_path.is_file():
        typer.echo(f"error: catalog not found: {catalog_path} (use --catalog or MIGRATEIQ_CATALOG)", err=True)
        raise typer.Exit(1)
    try:
        unit = parse_artifact(path, Catalog.from_ddl(catalog_path))
    except ValueError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from None
    text = dump_unit(unit)
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)
