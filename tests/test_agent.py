from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from dailies.agent import AgentProvider, AgentResult, ClaudeAgentSDKProvider, adapt
from dailies.models import TaskId, WorkflowId
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, ToolSpec, tool
from dailies.tools.state import QueryResult
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


class AddToolSet(ToolSet):
    def __init__(self, context: RunContext) -> None:
        self.context = context

    @tool
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


async def raises(args: dict[str, Any]) -> Any:
    raise RuntimeError("nope")


def add_spec() -> ToolSpec:
    context = RunContext(
        workflow_id=WorkflowId(uuid4()), workflow_doc_id=uuid4(), task_id=TaskId(uuid4()), run_id=uuid4()
    )
    return AddToolSet(context).get_tools()[0].to_spec()


async def test_adapt_wraps_result_in_text_block() -> None:
    assert await adapt(add_spec()).handler({"a": 1, "b": 2}) == {"content": [{"type": "text", "text": "3"}]}


async def test_adapt_dumps_model_results_as_json() -> None:
    async def model_result(args: dict[str, Any]) -> Any:
        return QueryResult(rows=[{"n": 1}], truncated=False)

    spec = ToolSpec(name="q", description="d", input_schema={"type": "object"}, invoke=model_result)
    assert await adapt(spec).handler({}) == {
        "content": [{"type": "text", "text": '{"rows": [{"n": 1}], "truncated": false}'}]
    }


async def test_adapt_maps_exception_to_is_error() -> None:
    spec = ToolSpec(name="boom", description="d", input_schema={"type": "object"}, invoke=raises)
    assert await adapt(spec).handler({}) == {"content": [{"type": "text", "text": "nope"}], "is_error": True}


def test_adapt_preserves_defs_schema() -> None:
    schema = {"$defs": {"N": {"type": "object"}}, "type": "object", "properties": {"n": {"$ref": "#/$defs/N"}}}
    spec = ToolSpec(name="x", description="d", input_schema=schema, invoke=raises)
    assert "$defs" in adapt(spec).input_schema


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(AgentResult("x", ok=True)), AgentProvider)


def test_provider_default_model() -> None:
    assert ClaudeAgentSDKProvider().model == "claude-opus-4-8"
    assert ClaudeAgentSDKProvider(model="claude-sonnet-4-6").model == "claude-sonnet-4-6"
