from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from loguru import logger

from dailies import tools
from dailies.agent import AgentProvider, AgentRequest, ClaudeAgentSDKProvider
from dailies.documents import Run, Workflow
from dailies.models import (
    Action,
    CronTrigger,
    EventTrigger,
    RunStatus,
    StatusUpdate,
    TextBlock,
    Trigger,
    WorkflowId,
)
from dailies.runtime import RunContext
from dailies.tools import ToolSet

# First-sweep lookback: a long-idle workflow fires at most one slot within this window
# (fire-at-most-once-per-sweep, no catch-up). Tune to match the dly tick cadence.
LOOKBACK = timedelta(days=1)

SYSTEM = (
    "You are the dailies workflow runner. Execute the user's workflow prompt using only the provided "
    "tools. Follow the workflow's rules, read and update state through the tools, take the actions the "
    "prompt calls for, and stop once the goal is met."
)


class WorkflowNotFound(LookupError):
    pass


def cron_due(trigger: CronTrigger, *, now: datetime, since: datetime) -> bool:
    """Whether a cron slot falls in the half-open window ``(since, now]``, evaluated in the trigger's timezone."""
    return croniter(trigger.cron_expression, since.astimezone(ZoneInfo(trigger.timezone))).get_next(datetime) <= now


async def workflow_cursor(workflow: Workflow, *, now: datetime) -> datetime:
    latest = await Run.find(Run.workflow_doc_id == workflow.uid).sort("-created_at").first_or_none()
    return max(latest.created_at if latest else workflow.created_at, now - LOOKBACK)


@dataclass(frozen=True, slots=True)
class TriggerFired:
    workflow_id: WorkflowId
    trigger: Trigger


@dataclass(frozen=True, slots=True)
class RunCreated:
    run_id: UUID
    workflow_id: WorkflowId


@dataclass(frozen=True, slots=True)
class StatusRecorded:
    run_id: UUID
    update_id: UUID


@dataclass(frozen=True, slots=True)
class ActionRecorded:
    run_id: UUID
    action_id: UUID


@dataclass(frozen=True, slots=True)
class RunStatusChanged:
    run_id: UUID
    status: RunStatus


type Event = RunCreated | StatusRecorded | ActionRecorded | RunStatusChanged


def emit(event: Event) -> None:
    match event:
        case RunCreated(run_id=run_id, workflow_id=workflow_id):
            logger.info("run created: run={} workflow={}", run_id, workflow_id)
        case StatusRecorded(run_id=run_id, update_id=update_id):
            logger.info("status recorded: run={} update={}", run_id, update_id)
        case ActionRecorded(run_id=run_id, action_id=action_id):
            logger.info("action recorded: run={} action={}", run_id, action_id)
        case RunStatusChanged(run_id=run_id, status=status):
            logger.info("run status: run={} status={}", run_id, status)


@dataclass(frozen=True, slots=True)
class Engine:
    provider: AgentProvider = field(default_factory=ClaudeAgentSDKProvider)

    async def active_workflow(self, workflow_id: WorkflowId) -> Workflow:
        workflow = (
            await Workflow.find(Workflow.workflow_id == workflow_id, Workflow.status == "active")
            .sort("-version")
            .first_or_none()
        )
        if workflow is None:
            raise WorkflowNotFound(workflow_id)
        return workflow

    async def dispatch(self, fired: TriggerFired) -> Run:
        workflow = await self.active_workflow(fired.workflow_id)
        run = Run(
            workflow_doc_id=workflow.uid,
            workflow_id=workflow.workflow_id,
            task_id=workflow.task_id,
            trigger=fired.trigger,
        )
        await run.insert()
        emit(RunCreated(run_id=run.uid, workflow_id=run.workflow_id))
        await self.invoke_agent(run)
        return run

    async def dispatch_event(self, *, workflow_id: WorkflowId, event_type: str, event_key: str) -> Run:
        return await self.dispatch(TriggerFired(workflow_id, EventTrigger(event_type=event_type, event_key=event_key)))

    async def fire_due(self, *, now: datetime) -> list[Run]:
        runs: list[Run] = []
        async for workflow in Workflow.find(Workflow.status == "active"):
            since = await workflow_cursor(workflow, now=now)
            runs.extend(
                [
                    await self.dispatch(TriggerFired(workflow.workflow_id, trigger))
                    for trigger in workflow.triggers
                    if isinstance(trigger, CronTrigger) and cron_due(trigger, now=now, since=since)
                ]
            )
        return runs

    def build_toolsets(self, run: Run) -> tuple[ToolSet, ...]:
        return tools.build_toolsets(
            RunContext(
                workflow_id=run.workflow_id,
                workflow_doc_id=run.workflow_doc_id,
                task_id=run.task_id,
                run_id=run.uid,
            )
        )

    async def invoke_agent(self, run: Run) -> None:
        workflow = await Workflow.get(run.workflow_doc_id)
        if workflow is None:
            raise WorkflowNotFound(run.workflow_doc_id)
        specs = tuple(t.to_spec() for ts in self.build_toolsets(run) for t in ts.get_tools())
        await self.set_status(run, "running")
        result = await self.provider.run(AgentRequest(system=SYSTEM, prompt=workflow.definition.prompt, tools=specs))
        await self.record_status(run, StatusUpdate(title="result", blocks=[TextBlock(text=result.text)]))
        await self.set_status(run, "succeeded" if result.ok else "failed")

    async def set_status(self, run: Run, status: RunStatus) -> None:
        await run.update({"$set": {"status": status}})
        emit(RunStatusChanged(run_id=run.uid, status=status))

    async def record_status(self, run: Run, update: StatusUpdate) -> None:
        await run.update({"$push": {"status_updates": update.model_dump(mode="python")}})
        emit(StatusRecorded(run_id=run.uid, update_id=update.id))

    async def record_action(self, run: Run, action: Action) -> None:
        await run.update({"$push": {"actions": action.model_dump(mode="python")}})
        emit(ActionRecorded(run_id=run.uid, action_id=action.id))
