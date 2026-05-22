from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ChatResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str  # "tool_use", "end_turn", "stop"
    _raw: Any = field(repr=False, default=None)


class ModelAdapter(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResponse: ...

    @abstractmethod
    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict]:
        """Returns [assistant_turn, tool_result_turn] messages to append to conversation."""
        ...
