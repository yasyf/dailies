from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool

    from dailies.tools.base import ToolSpec


@dataclass(frozen=True, slots=True)
class AgentRequest:
    system: str
    prompt: str
    tools: tuple[ToolSpec, ...] = ()
    chrome: bool = False


@dataclass(frozen=True, slots=True)
class AgentResult:
    text: str
    ok: bool


@runtime_checkable
class AgentProvider(Protocol):
    """Runs an agent turn against a request and returns its terminal result."""

    async def run(self, request: AgentRequest) -> AgentResult: ...


def jsonable(result: object) -> object:
    match result:
        case BaseModel():
            return result.model_dump(mode="json")
        case list():
            return [jsonable(item) for item in result]
        case _:
            return result


def adapt(spec: ToolSpec) -> SdkMcpTool[Any]:
    from claude_agent_sdk import tool

    @tool(spec.name, spec.description, spec.input_schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await spec.invoke(args)
        except Exception as exc:  # tool-execution boundary -> MCP is_error envelope
            return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(jsonable(result), default=str)}]}

    return handler


@dataclass(frozen=True, slots=True)
class ClaudeAgentSDKProvider:
    """`AgentProvider` backed by the Claude Agent SDK.

    Exposes the run's `ToolSpec`s as a single in-process MCP server with no built-ins
    and no filesystem settings; `request.chrome` additionally launches the agent with
    `--chrome`, adding the native Claude-in-Chrome browser tools.
    """

    model: str = "claude-opus-4-8"
    max_turns: int = 30
    server: str = "dailies"
    version: str = "1.0.0"

    async def run(self, request: AgentRequest) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            create_sdk_mcp_server,
            query,
        )

        mcp_server = create_sdk_mcp_server(
            name=self.server, version=self.version, tools=[adapt(s) for s in request.tools]
        )
        options = ClaudeAgentOptions(
            system_prompt=request.system,
            model=self.model,
            mcp_servers={self.server: mcp_server},
            allowed_tools=[f"mcp__{self.server}__{s.name}" for s in request.tools]
            + (["mcp__claude-in-chrome"] if request.chrome else []),
            setting_sources=[],
            permission_mode="bypassPermissions",
            max_turns=self.max_turns,
            tools=[],
            extra_args={"chrome": None} if request.chrome else {},
        )
        parts: list[str] = []
        ok = False
        async for message in query(prompt=request.prompt, options=options):
            match message:
                case AssistantMessage(content=content):
                    parts.extend(block.text for block in content if isinstance(block, TextBlock))
                case ResultMessage(subtype=subtype):
                    ok = subtype == "success"
        return AgentResult(text="\n".join(parts), ok=ok)
