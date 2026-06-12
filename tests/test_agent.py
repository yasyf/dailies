from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from loguru import logger

from dailies.agent import AgentProvider, AgentRequest, AgentResult, ClaudeAgentSDKProvider, adapt
from dailies.models import TaskId, WorkflowId
from dailies.runtime import RunContext
from dailies.tools.base import ToolError, ToolSet, ToolSpec, tool
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


async def test_adapt_dumps_list_of_models_as_json_array() -> None:
    async def list_result(args: dict[str, Any]) -> Any:
        return [QueryResult(rows=[{"n": 1}], truncated=False), QueryResult(rows=[], truncated=True)]

    spec = ToolSpec(name="lq", description="d", input_schema={"type": "object"}, invoke=list_result)
    assert await adapt(spec).handler({}) == {
        "content": [
            {"type": "text", "text": '[{"rows": [{"n": 1}], "truncated": false}, {"rows": [], "truncated": true}]'}
        ]
    }


async def test_adapt_maps_exception_to_is_error() -> None:
    spec = ToolSpec(name="boom", description="d", input_schema={"type": "object"}, invoke=raises)
    assert await adapt(spec).handler({}) == {
        "content": [{"type": "text", "text": '{"error_type": "RuntimeError", "detail": "nope"}'}],
        "is_error": True,
    }


async def test_adapt_maps_validation_error_to_invalid_input() -> None:
    result = await adapt(add_spec()).handler({"a": "not-int", "b": 2})
    assert result["is_error"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error_type"] == "invalid_input"
    assert "Input should be a valid integer" in payload["detail"]
    assert payload["fix"] == "correct the arguments to match the tool schema"


async def test_adapt_renders_tool_error_payload() -> None:
    async def conflicted(args: dict[str, Any]) -> Any:
        raise ToolError("conflict", "already sent today", fix="check list_actions before re-sending")

    spec = ToolSpec(name="c", description="d", input_schema={"type": "object"}, invoke=conflicted)
    assert await adapt(spec).handler({}) == {
        "content": [
            {
                "type": "text",
                "text": '{"error_type": "conflict", "detail": "already sent today",'
                ' "fix": "check list_actions before re-sending"}',
            }
        ],
        "is_error": True,
    }


async def test_adapt_omits_fix_when_tool_error_has_none() -> None:
    async def fixless(args: dict[str, Any]) -> Any:
        raise ToolError("conflict", "already sent today")

    spec = ToolSpec(name="c", description="d", input_schema={"type": "object"}, invoke=fixless)
    assert await adapt(spec).handler({}) == {
        "content": [{"type": "text", "text": '{"error_type": "conflict", "detail": "already sent today"}'}],
        "is_error": True,
    }


async def test_adapt_logs_each_invocation() -> None:
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(str(message).strip()), format="{message}")
    try:
        await adapt(add_spec()).handler({"a": 1, "b": 2})
        await adapt(add_spec()).handler({"a": "not-int", "b": 2})
    finally:
        logger.remove(handler_id)
    assert messages == ["tool invoked: tool=add ok=True", "tool invoked: tool=add ok=False"]


def test_adapt_preserves_defs_schema() -> None:
    schema = {"$defs": {"N": {"type": "object"}}, "type": "object", "properties": {"n": {"$ref": "#/$defs/N"}}}
    spec = ToolSpec(name="x", description="d", input_schema=schema, invoke=raises)
    assert "$defs" in adapt(spec).input_schema


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(AgentResult("x", ok=True)), AgentProvider)


def test_provider_default_model() -> None:
    assert ClaudeAgentSDKProvider().model == "claude-opus-4-8"
    assert ClaudeAgentSDKProvider(model="claude-sonnet-4-6").model == "claude-sonnet-4-6"


def test_request_chrome_defaults_false() -> None:
    assert AgentRequest(system="s", prompt="p").chrome is False


@pytest.mark.parametrize(
    ("chrome", "extra_args", "env"),
    [(True, {"chrome": None}, {"ANTHROPIC_API_KEY": ""}), (False, {}, {})],
    ids=["chrome", "no-chrome"],
)
async def test_run_passes_chrome_extra_args(
    monkeypatch: pytest.MonkeyPatch, chrome: bool, extra_args: dict[str, Any], env: dict[str, str]
) -> None:
    captured: dict[str, Any] = {}

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        captured["options"] = options
        return
        yield

    monkeypatch.setattr("claude_agent_sdk.query", fake_query)
    result = await ClaudeAgentSDKProvider().run(AgentRequest(system="s", prompt="p", chrome=chrome))
    assert captured["options"].extra_args == extra_args
    assert captured["options"].env == env
    assert result == AgentResult(text="", ok=False)
