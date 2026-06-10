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
    ImageBlock,
    ManualTrigger,
    RunStatus,
    TaskStatus,
    TextBlock,
    Trigger,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue
    from textual.widget import Widget

    from dailies.documents import Task, TaskState, Workflow, WorkflowState
    from dailies.models import WorkflowDraft

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
        case EventTrigger(event_type=event_type, event_key=event_key):
            return f"event {event_type}/{event_key}"
        case ManualTrigger():
            return "manual"


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
        Static(excerpt(card.prompt), classes="prompt", markup=False),
        rules_list(card.rules),
        title=card.name if card.version is None else f"{card.name} v{card.version}",
        classes="flow-box workflow" if card.status is None else f"flow-box workflow {card.status}",
    )


def state_box(ddl: str) -> Vertical:
    match parse_ddl(ddl):
        case ():
            return flow_box(Static(ddl_syntax(ddl), classes="ddl"), title="state", classes="flow-box state")
        case summaries:
            return flow_box(
                *(
                    widget
                    for summary in summaries
                    for widget in (
                        Static(f"🗄 {summary.table}", classes="table-name", markup=False),
                        *(
                            Static(f"{column.name} {column.type}", classes="column", markup=False)
                            for column in summary.columns
                        ),
                    )
                ),
                ddl_block(ddl),
                title="state",
                classes="flow-box state",
            )


def workflow_flow(card: WorkflowCard) -> Vertical:
    return Vertical(
        *(
            (Horizontal(*(trigger_box(trigger) for trigger in card.triggers), classes="flow-triggers"), flow_arrow())
            if card.triggers
            else ()
        ),
        workflow_box(card),
        flow_arrow(),
        state_box(card.ddl),
        flow_arrow(),
        Static("📣 notify user", classes="flow-terminus"),
        classes="flow",
    )


def state_table(data: Mapping[str, JsonValue]) -> Table:
    table = Table("key", "value")
    for key, value in data.items():
        table.add_row(key, repr(value))
    return table


def state_widgets(title: str, ddl: str, state: WorkflowState | TaskState | None) -> list[Static]:
    heading = f"{title}  (no state)" if state is None else f"{title}  (updated {state.updated_at:%Y-%m-%d %H:%M})"
    return [
        Static(heading, markup=False),
        Static(ddl_syntax(ddl)),
        Static(state_table(state.data if state else {})),
    ]


def task_header(task: Task) -> Vertical:
    return Vertical(
        Horizontal(Label(task.name, classes="task-name", markup=False), status_badge(task.status)),
        Static(task.definition.description, markup=False),
        Static(task.definition.prompt, classes="muted", markup=False),
        *(() if task.shared_ddl is None else (ddl_block(task.shared_ddl),)),
        classes="task-header",
    )
