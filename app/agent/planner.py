"""Planners propose; they never decide.

A planner's entire vocabulary is one :class:`ActionProposal`. It has no database
handle, no permit, and no way to reach a write tool. A planner that proposes
something absurd is not a bug in the planner interface: it is exactly the input
the policy and permit tests are built to survive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models.schemas import ActionProposal

RawProposal = ActionProposal | Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PlannerContext:
    """Everything a planner is told. Deliberately small."""

    run_id: str
    scenario_id: str
    user_request: str
    order_id: str
    attempt: int = 1
    observations: dict[str, Any] = field(default_factory=dict)


class Planner(Protocol):
    def propose(self, context: PlannerContext) -> ActionProposal | None: ...


class ScriptedPlanner:
    """Replays a fixed sequence of proposals. No network, no model, no clock.

    Entries may be raw mappings rather than validated ``ActionProposal``
    objects, which is how the malicious and malformed cases are written. The
    runner validates whatever comes out; the planner is never trusted to.
    """

    def __init__(self, proposals: Sequence[RawProposal] = ()) -> None:
        self._proposals = list(proposals)
        self._index = 0
        self.calls = 0

    def propose(self, context: PlannerContext) -> RawProposal:
        self.calls += 1
        if self._index >= len(self._proposals):
            return None
        proposal = self._proposals[self._index]
        self._index += 1
        return proposal

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._proposals)
