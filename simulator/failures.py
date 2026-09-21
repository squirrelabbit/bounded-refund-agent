"""Deterministic failure injection for the synthetic world.

Exactly three failures exist, and no more may be added without changing
DESIGN.md and SCOPE.md:

* ``PRE_COMMIT_STATE_CHANGE``    someone else changes the resource between the
  agent's read and the agent's write, so the version-guarded UPDATE misses.
* ``POST_COMMIT_RESPONSE_TIMEOUT`` the mutation and its execution record are
  committed, then the response is lost on the way back to the caller.
* ``READ_TIMEOUT``              the first N calls of one read tool fail.

Every injection is driven by a scenario specification and is bounded by a call
count, so nothing here can retry forever.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FailureKind(StrEnum):
    PRE_COMMIT_STATE_CHANGE = "pre_commit_state_change"
    POST_COMMIT_RESPONSE_TIMEOUT = "post_commit_response_timeout"
    READ_TIMEOUT = "read_timeout"


class ResponseTimeout(Exception):
    """The mutation committed, but the caller never saw the answer."""

    def __init__(self, action: str, idempotency_key: str) -> None:
        super().__init__(f"response lost after committing {action}")
        self.action = action
        self.idempotency_key = idempotency_key


class ReadUnavailable(Exception):
    """A read tool exhausted its bounded retries."""

    def __init__(self, tool: str, attempts: int) -> None:
        super().__init__(f"{tool} failed after {attempts} attempt(s)")
        self.tool = tool
        self.attempts = attempts


_RESOURCE_TABLES = {
    "order": ("orders", "order_id"),
    "payment": ("payments", "payment_id"),
    "shipment": ("shipments", "shipment_id"),
}

_EDITABLE_COLUMNS = {
    "order": frozenset({"status", "delivered_at", "total_cents"}),
    "payment": frozenset({"status", "captured_cents", "refunded_cents"}),
    "shipment": frozenset({"status", "carrier"}),
}


@dataclass(frozen=True, slots=True)
class StateChange:
    """One declarative edit made behind the agent's back."""

    resource: str
    resource_id: str
    set_values: dict[str, Any] = field(default_factory=dict)
    bump_version: bool = True

    def apply(self, conn: sqlite3.Connection) -> None:
        if self.resource not in _RESOURCE_TABLES:
            raise ValueError(f"unknown resource {self.resource!r}")
        table, key = _RESOURCE_TABLES[self.resource]
        allowed = _EDITABLE_COLUMNS[self.resource]
        unknown = set(self.set_values) - allowed
        if unknown:
            raise ValueError(f"cannot change {sorted(unknown)} on {self.resource}")
        assignments = [f"{column} = ?" for column in self.set_values]
        params: list[Any] = list(self.set_values.values())
        if self.bump_version:
            assignments.append("version = version + 1")
        if not assignments:
            return
        params.append(self.resource_id)
        conn.execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE {key} = ?", params
        )


@dataclass(frozen=True, slots=True)
class FailureSpec:
    """What a scenario asks to go wrong. Absent entries mean "nothing".

    ``read_timeouts`` maps a read tool name to how many of its first calls fail.
    ``pre_commit_changes`` and ``post_commit_timeout_actions`` are keyed by the
    action whose execution they disturb.
    """

    read_timeouts: dict[str, int] = field(default_factory=dict)
    pre_commit_changes: dict[str, tuple[StateChange, ...]] = field(default_factory=dict)
    pre_commit_times: dict[str, int] = field(default_factory=dict)
    post_commit_timeout_actions: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, spec: dict[str, Any] | None) -> FailureSpec:
        if not spec:
            return cls()
        read_timeouts = {
            str(tool): int(times) for tool, times in spec.get("read_timeouts", {}).items()
        }
        pre_commit: dict[str, tuple[StateChange, ...]] = {}
        for action, changes in spec.get("pre_commit_changes", {}).items():
            pre_commit[str(action)] = tuple(
                StateChange(
                    resource=change["resource"],
                    resource_id=change["resource_id"],
                    set_values=dict(change.get("set_values", {})),
                    bump_version=bool(change.get("bump_version", True)),
                )
                for change in changes
            )
        pre_commit_times = {
            str(action): int(times)
            for action, times in spec.get("pre_commit_times", {}).items()
        }
        post_commit = {
            str(action): int(times)
            for action, times in spec.get("post_commit_timeout_actions", {}).items()
        }
        return cls(read_timeouts, pre_commit, pre_commit_times, post_commit)


class FailureInjector:
    """Stateful but fully deterministic: every decision is a bounded counter."""

    def __init__(self, spec: FailureSpec | dict[str, Any] | None = None) -> None:
        self.spec = spec if isinstance(spec, FailureSpec) else FailureSpec.from_dict(spec)
        self._read_calls: dict[str, int] = {}
        self._pre_commit_fired: dict[str, int] = {}
        self._post_commit_fired: dict[str, int] = {}
        self.events: list[str] = []

    def read_should_fail(self, tool: str) -> bool:
        budget = self.spec.read_timeouts.get(tool, 0)
        seen = self._read_calls.get(tool, 0)
        self._read_calls[tool] = seen + 1
        if seen < budget:
            self.events.append(f"{FailureKind.READ_TIMEOUT}:{tool}:{seen + 1}")
            return True
        return False

    def apply_pre_commit(self, conn: sqlite3.Connection, action: str) -> bool:
        changes = self.spec.pre_commit_changes.get(action)
        if not changes:
            return False
        budget = self.spec.pre_commit_times.get(action, 1)
        fired = self._pre_commit_fired.get(action, 0)
        if fired >= budget:
            return False
        self._pre_commit_fired[action] = fired + 1
        for change in changes:
            change.apply(conn)
        self.events.append(f"{FailureKind.PRE_COMMIT_STATE_CHANGE}:{action}")
        return True

    def should_timeout_after_commit(self, action: str) -> bool:
        budget = self.spec.post_commit_timeout_actions.get(action, 0)
        fired = self._post_commit_fired.get(action, 0)
        if fired < budget:
            self._post_commit_fired[action] = fired + 1
            self.events.append(f"{FailureKind.POST_COMMIT_RESPONSE_TIMEOUT}:{action}")
            return True
        return False
