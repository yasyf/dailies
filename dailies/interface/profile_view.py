"""Profile-specific rich compositions: the static review panel and the live mining dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

from rich.console import Group
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from dailies.interface.console import (
    CONFIDENCE_STYLES,
    SOURCE_GLYPHS,
    Glyphs,
    KvRow,
    kv_table,
    panel,
    section,
    success,
)
from dailies.profile import AccountSource, EmailSource, UserSource, WebSource, describe
from dailies.tools.profile import FactRecorded, FieldRecorded, LoyaltyRecorded, MerchantRecorded

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from typing import Self

    from rich.console import Console, RenderableType

    from dailies.profile import Profile, Source, Sourced
    from dailies.tools.profile import DraftEvent


def scalar_row(label: str, sourced: Sourced[str]) -> KvRow:
    return KvRow(
        label=label,
        value=sourced.value,
        note=describe(sourced.source),
        value_style=CONFIDENCE_STYLES[sourced.confidence],
        glyph=SOURCE_GLYPHS[sourced.source.kind],
    )


def section_block(title: str, rows: Sequence[KvRow]) -> tuple[RenderableType, ...]:
    return (section(title), kv_table(rows)) if rows else ()


def tracked_value(profile: Profile, field: str) -> Sourced[str] | None:
    match field.split("."):
        case ["partner", sub]:
            return getattr(profile.partner, sub)
        case [name]:
            return getattr(profile, name)


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def event_source(event: DraftEvent) -> Source:
    match event:
        case FieldRecorded(value=value):
            return value.source
        case FactRecorded(fact=fact):
            return fact.source
        case LoyaltyRecorded(program=program):
            return program.member_number.source
        case MerchantRecorded(merchant=merchant):
            return merchant.source


def event_activity(event: DraftEvent) -> str:
    match event_source(event):
        case EmailSource(sender=sender):
            return f"{Glyphs.EMAIL} {sender}"
        case WebSource(url=url):
            return f"{Glyphs.WEB} {url}"
        case AccountSource():
            return f"{Glyphs.MACHINE} account"
        case UserSource():
            return f"{Glyphs.USER} you"


def event_summary(event: DraftEvent) -> str:
    match event:
        case FieldRecorded(field=scalar, value=value):
            return f"{scalar}: {value.value}"
        case FactRecorded(fact=fact):
            return f"{fact.label}: {fact.value}"
        case LoyaltyRecorded(program=program):
            return f"{program.program} ({program.kind})"
        case MerchantRecorded(merchant=merchant):
            return merchant.name


@dataclass(slots=True)
class MiningState:
    started_at: float
    actions: int = 0
    activity: str = ""
    profile: Profile | None = None


@dataclass(slots=True)
class MiningDashboard:
    """Live `rich` dashboard for `dly profile init`: a header that animates while fields populate.

    Drives a `rich.Live` on a TTY, refreshing a header (spinner, elapsed time, discovered count,
    current activity) over a panel of tracked fields that flip from `◦` to `✓` as the agent records
    them. Off a TTY it prints one plain line per record and a persistent summary on exit. Pass
    `on_event` as the `discover_profile` listener.
    """

    TRACKED_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "email",
        "timezone",
        "phone",
        "imessage_handle",
        "home_address",
        "birthday",
        "employer",
        "role",
        "partner.email",
        "partner.phone",
        "partner.imessage_handle",
    )

    con: Console
    title: str = "Mining your profile"
    state: MiningState = field(init=False, default_factory=lambda: MiningState(started_at=monotonic()))
    _live: Live | None = field(init=False, default=None)

    def on_event(self, event: DraftEvent) -> None:
        self.state.actions += 1
        self.state.profile = event.profile
        self.state.activity = event_activity(event)
        if not self.con.is_terminal:
            self.con.print(Text(f"{Glyphs.OK} {event_summary(event)}", style="success"))

    def __enter__(self) -> Self:
        if self.con.is_terminal:
            self._live = Live(self, console=self.con, refresh_per_second=12.5, transient=False)
            self._live.__enter__()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
        self.con.print(
            success(
                f"Discovered {self.state.actions} signals in {mmss(monotonic() - self.state.started_at)}.",
                glyph=Glyphs.PROFILE,
            )
        )

    def __rich__(self) -> RenderableType:
        state = self.state
        profile = state.profile
        header = Table.grid(padding=(0, 1))
        header.add_row(
            Text(f"{Glyphs.PROFILE} {self.title}", style="bold primary"),
            Spinner(Glyphs.SPINNER, style="accent"),
            Text(mmss(monotonic() - state.started_at), style="muted"),
            Text(f"{state.actions} discovered", style="secondary"),
            Text(state.activity or f"{Glyphs.SEARCH} starting…", style="muted"),
        )
        rows = [
            KvRow(label=field, value=sourced.value, value_style=CONFIDENCE_STYLES[sourced.confidence], glyph=Glyphs.OK)
            if profile is not None and (sourced := tracked_value(profile, field)) is not None
            else KvRow(label=field, value="—", value_style="muted", glyph=Glyphs.PENDING)
            for field in self.TRACKED_FIELDS
        ]
        counters = Text(
            f"{len(profile.merchants) if profile else 0} merchants  "
            f"{len(profile.loyalty_programs) if profile else 0} loyalty  "
            f"{len(profile.facts) if profile else 0} facts",
            style="muted",
        )
        return Group(header, panel(Group(kv_table(rows), counters), title="fields"))


def profile_panel(profile: Profile) -> RenderableType:
    """Compose the styled review panel for a profile, preserving each value's provenance text."""
    identity = kv_table(
        [
            scalar_row("name", profile.name),
            scalar_row("email", profile.email),
            scalar_row("timezone", profile.timezone),
            *(
                scalar_row(name, sourced)
                for name in ("phone", "imessage_handle", "home_address", "birthday", "employer", "role")
                if (sourced := getattr(profile, name)) is not None
            ),
        ]
    )
    partner = [
        scalar_row(sub, sourced)
        for sub in ("email", "phone", "imessage_handle")
        if (sourced := getattr(profile.partner, sub)) is not None
    ]
    loyalty = [
        KvRow(
            label=f"{p.program} ({p.kind}{f', {p.status_tier}' if p.status_tier else ''})",
            value=p.member_number.value,
            note=describe(p.member_number.source),
            value_style=CONFIDENCE_STYLES[p.member_number.confidence],
            glyph=SOURCE_GLYPHS[p.member_number.source.kind],
        )
        for p in profile.loyalty_programs
    ]
    merchants = [
        KvRow(
            label=m.name,
            value=f"{m.category}{f', {m.cadence}' if m.cadence else ''}",
            note=describe(m.source),
            glyph=SOURCE_GLYPHS[m.source.kind],
        )
        for m in profile.merchants
    ]
    facts = [
        KvRow(
            label=f.label,
            value=f.value,
            note=describe(f.source),
            value_style=CONFIDENCE_STYLES[f.confidence],
            glyph=SOURCE_GLYPHS[f.source.kind],
        )
        for f in profile.facts
    ]
    return panel(
        Group(
            section("identity"),
            identity,
            section(f"partner: {profile.partner.name}"),
            *((kv_table(partner),) if partner else ()),
            *section_block("loyalty programs", loyalty),
            *section_block("merchants", merchants),
            *section_block("facts", facts),
        ),
        title="Profile",
        glyph=Glyphs.PROFILE,
    )
