"""One optional live planner adapter.

Three things to be clear about:

* Importing this module opens no connection and reads no credential beyond an
  environment variable lookup. The SDK import happens inside ``__init__``.
* Without ``ANTHROPIC_API_KEY`` the planner refuses to be constructed. It does
  not silently degrade into a stub.
* Whatever the model returns is just a proposal. It goes through the same
  schema gate, the same policy and the same permit boundary as a scripted
  proposal. There is no path from here to a write tool.

Nothing in this module is used for release-criteria evidence. The safety
numbers in this project come from the scripted planner and the deterministic
oracle, because a model's output is not reproducible and an unreproducible
measurement is not evidence.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.agent.planner import PlannerContext, RawProposal
from app.models.schemas import ActionName

API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 1024

SYSTEM_PROMPT = """You are a support planner for an order system.

You do not perform actions. You propose exactly one action and the server
decides whether it may happen. Reply with a single JSON object and nothing
else:

  {"action": "<one of cancel_order|issue_refund|create_support_ticket>",
   "order_id": "<the order id you were given>",
   "amount_cents": <integer or null>,
   "reason": "<one short sentence>"}
"""


class LivePlannerDisabled(RuntimeError):
    """Raised when the adapter is asked to exist without a credential."""


class LivePlanner:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
    ) -> None:
        resolved = api_key or os.environ.get(API_KEY_ENV)
        if not resolved:
            raise LivePlannerDisabled(
                f"{API_KEY_ENV} is not set, so the live planner is disabled. "
                "Scenario evaluation uses ScriptedPlanner and never calls a model."
            )
        try:
            import anthropic
        except ImportError as missing:
            raise LivePlannerDisabled(
                "the anthropic SDK is not installed; the live planner is optional"
            ) from missing

        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=resolved)

    def propose(self, context: PlannerContext) -> RawProposal:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _render_request(context)}],
        )
        return _parse(_first_text(response))


def _render_request(context: PlannerContext) -> str:
    return (
        f"order_id: {context.order_id}\n"
        f"attempt: {context.attempt}\n"
        f"allowed actions: {', '.join(sorted(str(a) for a in ActionName))}\n"
        f"customer says: {context.user_request}"
    )


def _first_text(response: Any) -> str:
    for block in getattr(response, "content", []):
        if getattr(block, "type", "") == "text":
            return str(getattr(block, "text", ""))
    return ""


def _parse(text: str) -> RawProposal:
    """Return the raw mapping. Validation belongs to the server, not here."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
