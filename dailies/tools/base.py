from __future__ import annotations

import inspect
from abc import ABC
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar, get_type_hints

from pydantic import BaseModel, create_model

P = ParamSpec("P")
T = TypeVar("T")


def model_for(fn: Callable[..., Any], *, name: str) -> type[BaseModel]:
    hints = get_type_hints(fn, include_extras=True)
    return create_model(  # ty: ignore[no-matching-overload]
        name,
        **{
            param: (hints[param], ... if p.default is inspect.Parameter.empty else p.default)
            for param, p in inspect.signature(fn).parameters.items()
            if param != "self"
        },
    )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    # Draft-2020-12 JSON Schema with top-level $defs. A future Anthropic adapter must pass it
    # as a plain dict / cast(InputSchema, schema) and must NOT route it through
    # TypeAdapter(InputSchema).validate_python (strips $defs -> SDK #485 -> API 500s).
    input_schema: dict[str, Any]
    invoke: Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    fn: Callable[..., Coroutine[Any, Any, Any]]

    def to_spec(self) -> ToolSpec:
        model = model_for(self.fn, name=self.name)
        fn = self.fn

        async def invoke(args: dict[str, Any]) -> Any:
            validated = model.model_validate(args)
            return await fn(**{name: getattr(validated, name) for name in model.model_fields})

        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=model.model_json_schema(),
            invoke=invoke,
        )


@dataclass(slots=True)
class StructuredSink[T: BaseModel]:
    """Captures one validated result of type ``T`` from a ``submit`` tool call.

    Pass any model instead of hand-writing a per-type capture toolset: the model
    becomes the tool's input schema and validates the call. Satisfies the shape
    an agent runner needs (``result`` plus ``get_tools``).
    """

    output: type[T]
    result: T | None = None

    def get_tools(self) -> list[Tool]:
        sink = self

        async def submit(value: T) -> None:
            sink.result = value

        # T is unresolvable at runtime under PEP 563, so pin the annotation to the concrete model.
        submit.__annotations__ = {"value": self.output, "return": type(None)}
        return [Tool(name="submit", description=f"Submit the final {self.output.__name__}.", fn=submit)]


class ToolSet(ABC):
    @staticmethod
    def tool(fn: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        qualname = getattr(fn, "__qualname__", "<tool>")
        if not fn.__doc__:
            raise TypeError(f"tool {qualname} must have a docstring")
        hints = get_type_hints(fn)
        if missing := [param for param in inspect.signature(fn).parameters if param != "self" and param not in hints]:
            raise TypeError(f"tool {qualname} must annotate parameters: {', '.join(missing)}")
        setattr(fn, "__tool__", True)
        return fn

    def get_tools(self) -> list[Tool]:
        return [
            Tool(name=method.__name__, description=method.__doc__ or "", fn=method)
            for _, method in inspect.getmembers(self, inspect.ismethod)
            if getattr(method, "__tool__", False)
        ]


tool = ToolSet.tool
