from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

STATUS_VALUES = frozenset(
    {
        "ok",
        "not_found",
        "ambiguous",
        "unsupported_language",
        "unsupported_target",
        "invalid_query",
        "error",
        "unavailable",
    }
)

EVIDENCE_SEMANTIC = "semantic"
EVIDENCE_LEXICAL = "lexical"
EVIDENCE_POSSIBLE_DYNAMIC = "possible_dynamic"
EVIDENCE_BASE_SIDE_LEXICAL = "base_side_lexical"
EVIDENCE_CURRENT_SEMANTIC = "current_semantic"

EVIDENCE_VALUES = frozenset(
    {
        EVIDENCE_SEMANTIC,
        EVIDENCE_LEXICAL,
        EVIDENCE_POSSIBLE_DYNAMIC,
        EVIDENCE_BASE_SIDE_LEXICAL,
        EVIDENCE_CURRENT_SEMANTIC,
    }
)


@dataclass(frozen=True)
class QueryBudget:
    """Internal disclosure budget derived from the single public --limit knob."""

    items: int
    nested_items: int
    hover_chars: int = 4000
    snippet_chars: int = 6000
    text_line_chars: int = 500

    @classmethod
    def from_limit(cls, limit: int) -> "QueryBudget":
        items = max(1, int(limit))
        return cls(items=items, nested_items=min(5, items))


def bounded_text(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars < 1:
        return "", bool(value)
    if len(value) <= max_chars:
        return value, False
    if max_chars <= 3:
        return value[:max_chars], True
    return value[: max_chars - 3] + "...", True


def attach_schema(data: dict[str, Any]) -> dict[str, Any]:
    data["schema_version"] = SCHEMA_VERSION
    return data
