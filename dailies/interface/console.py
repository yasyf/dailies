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
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from rich.console import RenderableType

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
