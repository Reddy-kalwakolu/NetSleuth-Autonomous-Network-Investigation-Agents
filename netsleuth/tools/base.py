"""The investigation tools' contract.

Every tool is a plain function ``fn(session, **args)`` that returns a short summary plus the data
behind it. The session carries the run and the ``as_of`` cutoff, so no tool takes a time or a run
as an argument and the model can't move the clock. ``query_ref`` names the call exactly, so the
harness can rerun any piece of evidence later. Every tool is read only; applying an action is not
a tool.
"""

import inspect
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from netsleuth.storage import StorageSession


class ToolError(ValueError):
    """A tool call that is refused: an unknown tool, bad arguments, or an unknown device."""


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    args: dict[str, Any]
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def query_ref(self) -> str:
        return make_ref(self.tool, self.args)


ToolFn = Callable[..., ToolResult]
TOOLS: dict[str, ToolFn] = {}


def tool(fn: ToolFn) -> ToolFn:
    TOOLS[fn.__name__] = fn
    return fn


def make_ref(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, separators=(',', ':'))}"


def call_tool(session: StorageSession, name: str, args: dict[str, Any]) -> ToolResult:
    fn = TOOLS.get(name)
    if fn is None:
        raise ToolError(f"unknown tool {name}")
    try:
        inspect.signature(fn).bind(session, **args)
    except TypeError as error:
        raise ToolError(f"bad arguments for {name}: {error}") from error
    return fn(session, **args)


def rerun(session: StorageSession, query_ref: str) -> ToolResult:
    name, _, raw = query_ref.partition(":")
    return call_tool(session, name, json.loads(raw))
