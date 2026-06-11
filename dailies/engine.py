from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from beanie.operators import GT, In, Max
from croniter import croniter
from loguru import logger

from dailies import tools
from dailies.agent import AgentProvider, AgentRequest, ClaudeAgentSDKProvider
from dailies.documents import Run, Subscription, Workflow
from dailies.gmail import GmailClient, ThreadNotFound, gmail_client
from dailies.models import (
    Action,
    CronTrigger,
    EventTrigger,
    Firing,
    ManualTrigger,
    RunStatus,
    StatusUpdate,
    TextBlock,
    Trigger,
    WorkflowId,
    WorkflowTrigger,
    utcnow,
)
from dailies.runtime import RunContext
from dailies.storage import StateStorage, state_storage
from dailies.tools import ToolSet
from dailies.tools.inputs import insert_subscription, news_since
from dailies.web import BrowserClient, WebClient, browser_client, chrome_available, web_client

# First-sweep lookback: a long-idle workflow fires at most one slot within this window
# (fire-at-most-once-per-sweep, no catch-up). Tune to match the dly tick cadence.
LOOKBACK = timedelta(days=1)

# Debounce for workflow-completion triggers: while any upstream completion is younger
# than this, hold all of a workflow's completion occurrences so near-simultaneous
# finishes coalesce into one downstream run.
SETTLE_WINDOW = timedelta(minutes=5)

SYSTEM = (
    "You are the dailies workflow runner. Execute the user's workflow prompt using only the provided "
    "tools. Follow the workflow's rules, read and update state through the tools, take the actions the "
    "prompt calls for, and stop once the goal is met. "
    "State lives in a SQLite database: this workflow's private tables are addressed bare, and tables "
    "shared across the task's workflows as shared.<table>; inspect the schema with describe_state "
    "before querying. "
    "check_subscriptions reports new messages on watched email threads and queries; check it whenever "
    "a run may have been fired by email activity. "
    "On the web, search_web finds pages, fetch_url reads one, and scrape extracts targeted content "
    "from a single page. "
)
CHROME_HINT = "For interactive multi-step web tasks, use the Claude-in-Chrome browser tools."
BROWSE_HINT = "For interactive multi-step web tasks, call the browse tool with the goal stated as one task."


def system_prompt(*, chrome: bool) -> str:
    return f"{SYSTEM}{CHROME_HINT if chrome else BROWSE_HINT}"


class WorkflowNotFound(LookupError):
    pass


def cron_due(trigger: CronTrigger, *, now: datetime, since: datetime) -> bool:
    """Whether a cron slot falls in the half-open window ``(since, now]``, evaluated in the trigger's timezone."""
    return croniter(trigger.cron_expression, since.astimezone(ZoneInfo(trigger.timezone))).get_next(datetime) <= now


async def workflow_cursor(workflow: Workflow, *, now: datetime) -> datetime:
    latest = await Run.find(Run.workflow_doc_id == workflow.uid).sort("-created_at").first_or_none()
    return max(latest.created_at if latest else workflow.created_at, now - LOOKBACK)


def subscription_key(trigger: Trigger, *, workflow: Workflow) -> tuple[str, str, str] | None:
    match trigger:
        case EventTrigger(source="gmail", event=("thread" | "query") as event, key=key):
            return ("gmail", event, key)
        case EventTrigger(source=source, event=event):
            raise ValueError(f"unknown event trigger {source}/{event} on workflow {workflow.workflow_id}")
        case WorkflowTrigger(workflow_id=upstream):
            return ("workflow", "completed", str(upstream))
        case CronTrigger() | ManualTrigger():
            return None


def firing_trigger(subscription: Subscription) -> Trigger:
    match subscription.source:
        case "gmail":
            return EventTrigger(source=subscription.source, event=subscription.event, key=subscription.key)
        case "workflow":
            return WorkflowTrigger(workflow_id=WorkflowId(UUID(subscription.key)))
        case source:
            raise ValueError(f"unknown subscription source: {source}")


@dataclass(frozen=True, slots=True)
class TriggerFired:
    workflow_id: WorkflowId
    firings: list[Firing]


@dataclass(frozen=True, slots=True)
class Occurrence:
    """What one poll pass saw on one subscription; exists only inside the tick."""

    subscription: Subscription
    new_ids: list[str]
    latest: datetime


def settle(occurrences: list[Occurrence], *, now: datetime) -> list[Occurrence]:
    completions = [o for o in occurrences if o.subscription.source == "workflow"]
    if any(occurrence.latest > now - SETTLE_WINDOW for occurrence in completions):
        return [o for o in occurrences if o.subscription.source != "workflow"]
    return occurrences


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
    storage: StateStorage = field(default_factory=state_storage)
    gmail: GmailClient = field(default_factory=gmail_client)
    web: WebClient = field(default_factory=web_client)
    browser: BrowserClient = field(default_factory=browser_client)
    chrome: bool = field(default_factory=chrome_available)

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
            fired_by=fired.firings,
        )
        await run.insert()
        emit(RunCreated(run_id=run.uid, workflow_id=run.workflow_id))
        await self.invoke_agent(run)
        return run

    async def fire_due(self, *, now: datetime) -> list[Run]:
        runs: list[Run] = []
        async for workflow in Workflow.find(Workflow.status == "active"):
            since = await workflow_cursor(workflow, now=now)
            runs.extend(
                [
                    await self.dispatch(TriggerFired(workflow.workflow_id, [Firing(trigger=trigger)]))
                    for trigger in workflow.triggers
                    if isinstance(trigger, CronTrigger) and cron_due(trigger, now=now, since=since)
                ]
            )
        return runs

    async def latest_active_workflows(self) -> list[Workflow]:
        current: dict[WorkflowId, Workflow] = {}
        async for workflow in Workflow.find(Workflow.status == "active"):
            if (seen := current.get(workflow.workflow_id)) is None or workflow.version > seen.version:
                current[workflow.workflow_id] = workflow
        return list(current.values())

    async def materialize_subscriptions(self, workflow: Workflow) -> None:
        declared = {watch for trigger in workflow.triggers if (watch := subscription_key(trigger, workflow=workflow))}
        for source, event, key in declared:
            existing = await Subscription.find_one(
                Subscription.workflow_id == workflow.workflow_id,
                Subscription.source == source,
                Subscription.event == event,
                Subscription.key == key,
            )
            if existing is None:
                await insert_subscription(workflow.workflow_id, source, event, key, origin="trigger")
        async for subscription in Subscription.find(
            Subscription.workflow_id == workflow.workflow_id, Subscription.origin == "trigger"
        ):
            if (subscription.source, subscription.event, subscription.key) not in declared:
                await subscription.delete()

    async def observe(self, subscription: Subscription) -> Occurrence | None:
        match subscription.source:
            case "gmail":
                return await self.observe_gmail(subscription)
            case "workflow":
                return await self.observe_completions(subscription)
            case source:
                raise ValueError(f"unknown subscription source: {source}")

    async def observe_gmail(self, subscription: Subscription) -> Occurrence | None:
        try:
            metas = await news_since(self.gmail, subscription)
        except ThreadNotFound:
            match subscription.origin:
                case "agent":
                    logger.warning("watched thread gone, dropping subscription: {}", subscription.key)
                    await subscription.delete()
                case "trigger":
                    logger.warning("watched thread gone, skipping declared subscription: {}", subscription.key)
            return None
        if not metas:
            return None
        return Occurrence(subscription=subscription, new_ids=[meta.id for meta in metas], latest=metas[-1].date)

    async def observe_completions(self, subscription: Subscription) -> Occurrence | None:
        runs = (
            await Run.find(
                Run.workflow_id == WorkflowId(UUID(subscription.key)),
                Run.status == "succeeded",
                GT(Run.finished_at, subscription.watermark),
            )
            .sort("+finished_at")
            .to_list()
        )
        if not (completions := [(str(run.uid), at) for run in runs if (at := run.finished_at)]):
            return None
        return Occurrence(subscription=subscription, new_ids=[uid for uid, _ in completions], latest=completions[-1][1])

    async def fire_occurrences(self, workflow_id: WorkflowId, occurrences: list[Occurrence]) -> Run:
        run = await self.dispatch(
            TriggerFired(
                workflow_id,
                [
                    Firing(trigger=firing_trigger(occurrence.subscription), occurrence_ids=occurrence.new_ids)
                    for occurrence in occurrences
                ],
            )
        )
        if run.status == "succeeded":
            for occurrence in occurrences:
                await occurrence.subscription.update(Max({Subscription.watermark: occurrence.latest}))
        return run

    async def poll_subscriptions(self, *, now: datetime) -> list[Run]:
        workflows = await self.latest_active_workflows()
        for workflow in workflows:
            await self.materialize_subscriptions(workflow)
        watched: dict[WorkflowId, list[Subscription]] = {}
        for subscription in await Subscription.find(
            In(Subscription.workflow_id, [workflow.workflow_id for workflow in workflows])
        ).to_list():
            watched.setdefault(subscription.workflow_id, []).append(subscription)
        runs: list[Run] = []
        for workflow_id, subscriptions in watched.items():
            observed = [o for subscription in subscriptions if (o := await self.observe(subscription))]
            if occurrences := settle(observed, now=now):
                runs.append(await self.fire_occurrences(workflow_id, occurrences))
        return runs

    def build_toolsets(self, run: Run) -> tuple[ToolSet, ...]:
        async def record(action: Action) -> None:
            await self.record_action(run, action)

        return tools.build_toolsets(
            RunContext(
                workflow_id=run.workflow_id,
                workflow_doc_id=run.workflow_doc_id,
                task_id=run.task_id,
                run_id=run.uid,
            ),
            storage=self.storage,
            gmail=self.gmail,
            web=self.web,
            browser=self.browser,
            chrome=self.chrome,
            record=record,
        )

    async def invoke_agent(self, run: Run) -> None:
        workflow = await Workflow.get(run.workflow_doc_id)
        if workflow is None:
            raise WorkflowNotFound(run.workflow_doc_id)
        specs = tuple(t.to_spec() for ts in self.build_toolsets(run) for t in ts.get_tools())
        await self.set_status(run, "running")
        result = await self.provider.run(
            AgentRequest(
                system=system_prompt(chrome=self.chrome),
                prompt=workflow.definition.prompt,
                tools=specs,
                chrome=self.chrome,
            )
        )
        await self.record_status(run, StatusUpdate(title="result", blocks=[TextBlock(text=result.text)]))
        await self.set_status(run, "succeeded" if result.ok else "failed")

    async def set_status(self, run: Run, status: RunStatus) -> None:
        match status:
            case "succeeded" | "failed" | "stopped":
                await run.update({"$set": {"status": status, "finished_at": utcnow()}})
            case _:
                await run.update({"$set": {"status": status}})
        emit(RunStatusChanged(run_id=run.uid, status=status))

    async def record_status(self, run: Run, update: StatusUpdate) -> None:
        await run.update({"$push": {"status_updates": update.model_dump(mode="python")}})
        emit(StatusRecorded(run_id=run.uid, update_id=update.id))

    async def record_action(self, run: Run, action: Action) -> None:
        await run.update({"$push": {"actions": action.model_dump(mode="python")}})
        emit(ActionRecorded(run_id=run.uid, action_id=action.id))
