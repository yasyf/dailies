"""Pure widget factories and DDL parsing for the Textual UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Collapsible, Label, Static

from dailies.models import (
    Block,
    CronTrigger,
    EventTrigger,
    Firing,
    ImageBlock,
    ManualTrigger,
    RunStatus,
    TaskStatus,
    TextBlock,
    Trigger,
)
from dailies.state import MAX_ROWS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue
    from textual.widget import Widget

    from dailies.documents import Task, Workflow
    from dailies.models import WorkflowDraft
    from dailies.state import StateDump

ROW_PREVIEW = 5
CREATE_TABLE = re.compile(
    r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["'`\[]?(?P<table>[\w.]+)["'`\]]?\s*\((?P<body>.*)\)""",
    re.IGNORECASE | re.DOTALL,
)
CONSTRAINT_KEYWORDS = frozenset({"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"})
TRIGGER_GLYPHS: Mapping[str, str] = {"cron": "⏰", "event": "⚡", "manual": "✋"}
RUN_STATUS_STYLES: Mapping[RunStatus, str] = {
    "pending": "dim",
    "running": "yellow",
    "succeeded": "green",
    "failed": "bold red",
    "stopped": "dim",
}


def split_columns(body: str) -> list[str]:
    parts: list[str] = []
    depth, start = 0, 0
    for index, char in enumerate(body):
        match char:
            case "(":
                depth += 1
            case ")":
                depth -= 1
            case "," if depth == 0:
                parts.append(body[start:index])
                start = index + 1
    return [part for chunk in [*parts, body[start:]] if (part := chunk.strip())]


def column_def(line: str) -> ColumnDef | None:
    name, *rest = line.split()
    if name.upper() in CONSTRAINT_KEYWORDS:
        return None
    return ColumnDef(name=name.strip("\"'`[]"), type=" ".join(rest))


@dataclass(frozen=True, slots=True)
class ColumnDef:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class TableSummary:
    table: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True, slots=True)
class WorkflowCard:
    """Render-ready view of a workflow, built from a persisted document or an interview draft."""

    name: str
    summary: str
    prompt: str
    rules: tuple[str, ...]
    ddl: str
    triggers: tuple[Trigger, ...]
    version: int | None = None
    status: TaskStatus | None = None

    @classmethod
    def from_workflow(cls, workflow: Workflow) -> WorkflowCard:
        return cls(
            name=workflow.name,
            summary=workflow.definition.summary,
            prompt=workflow.definition.prompt,
            rules=tuple(workflow.definition.rules),
            ddl=workflow.ddl,
            triggers=tuple(workflow.triggers),
            version=workflow.version,
            status=workflow.status,
        )

    @classmethod
    def from_draft(cls, draft: WorkflowDraft) -> WorkflowCard:
        return cls(
            name=draft.name,
            summary=draft.summary,
            prompt=draft.prompt,
            rules=tuple(draft.rules),
            ddl=draft.ddl,
            triggers=tuple(draft.triggers),
        )


def parse_statement(statement: str) -> TableSummary | None:
    if (match := CREATE_TABLE.search(statement)) is None:
        return None
    columns = tuple(col for line in split_columns(match["body"]) if (col := column_def(line)) is not None)
    return TableSummary(table=match["table"], columns=columns)


def parse_ddl(ddl: str) -> tuple[TableSummary, ...]:
    return tuple(summary for statement in ddl.split(";") if (summary := parse_statement(statement)) is not None)


def render_trigger(trigger: Trigger) -> str:
    match trigger:
        case CronTrigger(cron_expression=expr, timezone=tz):
            return f"cron {expr} ({tz})"
        case EventTrigger(source=source, event=event, key=key):
            return f"event {source} {event}/{key}"
        case ManualTrigger():
            return "manual"


def render_firing(firing: Firing) -> str:
    match firing:
        case Firing(occurrence_ids=[]):
            return render_trigger(firing.trigger)
        case Firing(trigger=trigger, occurrence_ids=ids):
            return f"{render_trigger(trigger)} ({len(ids)} msgs)"


def excerpt(text: str, *, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_badge(status: TaskStatus) -> Label:
    return Label(status, classes=f"badge {status}")


def run_status_text(status: RunStatus) -> Text:
    return Text(status, style=RUN_STATUS_STYLES[status])


def ddl_syntax(ddl: str) -> Syntax:
    return Syntax(ddl, "sql", background_color="default", word_wrap=True)


def ddl_block(ddl: str) -> Collapsible:
    return Collapsible(Static(ddl_syntax(ddl), classes="ddl"), title="DDL", collapsed=True)


def prompt_block(prompt: str) -> Collapsible:
    return Collapsible(Static(prompt, classes="prompt", markup=False), title="prompt", collapsed=True)


def rules_list(rules: Sequence[str]) -> Static:
    return Static("\n".join(f"• {rule}" for rule in rules) or "—", markup=False)


def block_widget(block: Block) -> Static:
    match block:
        case TextBlock(text=text):
            return Static(text, markup=False)
        case ImageBlock(url=url):
            return Static(f"[image] {url}", markup=False)


def flow_box(*children: Widget, title: str, classes: str) -> Vertical:
    box = Vertical(*children, classes=classes)
    box.border_title = title
    return box


def flow_arrow() -> Static:
    return Static("│\n▼", classes="flow-arrow")


def trigger_box(trigger: Trigger) -> Vertical:
    return flow_box(
        Static(f"{TRIGGER_GLYPHS[trigger.kind]} {render_trigger(trigger)}", markup=False),
        title="trigger",
        classes="flow-box trigger",
    )


def workflow_box(card: WorkflowCard) -> Vertical:
    return flow_box(
        *(() if card.status is None else (status_badge(card.status),)),
        Static(card.summary, classes="summary", markup=False),
        rules_list(card.rules),
        prompt_block(card.prompt),
        title=card.name if card.version is None else f"{card.name} v{card.version}",
        classes="flow-box workflow" if card.status is None else f"flow-box workflow {card.status}",
    )


def schema_widgets(ddl: str) -> list[Static]:
    match parse_ddl(ddl):
        case ():
            return [Static(ddl_syntax(ddl), classes="ddl")]
        case summaries:
            return [
                widget
                for summary in summaries
                for widget in (
                    Static(f"🗄 {summary.table}", classes="table-name", markup=False),
                    Static(
                        ", ".join(f"{column.name} {column.type}" for column in summary.columns) or "—",
                        classes="columns",
                        markup=False,
                    ),
                )
            ]


def state_box(ddl: str, state: StateDump | None) -> Vertical:
    return flow_box(
        *schema_widgets(ddl),
        *(
            ()
            if state is None
            else (Static(state_table(name, rows, limit=ROW_PREVIEW)) for name, rows in state.items())
        ),
        title="state",
        classes="flow-box state",
    )


def workflow_flow(card: WorkflowCard, state: StateDump | None = None) -> Vertical:
    return Vertical(
        *(
            (Horizontal(*(trigger_box(trigger) for trigger in card.triggers), classes="flow-triggers"), flow_arrow())
            if card.triggers
            else ()
        ),
        workflow_box(card),
        flow_arrow(),
        state_box(card.ddl, state),
        flow_arrow(),
        Static("📣 notify user", classes="flow-terminus"),
        classes="flow",
    )


def state_table(name: str, rows: Sequence[Mapping[str, JsonValue]], *, limit: int | None = None) -> Table | Text:
    if not rows:
        return Text(f"{name} (no rows)", style="dim")
    shown = rows if limit is None else rows[:limit]
    hidden = len(rows) - len(shown)
    over_cap = "+" if len(rows) >= MAX_ROWS else ""
    table = Table(*rows[0], title=name, caption=f"… +{hidden}{over_cap} more" if hidden else None)
    for row in shown:
        table.add_row(*(repr(value) for value in row.values()))
    return table


def state_widgets(title: str, ddl: str | None, state: StateDump) -> list[Static]:
    return [
        Static(title, markup=False),
        *(() if ddl is None else (Static(ddl_syntax(ddl)),)),
        *(Static(state_table(name, rows)) for name, rows in state.items()),
    ]


def task_header(task: Task) -> Vertical:
    return Vertical(
        Horizontal(Label(task.name, classes="task-name", markup=False), status_badge(task.status)),
        Static(task.definition.description, markup=False),
        prompt_block(task.definition.prompt),
        *(() if task.shared_ddl is None else (ddl_block(task.shared_ddl),)),
        classes="task-header",
    )
