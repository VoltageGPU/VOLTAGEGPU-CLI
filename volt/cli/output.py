"""Console helpers shared by every command. ASCII only: legacy Windows consoles
render anything else as replacement characters."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Optional

import click
from rich.console import Console
from rich.table import Table


def _width(stream: Any) -> Optional[int]:
    """Real width on a terminal; 140 columns when piped, instead of rich's 80."""
    try:
        return None if stream.isatty() else 140
    except (AttributeError, ValueError):
        return 140


console = Console(highlight=False, soft_wrap=False, width=_width(sys.stdout))
err_console = Console(stderr=True, highlight=False, width=_width(sys.stderr))


def money(value: Optional[float], digits: int = 2) -> str:
    return "-" if value is None else f"${value:,.{digits}f}"


def echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def table(title: Optional[str], columns: Iterable[tuple], rows: Iterable[Iterable[Any]]) -> None:
    t = Table(title=title, title_justify="left", header_style="bold", box=None, pad_edge=False)
    for col in columns:
        name, *opts = col
        t.add_column(name, **(opts[0] if opts else {}))
    for row in rows:
        t.add_row(*["-" if v is None or v == "" else str(v) for v in row])
    console.print(t)


def info(message: str) -> None:
    console.print(message)


def hint(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def warn(message: str) -> None:
    err_console.print(f"[yellow]Warning:[/yellow] {message}")


def fail(message: str, code: int = 1) -> "click.exceptions.Exit":
    err_console.print(f"[red]Error:[/red] {message}")
    raise click.exceptions.Exit(code)


def status_style(status: Optional[str]) -> str:
    s = (status or "").lower()
    if s in ("running", "ready"):
        return "green"
    if s in ("stopped", "terminated", "failed", "deleted"):
        return "red"
    return "yellow"


def is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False
