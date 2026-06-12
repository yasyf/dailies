from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from dailies.browser import browser_profile_key
from dailies.gmail import MAX_BODY, EmailMessage
from dailies.models import Action, Exchange, InterviewTurn, PromptStr, TaskId, Trigger, WorkflowId, utcnow
from dailies.runtime import RunContext
from dailies.storage import state_storage
from dailies.tools import TOOLSETS, build_toolsets, render_catalog
from dailies.tools.action import ActionToolSet, Notification, SentReceipt
from dailies.tools.base import StructuredSink, ToolError, ToolSet, ToolSpec, tool
from dailies.tools.inputs import BrowseToolSet
from dailies.web import SearchResult
from tests.fakes import FakeBrowser, FakeGmail, FakeWeb

pytestmark = pytest.mark.unit


def context() -> RunContext:
    return RunContext(workflow_id=WorkflowId(uuid4()), workflow_doc_id=uuid4(), task_id=TaskId(uuid4()), run_id=uuid4())


def toolsets(
    gmail: FakeGmail,
    recorded: list[Action],
    *,
    web: FakeWeb | None = None,
    browser: FakeBrowser | None = None,
    chrome: bool = False,
) -> tuple[ToolSet, ...]:
    async def record(action: Action) -> None:
        recorded.append(action)

    async def recorded_actions() -> list[Action]:
        return recorded

    return build_toolsets(
        context(),
        storage=state_storage(),
        gmail=gmail,
        web=web or FakeWeb(),
        browser=browser or FakeBrowser(),
        chrome=chrome,
        record=record,
        recorded=recorded_actions,
    )


def spec_named(sets: tuple[ToolSet, ...], name: str) -> ToolSpec:
    return next(t.to_spec() for ts in sets for t in ts.get_tools() if t.name == name)


def email(message_id: str, *, body: str = "hello") -> EmailMessage:
    return EmailMessage(
        id=message_id,
        thread_id="t1",
        sender="a@example.com",
        to="me@example.com",
        subject="subj",
        body=body,
        date=utcnow(),
    )


class SampleToolSet(ToolSet):
    def __init__(self, context: RunContext) -> None:
        self.context = context

    @tool
    async def calc(self, n: int, label: str = "x") -> int:
        """Compute something."""
        raise NotImplementedError

    @tool
    async def push(self, note: Exchange) -> None:
        """Push a notification."""
        raise NotImplementedError

    @tool
    async def fire(self, trigger: Trigger) -> None:
        """Fire a trigger."""
        raise NotImplementedError

    @tool
    async def echo(self, prompt: PromptStr) -> None:
        """Echo a prompt."""
        raise NotImplementedError

    async def not_a_tool(self) -> None:
        raise NotImplementedError


class AddToolSet(ToolSet):
    def __init__(self, context: RunContext) -> None:
        self.context = context

    @tool
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


def schemas() -> dict[str, dict]:
    return {t.name: t.to_spec().input_schema for t in SampleToolSet(context()).get_tools()}


def test_get_tools_finds_only_decorated_methods() -> None:
    names = {t.name for t in SampleToolSet(context()).get_tools()}
    assert names == {"calc", "push", "fire", "echo"}


def test_schema_int_defaults_and_required() -> None:
    schema = schemas()["calc"]
    assert schema["properties"]["n"]["type"] == "integer"
    assert schema["required"] == ["n"]
    assert schema["properties"]["label"]["default"] == "x"


def test_schema_nested_model_uses_ref() -> None:
    schema = schemas()["push"]
    assert "Exchange" in schema["$defs"]
    assert "$ref" in str(schema["properties"]["note"])


def test_schema_newtype_inlines_primitive() -> None:
    assert schemas()["echo"]["properties"]["prompt"]["type"] == "string"


def test_schema_discriminated_union_has_discriminator() -> None:
    schema = schemas()["fire"]
    assert "discriminator" in str(schema)
    assert "$ref" in str(schema["properties"]["trigger"])


async def test_invoke_validates_then_calls() -> None:
    spec = AddToolSet(context()).get_tools()[0].to_spec()
    assert await spec.invoke({"a": 2, "b": 3}) == 5


async def test_invoke_rejects_bad_input() -> None:
    spec = AddToolSet(context()).get_tools()[0].to_spec()
    with pytest.raises(ToolError) as excinfo:
        await spec.invoke({"a": "not-int", "b": 3})
    assert excinfo.value.error_type == "invalid_input"
    assert excinfo.value.fix == "correct the arguments to match the tool schema"


def test_tool_guard_requires_docstring() -> None:
    async def nodoc(self) -> None:
        return None

    with pytest.raises(TypeError):
        tool(nodoc)


def test_tool_guard_requires_annotations() -> None:
    async def bad(self, x) -> None:  # noqa: ANN001
        """Missing annotation on x."""
        return None

    with pytest.raises(TypeError):
        tool(bad)


def test_draft_tool_guard_requires_docstring() -> None:
    async def nodoc(self) -> None:
        return None

    with pytest.raises(TypeError):
        tool(draft=True)(nodoc)


async def test_draft_stubs_raise_not_implemented() -> None:
    toolset = next(ts for ts in toolsets(FakeGmail(), []) if isinstance(ts, ActionToolSet))
    with pytest.raises(NotImplementedError):
        await toolset.notify(Notification(channel="c", title="t", body="b"))


async def test_structured_sink_captures_validated_model() -> None:
    sink = StructuredSink(InterviewTurn)
    spec = sink.get_tools()[0].to_spec()
    assert spec.name == "submit"
    assert set(spec.input_schema["properties"]) == {"value"}
    await spec.invoke({"value": {"finished": True, "question": None}})
    assert sink.result == InterviewTurn(finished=True, question=None)


async def test_send_email_records_one_action_after_send() -> None:
    gmail = FakeGmail()
    recorded: list[Action] = []
    receipt = await spec_named(toolsets(gmail, recorded), "send_email").invoke(
        {"to": "a@b.com", "subject": "s", "body": "b"}
    )
    assert [message.to for message in gmail.sent] == ["a@b.com"]
    assert [action.id for action in recorded] == [receipt.action_id]
    assert receipt == SentReceipt(action_id=recorded[0].id, message_id="sent-0", thread_id="sent-thread-0")
    assert (recorded[0].kind, recorded[0].target) == ("email", "a@b.com")
    assert recorded[0].payload == {"subject": "s", "message_id": "sent-0", "thread_id": "sent-thread-0"}


async def test_record_action_records_and_returns_id() -> None:
    recorded: list[Action] = []
    action_id = await spec_named(toolsets(FakeGmail(), recorded), "record_action").invoke(
        {"kind": "calendar", "target": "standup", "payload": {"when": "9am"}}
    )
    assert [action.id for action in recorded] == [action_id]
    assert (recorded[0].kind, recorded[0].target, recorded[0].payload) == ("calendar", "standup", {"when": "9am"})


async def test_record_action_defaults_empty_payload() -> None:
    recorded: list[Action] = []
    await spec_named(toolsets(FakeGmail(), recorded), "record_action").invoke({"kind": "k", "target": "t"})
    assert recorded[0].payload == {}


def test_record_action_schema_requires_kind_and_target() -> None:
    assert spec_named(toolsets(FakeGmail(), []), "record_action").input_schema["required"] == ["kind", "target"]


async def test_list_actions_returns_recorded_actions_in_order() -> None:
    recorded: list[Action] = []
    sets = toolsets(FakeGmail(), recorded)
    first = await spec_named(sets, "send_email").invoke({"to": "a@b.com", "subject": "s", "body": "b"})
    second = await spec_named(sets, "record_action").invoke({"kind": "demo", "target": "t"})
    actions = await spec_named(sets, "list_actions").invoke({})
    assert actions == recorded
    assert [action.id for action in actions] == [first.action_id, second]


async def test_get_thread_and_search_truncate_bodies() -> None:
    gmail = FakeGmail()
    gmail.add(email("m1", body="x" * (MAX_BODY + 1)))
    sets = toolsets(gmail, [])
    for name, args in {"get_thread": {"thread_id": "t1"}, "search_emails": {"query": "subj"}}.items():
        (message,) = await spec_named(sets, name).invoke(args)
        assert message.truncated is True
        assert message.body == "x" * MAX_BODY


async def test_get_message_returns_full_body() -> None:
    gmail = FakeGmail()
    gmail.add(email("m1", body="x" * (MAX_BODY + 1)))
    message = await spec_named(toolsets(gmail, []), "get_message").invoke({"message_id": "m1"})
    assert message.truncated is False
    assert message.body == "x" * (MAX_BODY + 1)


def tool_names(sets: tuple[ToolSet, ...]) -> set[str]:
    return {t.name for ts in sets for t in ts.get_tools()}


def test_chrome_excludes_browse_toolset() -> None:
    assert "browse" not in tool_names(toolsets(FakeGmail(), [], chrome=True))
    assert "browse" in tool_names(toolsets(FakeGmail(), [], chrome=False))
    assert {"fetch_url", "search_web", "scrape"} <= tool_names(toolsets(FakeGmail(), [], chrome=True))


def test_render_catalog_groups_tools_by_toolset() -> None:
    catalog = render_catalog()
    assert {"State:", "Action:", "Email:", "Web:", "Browse:", "Profile:"} <= set(catalog.splitlines())
    assert "- query_state: Run a read-only SQL query against this workflow's state database." in catalog
    assert "- send_email: Send an email and return a receipt with the action, message, and thread ids." in catalog


def test_render_catalog_enumerates_profile_fields() -> None:
    assert (
        "- get_profile: Return the user's profile: name, email, phone, iMessage handle, home address, timezone, "
        "birthday, employer and role, partner contact (Rebecca: email/phone), airline and hotel loyalty programs "
        "with member numbers, frequent merchants, and extra facts — each value with its source."
    ) in render_catalog()


def test_catalog_stays_in_sync_with_runtime_toolsets() -> None:
    sets = toolsets(FakeGmail(), [], chrome=False)
    assert {type(ts) for ts in sets} == set(TOOLSETS)
    catalog = render_catalog()
    assert [t.name for ts in sets for t in ts.get_tools() if f"- {t.name}: " not in catalog] == []


def test_draft_tools_hidden_from_runtime_and_catalog() -> None:
    drafts = {"notify"}
    assert not drafts & tool_names(toolsets(FakeGmail(), []))
    catalog = render_catalog()
    assert [name for name in drafts if name in catalog] == []


async def test_web_tools_delegate_to_clients() -> None:
    web = FakeWeb(pages={"https://example.com": "body"}, results=[SearchResult(title="t", url="u", snippet="s")])
    browser = FakeBrowser(result="browsed")
    sets = toolsets(FakeGmail(), [], web=web, browser=browser)
    assert await spec_named(sets, "fetch_url").invoke({"url": "https://example.com"}) == "body"
    assert await spec_named(sets, "search_web").invoke({"query": "q"}) == web.results
    scraped = await spec_named(sets, "scrape").invoke({"url": "https://example.com", "instruction": "the title"})
    assert scraped == "scraped https://example.com"
    assert web.scraped == [("https://example.com", "the title")]
    assert await spec_named(sets, "browse").invoke({"task": "book it"}) == "browsed"
    assert browser.tasks == ["book it"]


def test_web_tool_schemas_require_args() -> None:
    sets = toolsets(FakeGmail(), [])
    assert spec_named(sets, "scrape").input_schema["required"] == ["url", "instruction"]
    assert spec_named(sets, "browse").input_schema["required"] == ["task"]
    assert spec_named(sets, "search_web").input_schema["required"] == ["query"]


async def test_browse_leases_workflow_profile() -> None:
    ctx = context()
    browser = FakeBrowser(result="ok")
    toolset = BrowseToolSet(ctx, browser, state_storage())
    spec = next(t.to_spec() for t in toolset.get_tools() if t.name == "browse")
    assert await spec.invoke({"task": "go"}) == "ok"
    expected = Path(os.environ["DAILIES_STATE_DIR"]) / browser_profile_key(ctx.workflow_id)
    assert browser.profiles == [expected]
    assert expected.exists()
