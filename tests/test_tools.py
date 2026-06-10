from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from dailies.models import InterviewTurn, PromptStr, TaskId, Trigger, WorkflowId
from dailies.runtime import RunContext
from dailies.storage import state_storage
from dailies.tools import build_toolsets
from dailies.tools.action import Notification
from dailies.tools.base import StructuredSink, ToolSet, tool
from dailies.tools.state import StateToolSet

pytestmark = pytest.mark.unit


def context() -> RunContext:
    return RunContext(
        workflow_id=WorkflowId(uuid4()), workflow_doc_id=uuid4(), task_id=TaskId(uuid4()), run_id=uuid4()
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
    "send_email": {"to": "a", "subject": "s", "body": "b"},
    "notify": {"notification": {"channel": "c", "title": "t", "body": "b"}},
    "record_action": {"kind": "k", "payload": {}},
    "list_actions": {},
    "get_thread": {"thread_id": "t"},
    "get_message": {"message_id": "m"},
    "search_emails": {"query": "q"},
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
    for toolset in build_toolsets(context(), storage=state_storage()):
        if isinstance(toolset, StateToolSet):
            continue
        for handle in toolset.get_tools():
            with pytest.raises(NotImplementedError):
                await handle.to_spec().invoke(VALID_ARGS[handle.name])
