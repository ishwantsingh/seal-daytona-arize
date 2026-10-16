"""Bounded instructions and recent complete tool exchanges."""

from __future__ import annotations

import json
from typing import Any

from self_heal.settings import AnalystConfig


def build_messages(
    task: dict[str, Any],
    rounds: list[list[dict[str, Any]]],
    config: AnalystConfig,
    question: str | None = None,
) -> list[dict[str, Any]]:
    system = (
        "You are an inventory table analyst. Use only the assigned table and the provided tools; "
        "never invent rows. Inspect the table, read all rows needed for the answer, and use calculate "
        "when arithmetic helps. An equality filter matches the named field exactly. The available "
        "metric is on_hand minus reserved for each row. For an ungrouped task, reply with only JSON "
        '{"value": integer}. For a grouped task, reply with only JSON {"groups": {"name": integer, ...}} '
        "with group keys in alphabetical order. Empty matches total zero; empty grouped results are {}. "
        "If you cannot inspect all needed rows within the tool budget, do not guess."
    )
    user_content = "Answer this structured task: " + json.dumps(task, sort_keys=True)
    if question is not None:
        user_content = "Original question: " + question + "\nValidated task: " + json.dumps(task, sort_keys=True)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    for round_messages in rounds[-config.limits.context_rounds :]:
        messages.extend(round_messages)
    return messages
