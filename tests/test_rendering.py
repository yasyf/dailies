from __future__ import annotations

from uuid import uuid4

import pytest

from dailies.interface.rendering import (
    ColumnDef,
    TableSummary,
    WorkflowCard,
    excerpt,
    parse_ddl,
    render_firing,
    render_trigger,
    run_status_text,
    state_table,
)
from dailies.models import (
    CronExpr,
    CronTrigger,
    EventTrigger,
    Firing,
    ManualTrigger,
    PromptStr,
    RunStatus,
    SchemaStr,
    Trigger,
    WorkflowDefinition,
    WorkflowDraft,
    WorkflowId,
)
from tests.fakes import FakeWorkflow

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        pytest.param(
            "CREATE TABLE sent (day TEXT, count INT)",
            (TableSummary("sent", (ColumnDef("day", "TEXT"), ColumnDef("count", "INT"))),),
            id="single-table",
        ),
        pytest.param(
            "CREATE TABLE a (x TEXT);\nCREATE TABLE b (y INT);",
            (
                TableSummary("a", (ColumnDef("x", "TEXT"),)),
                TableSummary("b", (ColumnDef("y", "INT"),)),
            ),
            id="multi-statement-shared-ddl",
        ),
        pytest.param(
            "create table if not exists logs (id INTEGER, msg TEXT)",
            (TableSummary("logs", (ColumnDef("id", "INTEGER"), ColumnDef("msg", "TEXT"))),),
            id="if-not-exists-lowercase",
        ),
        pytest.param(
            'CREATE TABLE "sent" (day TEXT)',
            (TableSummary("sent", (ColumnDef("day", "TEXT"),)),),
            id="quoted-table-name",
        ),
        pytest.param(
            """CREATE TABLE orders (
                id INT,
                user_id INT,
                email TEXT,
                total INT,
                PRIMARY KEY (id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE (email),
                CHECK (total > 0),
                CONSTRAINT positive CHECK (total > 0)
            )""",
            (
                TableSummary(
                    "orders",
                    (
                        ColumnDef("id", "INT"),
                        ColumnDef("user_id", "INT"),
                        ColumnDef("email", "TEXT"),
                        ColumnDef("total", "INT"),
                    ),
                ),
            ),
            id="table-level-constraints-skipped",
        ),
        pytest.param(
            "CREATE TABLE prices (amount DECIMAL(10,2) NOT NULL, note TEXT)",
            (TableSummary("prices", (ColumnDef("amount", "DECIMAL(10,2) NOT NULL"), ColumnDef("note", "TEXT"))),),
            id="decimal-comma-survives",
        ),
        pytest.param(
            "CREATE TABLE t (price INT CHECK (price > 0), label TEXT)",
            (TableSummary("t", (ColumnDef("price", "INT CHECK (price > 0)"), ColumnDef("label", "TEXT"))),),
            id="inline-check-survives",
        ),
        pytest.param("CREATE INDEX idx_sent ON sent (day)", (), id="create-index-only"),
        pytest.param("not sql at all, just prose", (), id="garbage"),
    ],
)
def test_parse_ddl(ddl: str, expected: tuple[TableSummary, ...]) -> None:
    assert parse_ddl(ddl) == expected


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        pytest.param("short prompt", 160, "short prompt", id="short-unchanged"),
        pytest.param("a" * 160, 160, "a" * 160, id="exact-boundary-unchanged"),
        pytest.param("a" * 161, 160, "a" * 159 + "…", id="long-truncated-with-ellipsis"),
        pytest.param("hello world", 5, "hell…", id="custom-limit"),
    ],
)
def test_excerpt(text: str, limit: int, expected: str) -> None:
    assert excerpt(text, limit=limit) == expected


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        pytest.param(
            CronTrigger(cron_expression=CronExpr("0 9 * * *"), timezone="UTC"), "cron 0 9 * * * (UTC)", id="cron"
        ),
        pytest.param(
            EventTrigger(source="gmail", event="query", key="from:a@b.com"),
            "event gmail query/from:a@b.com",
            id="event",
        ),
        pytest.param(ManualTrigger(), "manual", id="manual"),
    ],
)
def test_render_trigger(trigger: Trigger, expected: str) -> None:
    assert render_trigger(trigger) == expected


@pytest.mark.parametrize(
    ("firing", "expected"),
    [
        pytest.param(Firing(trigger=ManualTrigger()), "manual", id="manual-no-occurrences"),
        pytest.param(
            Firing(trigger=CronTrigger(cron_expression=CronExpr("0 9 * * *"), timezone="UTC")),
            "cron 0 9 * * * (UTC)",
            id="cron-no-occurrences",
        ),
        pytest.param(
            Firing(trigger=EventTrigger(source="gmail", event="query", key="inbox"), occurrence_ids=["m1", "m2", "m3"]),
            "event gmail query/inbox (3 msgs)",
            id="event-counts-occurrences",
        ),
    ],
)
def test_render_firing(firing: Firing, expected: str) -> None:
    assert render_firing(firing) == expected


@pytest.mark.parametrize(
    ("status", "style"),
    [
        pytest.param("pending", "dim", id="pending-dim"),
        pytest.param("running", "yellow", id="running-yellow"),
        pytest.param("succeeded", "green", id="succeeded-green"),
        pytest.param("failed", "bold red", id="failed-bold-red"),
        pytest.param("stopped", "dim", id="stopped-dim"),
    ],
)
def test_run_status_text(status: RunStatus, style: str) -> None:
    text = run_status_text(status)
    assert text.plain == status
    assert text.style == style


def test_workflow_card_from_workflow() -> None:
    trigger = CronTrigger(cron_expression=CronExpr("0 9 * * *"), timezone="UTC")
    workflow = FakeWorkflow(
        name="digest-workflow",
        version=2,
        workflow_id=WorkflowId(uuid4()),
        definition=WorkflowDefinition(
            summary="Sends the digest each morning", prompt=PromptStr("send the digest"), rules=["be brief"]
        ),
        ddl=SchemaStr("CREATE TABLE sent (day TEXT)"),
        status="active",
        triggers=[trigger],
    )
    assert WorkflowCard.from_workflow(workflow) == WorkflowCard(
        name="digest-workflow",
        summary="Sends the digest each morning",
        prompt="send the digest",
        rules=("be brief",),
        ddl="CREATE TABLE sent (day TEXT)",
        triggers=(trigger,),
        version=2,
        status="active",
    )


def test_workflow_card_from_draft() -> None:
    draft = WorkflowDraft(
        name="digest-workflow",
        summary="Sends the digest each morning",
        prompt="send the digest",
        rules=["be brief", "no emoji"],
        ddl="CREATE TABLE sent (day TEXT)",
        triggers=[ManualTrigger()],
    )
    assert WorkflowCard.from_draft(draft) == WorkflowCard(
        name="digest-workflow",
        summary="Sends the digest each morning",
        prompt="send the digest",
        rules=("be brief", "no emoji"),
        ddl="CREATE TABLE sent (day TEXT)",
        triggers=(ManualTrigger(),),
        version=None,
        status=None,
    )


@pytest.mark.parametrize(
    ("rows", "limit", "expected_rows", "caption"),
    [
        pytest.param([{"v": i} for i in range(7)], 5, 5, "… +2 more", id="over-limit-truncated"),
        pytest.param([{"v": i} for i in range(7)], None, 7, None, id="no-limit-shows-all"),
        pytest.param([{"v": 1}], 5, 1, None, id="under-limit-no-caption"),
        pytest.param([], 5, 0, "(no rows)", id="empty-captioned"),
    ],
)
def test_state_table_limits(
    rows: list[dict[str, int]], limit: int | None, expected_rows: int, caption: str | None
) -> None:
    table = state_table("t", rows, limit=limit)
    assert table.title == "t"
    assert table.row_count == expected_rows
    assert table.caption == caption
