"""Task-local Component discovery visibility for Assistant tools."""

from __future__ import annotations

from contextvars import ContextVar

_visibility_var: ContextVar[tuple[frozenset[str], frozenset[str]]] = ContextVar(
    "agentic_component_visibility",
    default=(frozenset(), frozenset()),
)


def set_component_visibility(hidden_bundles: list[str], hidden_components: list[str]) -> None:
    _visibility_var.set((frozenset(hidden_bundles), frozenset(hidden_components)))


def current_component_visibility() -> tuple[frozenset[str], frozenset[str]]:
    return _visibility_var.get()


def reset_component_visibility() -> None:
    _visibility_var.set((frozenset(), frozenset()))
