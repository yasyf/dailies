"""Platform-owned weekly profile refresh: a system task seeded at reserved ids, idempotently."""

from __future__ import annotations

from uuid import UUID

from dailies.documents import Task, Workflow
from dailies.models import (
    CronExpr,
    CronTrigger,
    PromptStr,
    SchemaStr,
    TaskDefinition,
    TaskId,
    WorkflowDefinition,
    WorkflowId,
)
from dailies.state import apply_ddl, task_db_key, workflow_db_key
from dailies.storage import state_storage

REFRESH_TASK_ID = TaskId(UUID("00000000-0000-4000-8000-000000000002"))
REFRESH_WORKFLOW_ID = WorkflowId(UUID("00000000-0000-4000-8000-000000000003"))

REFRESH_TASK_PROMPT = PromptStr(
    "You keep the user's profile current. Each run re-mines their recent mail and the web for anything new "
    "or changed and folds only those deltas into the existing profile. Never rebuild the profile from "
    "scratch and never overwrite what the user typed: merge, never replace."
)

REFRESH_WORKFLOW_SUMMARY = "Folds new findings from recent mail and the web into the existing profile."

REFRESH_WORKFLOW_PROMPT = PromptStr(
    "Call get_profile first to see what is already known and where each value came from. Then mine the last "
    "week or so of mail: search_emails with narrow, recent queries such as newer_than:8d, and ALWAYS "
    "get_message for the full body before extracting an address, a member number, or a signature — those "
    "live in the footers a search snippet cuts off. Confirm any web finding with fetch_url before trusting "
    "it. For each value that is genuinely new or has changed, call update_profile_field for a scalar or "
    "record_fact for a durable fact (keyed by its label), passing the email or web source it came from and "
    "reserving high confidence for primary documents. Do not re-submit a value that already matches what the "
    "profile holds. Stop once every new finding is folded in."
)

REFRESH_RULES = [
    "Merge, never replace: fold in the deltas and leave everything else untouched.",
    "Respect provenance: carry the email or web source of every value you write.",
    "Never clobber a user's own edit — user values are sticky and your write is silently ignored.",
    "Record only durable facts, never one-off events.",
    "Prefer the most recent evidence, and let mail outrank the web for contact details.",
]


async def seed_refresh_task() -> Task:
    """Seed the weekly profile-refresh task at its reserved ids; idempotent once seeded.

    Returns the existing Task without touching state when it is already present,
    so re-seeding never hits ``StateDatabaseExists``. Otherwise materializes the
    empty task and workflow state databases and inserts both documents as drafts.
    """
    if (existing := await Task.get(REFRESH_TASK_ID)) is not None:
        return existing
    task = Task(
        id=REFRESH_TASK_ID,
        name="Profile refresh",
        definition=TaskDefinition(
            user_input="Keep my profile current as new mail and web evidence arrives.",
            description="Re-mine recent mail and the web weekly and merge new findings into the profile.",
            prompt=REFRESH_TASK_PROMPT,
        ),
        shared_ddl=None,
        gaps=[],
        status="draft",
    )
    workflow = Workflow(
        task_id=REFRESH_TASK_ID,
        workflow_id=REFRESH_WORKFLOW_ID,
        version=1,
        name="Refresh profile from recent mail and the web",
        definition=WorkflowDefinition(
            summary=REFRESH_WORKFLOW_SUMMARY, prompt=REFRESH_WORKFLOW_PROMPT, rules=REFRESH_RULES
        ),
        ddl=SchemaStr(""),
        requires=["gmail"],
        status="draft",
        triggers=[CronTrigger(cron_expression=CronExpr("0 7 * * 1"))],
    )
    storage = state_storage()
    await apply_ddl(storage, task_db_key(REFRESH_TASK_ID), None)
    await apply_ddl(storage, workflow_db_key(REFRESH_WORKFLOW_ID), SchemaStr(""))
    await task.insert()
    await workflow.insert()
    return task
