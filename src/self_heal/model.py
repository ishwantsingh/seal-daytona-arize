"""Thin OpenRouter client and a small model protocol for scripted tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI


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
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class OpenRouterModel:
    """OpenAI-compatible boundary that Phase 3 can wrap with Arize Phoenix."""

    def __init__(
        self, api_key: str, model: str, *, timeout_seconds: float = 45
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )

    @property
    def tracing_settings(self) -> dict[str, Any]:
        """Stable, non-secret settings recorded with supervisor-owned run evidence."""

        return {
            "temperature": 0,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "default_tool_schema_strict": False,
            "timeout_seconds": getattr(self, "timeout_seconds", None),
        }

    def complete_json(
        self, messages: list[dict[str, Any]], schema: dict[str, Any] | None = None
    ) -> ModelReply:
        """Supervisor-only structured output; ordinary agent settings stay fixed."""
        response_format = (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "seal_evolution_proposal",
                    "strict": True,
                    "schema": schema,
                },
            }
            if schema is not None
            else {"type": "json_object"}
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format=response_format,
            extra_body={"provider": {"require_parameters": True}},
        )
        if not response.choices:
            raise RuntimeError("Evolution model returned no choices")
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError("Evolution model output was truncated")
        usage = response.usage
        return ModelReply(
            content=choice.message.content,
            total_tokens=usage.total_tokens if usage and usage.total_tokens else 0,
        )

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            # Some providers default to strict schemas and fill optional fields.
            # Preserve our declared `required` fields and caller-owned schemas.
            normalized_tools = [
                {**tool, "function": {"strict": False, **tool["function"]}}
                for tool in tools
            ]
            request.update(
                tools=normalized_tools, tool_choice="auto", parallel_tool_calls=False
            )
        response = self.client.chat.completions.create(**request)
        if not response.choices:
            raise RuntimeError("Model returned no choices")
        message = response.choices[0].message
        calls = tuple(
            ToolCall(
                id=call.id, name=call.function.name, arguments=call.function.arguments
            )
            for call in (message.tool_calls or ())
        )
        usage = response.usage
        return ModelReply(
            content=message.content,
            tool_calls=calls,
            total_tokens=usage.total_tokens if usage and usage.total_tokens else 0,
        )
