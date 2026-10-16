from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    total_tokens: int = 0


class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...
