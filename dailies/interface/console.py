"""Textual-free rich presentation kit for the `dly` Click CLI."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import click
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from dailies.state import MAX_ROWS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from pydantic import JsonValue
    from rich.console import RenderableType

    from dailies.models import RunStatus

PALETTE: Mapping[str, str] = {
    "success": "green",
    "warning": "#ffaf00",
    "error": "bold red",
    "muted": "grey50",
    "foreground": "grey85",
    "primary": "blue",
    "secondary": "bright_blue",
    "accent": "purple",
    "surface": "grey23",
}
THEME = Theme(PALETTE)
CONFIDENCE_STYLES: Mapping[str, str] = {"high": "success", "medium": "warning", "low": "muted"}
SOURCE_GLYPHS: Mapping[str, str] = {"email": "📧", "web": "🌐", "user": "✍", "account": "⚙"}
TRIGGER_GLYPHS: Mapping[str, str] = {"cron": "⏰", "event": "⚡", "manual": "✋", "workflow": "⛓"}
RUN_STATUS_STYLES: Mapping[RunStatus, str] = {
    "pending": "dim",
    "running": "yellow",
    "succeeded": "green",
    "failed": "bold red",
    "stopped": "dim",
}
CELL_PREVIEW = 40


def glyph_prefix(glyph: str) -> str:
    return f"{glyph} " if glyph else ""


class Glyphs:
    SPINNER: ClassVar[str] = "dots"
    OK: ClassVar[str] = "✓"
    PENDING: ClassVar[str] = "◦"
    SEARCH: ClassVar[str] = "🔍"
    WEB: ClassVar[str] = "🌐"
    EMAIL: ClassVar[str] = "📧"
    USER: ClassVar[str] = "✍"
    MACHINE: ClassVar[str] = "⚙"
    PROFILE: ClassVar[str] = "👤"
    SAVE: ClassVar[str] = "💾"
    SCHEDULE: ClassVar[str] = "⏰"
    SUCCESS: ClassVar[str] = "✅"
    WARN: ClassVar[str] = "⚠"
    ERROR: ClassVar[str] = "✗"


@dataclass(frozen=True, slots=True)
class KvRow:
    label: str
    value: str
    note: str | None = None
    value_style: str = "foreground"
    glyph: str = ""


def console() -> Console:
    """Build a themed console bound to the current stdout stream (never cached)."""
    stream = click.get_text_stream("stdout")
    return Console(
        theme=THEME,
        file=stream,
        highlight=False,
        soft_wrap=False,
        width=None if stream.isatty() else 120,
    )


def title_style(style: str) -> Style:
    return Style.parse(f"bold {PALETTE[style]}")


def section(title: str, *, glyph: str = "", style: str = "primary") -> Rule:
    return Rule(Text(f"{glyph_prefix(glyph)}{title}", style=title_style(style)), align="left", style=style)


def panel(body: RenderableType, *, title: str = "", glyph: str = "", style: str = "primary") -> Panel:
    return Panel(
        body,
        title=Text(f"{glyph_prefix(glyph)}{title}", style=title_style(style)) if title else None,
        title_align="left",
        border_style=style,
    )


def kv_table(rows: Sequence[KvRow], *, label_style: str = "muted") -> Group:
    return Group(
        *(
            renderable
            for row in rows
            for renderable in (
                Text.assemble((f"{glyph_prefix(row.glyph)}{row.label}: ", label_style), (row.value, row.value_style)),
                *(() if row.note is None else (Text(f"  {row.note}", style="muted"),)),
            )
        )
    )


def success(msg: str, *, glyph: str = Glyphs.SUCCESS) -> Text:
    return Text(f"{glyph_prefix(glyph)}{msg}", style="success")


def warn(msg: str) -> Text:
    return Text(f"{Glyphs.WARN} {msg}", style="warning")


def error(msg: str) -> Text:
    return Text(f"{Glyphs.ERROR} {msg}", style="error")


def step(msg: str, *, glyph: str = "•") -> Text:
    return Text(f"{glyph_prefix(glyph)}{msg}", style="primary")


@contextmanager
def status(con: Console, msg: str) -> Iterator[None]:
    if con.is_terminal:
        with con.status(Text(msg, style="muted"), spinner=Glyphs.SPINNER):
            yield
    else:
        con.print(Text(msg, style="muted"))
        yield


def confirm(prompt: str, *, abort: bool = False, default: bool = False, glyph: str = "") -> bool:
    return click.confirm(f"{glyph_prefix(glyph)}{prompt}", abort=abort, default=default)


def excerpt(text: str, *, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_status_text(status: RunStatus) -> Text:
    return Text(status, style=RUN_STATUS_STYLES[status])


def ddl_syntax(ddl: str) -> Syntax:
    return Syntax(ddl, "sql", background_color="default", word_wrap=True)


def state_table(name: str, rows: Sequence[Mapping[str, JsonValue]], *, limit: int | None = None) -> Table | Text:
    if not rows:
        return Text(f"{name} (no rows)", style="dim")
    shown = rows if limit is None else rows[:limit]
    hidden = len(rows) - len(shown)
    over_cap = "+" if len(rows) >= MAX_ROWS else ""
    table = Table(*rows[0], title=name, caption=f"… +{hidden}{over_cap} more" if hidden else None)
    for row in shown:
        table.add_row(
            *(repr(value) if limit is None else excerpt(repr(value), limit=CELL_PREVIEW) for value in row.values())
        )
    return table
