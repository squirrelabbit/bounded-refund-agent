"""Structured audit trace, one JSON object per line.

Every event answers the same question: who decided this, about which resource,
at which version, and why. Reading the trace top to bottom should make the whole
run reconstructible without reading the code.

``mutation_committed`` is emitted only when a new mutation was actually written,
so the number of those events in a trace equals the number of ``mutation_log``
rows for that run. A retry that is answered out of the execution record emits
``mutation_replayed`` with ``reason_code: idempotent_replay`` instead; it changes
nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.clock import Clock, to_iso

EVENT_ORDER = (
    "user_request_received",
    "proposal_created",
    "proposal_validated",
    "proposal_rejected",
    "policy_allowed",
    "policy_denied",
    "permit_issued",
    "mutation_attempted",
    "mutation_committed",
    "mutation_replayed",
    "mutation_rejected",
    "state_verified",
    "run_finished",
)

REQUIRED_FIELDS = (
    "run_id",
    "scenario_id",
    "event_type",
    "resource_id",
    "resource_version",
    "decision_owner",
    "reason_code",
    "timestamp",
)


class AuditTrace:
    def __init__(
        self,
        run_id: str,
        scenario_id: str,
        clock: Clock,
        path: str | Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.clock = clock
        self.path = Path(path) if path else None
        self.events: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        event_type: str,
        resource_id: str = "",
        resource_version: int = -1,
        decision_owner: str = "",
        reason_code: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        if event_type not in EVENT_ORDER:
            raise ValueError(f"unknown audit event {event_type!r}")
        event: dict[str, Any] = {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "event_type": event_type,
            "resource_id": resource_id,
            "resource_version": resource_version,
            "decision_owner": str(decision_owner),
            "reason_code": str(reason_code),
            "timestamp": to_iso(self.clock.now()),
        }
        event.update({key: _plain(value) for key, value in extra.items()})
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def event_types(self) -> list[str]:
        return [event["event_type"] for event in self.events]


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
