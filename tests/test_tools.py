from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from dailies.gmail import MAX_BODY, EmailMessage
from dailies.models import Action, InterviewTurn, PromptStr, TaskId, Trigger, WorkflowId, utcnow
from dailies.runtime import RunContext
from dailies.storage import state_storage
from dailies.tools import build_toolsets
from dailies.tools.action import Notification
from dailies.tools.base import StructuredSink, ToolSet, ToolSpec, tool
from tests.fakes import FakeGmail

pytestmark = pytest.mark.unit


def context() -> RunContext:
    return RunContext(
        workflow_id=WorkflowId(uuid4()), workflow_doc_id=uuid4(), task_id=TaskId(uuid4()), run_id=uuid4()
    )


def toolsets(gmail: FakeGmail, recorded: list[Action]) -> tuple[ToolSet, ...]:
    async def record(action: Action) -> None:
        recorded.append(action)

    return build_toolsets(context(), storage=state_storage(), gmail=gmail, record=record)


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
    async def push(self, note: Notification) -> None:
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
    assert "Notification" in schema["$defs"]
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
    with pytest.raises(ValidationError):
        await spec.invoke({"a": "not-int", "b": 3})


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


VALID_ARGS: dict[str, dict] = {
    "notify": {"notification": {"channel": "c", "title": "t", "body": "b"}},
    "record_action": {"kind": "k", "payload": {}},
    "list_actions": {},
    "fetch_url": {"url": "u"},
    "search_web": {"query": "q"},
}


async def test_structured_sink_captures_validated_model() -> None:
    sink = StructuredSink(InterviewTurn)
    spec = sink.get_tools()[0].to_spec()
    assert spec.name == "submit"
    assert set(spec.input_schema["properties"]) == {"value"}
    await spec.invoke({"value": {"finished": True, "question": None}})
    assert sink.result == InterviewTurn(finished=True, question=None)


async def test_every_stub_raises_not_implemented() -> None:
    sets = toolsets(FakeGmail(), [])
    for name, args in VALID_ARGS.items():
        with pytest.raises(NotImplementedError):
            await spec_named(sets, name).invoke(args)


async def test_send_email_records_one_action_after_send() -> None:
    gmail = FakeGmail()
    recorded: list[Action] = []
    action_id = await spec_named(toolsets(gmail, recorded), "send_email").invoke(
        {"to": "a@b.com", "subject": "s", "body": "b"}
    )
    assert [message.to for message in gmail.sent] == ["a@b.com"]
    assert [action.id for action in recorded] == [action_id]
    assert (recorded[0].kind, recorded[0].target) == ("email", "a@b.com")
    assert recorded[0].payload == {"subject": "s", "message_id": "sent-0", "thread_id": "sent-thread-0"}


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
