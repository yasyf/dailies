"""Task activation: gate on every unmet prerequisite at once, each carrying its exact fix."""

from __future__ import annotations

from dailies.connections import INTEGRATIONS, integration_ready, unready_fix
from dailies.documents import Task, Workflow
from dailies.models import FrozenModel, SpendPolicy, TaskId, WorkflowId
from dailies.profile import ProfileNotFound, load_profile


class Problem(FrozenModel):
    """One unmet activation prerequisite and the exact command that fixes it."""

    detail: str
    fix: str


class ActivationError(Exception):
    """Activation refused; carries every unmet prerequisite at once."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(f"{(n := len(problems))} problem{'s block' if n != 1 else ' blocks'} activation")
        self.problems = problems


class TaskNotFound(LookupError):
    """No task with the given id; `dly tasks` lists the known ones."""

    def __init__(self, task_id: TaskId) -> None:
        super().__init__(f"no task {task_id} — run `dly tasks` to list tasks")


async def latest_workflows(task_id: TaskId) -> list[Workflow]:
    """Return the newest version of each of the task's workflows, regardless of status."""
    current: dict[WorkflowId, Workflow] = {}
    async for workflow in Workflow.find(Workflow.task_id == task_id):
        if (seen := current.get(workflow.workflow_id)) is None or workflow.version > seen.version:
            current[workflow.workflow_id] = workflow
    return list(current.values())


async def profile_seeded() -> bool:
    try:
        await load_profile()
    except ProfileNotFound:
        return False
    return True


async def collect_problems(task: Task, workflows: list[Workflow], *, ack_gaps: bool) -> list[Problem]:
    """Gather every unmet prerequisite at once: unacked gaps, then the profile, then integrations."""
    gaps = (
        []
        if ack_gaps
        else [
            Problem(detail=f"unacknowledged gap: {gap}", fix="review it, then re-run with --ack-gaps")
            for gap in task.gaps
        ]
    )
    profile = [] if await profile_seeded() else [Problem(detail="profile is not seeded", fix="run `dly profile init`")]
    integrations = [
        Problem(detail=f"integration {name} is not ready", fix=await unready_fix(INTEGRATIONS[name]))
        for name in sorted({name for workflow in workflows for name in workflow.requires})
        if not await integration_ready(INTEGRATIONS[name])
    ]
    return [*gaps, *profile, *integrations]


async def activate_task(task_id: TaskId, *, ack_gaps: bool, spend_policy: SpendPolicy | None) -> Task:
    """Flip the task and the latest version of each of its workflows to active.

    Activation is validation plus a status flip: the state databases were
    materialized when the proposal was persisted and are never touched here.

    Raises:
        TaskNotFound: no task has the given id.
        ActivationError: one or more prerequisites are unmet; carries all of them.
    """
    task = await Task.get(task_id)
    if task is None:
        raise TaskNotFound(task_id)
    workflows = await latest_workflows(task.uid)
    if problems := await collect_problems(task, workflows, ack_gaps=ack_gaps):
        raise ActivationError(problems)
    for workflow in workflows:
        workflow.status = "active"
        await workflow.replace()
    task.status = "active"
    task.spend_policy = spend_policy or task.spend_policy
    await task.replace()
    return task
