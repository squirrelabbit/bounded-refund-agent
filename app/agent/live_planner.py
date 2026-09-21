"""One optional live planner adapter.

Three things to be clear about:

* Importing this module opens no connection and reads no credential beyond an
  environment variable lookup. The SDK import happens inside ``__init__``.
* Without ``ANTHROPIC_API_KEY`` the planner refuses to be constructed. It does
  not silently degrade into a stub.
* Whatever the model returns is just a proposal. It goes through the same
  schema gate, the same policy and the same permit boundary as a scripted
  proposal. There is no path from here to a write tool.

Why the output schema is deliberately loose
-------------------------------------------

The adapter uses the provider's structured-output support
(``client.messages.parse`` with an ``output_format``), so the response comes
back as a parsed object rather than a blob of text this module has to guess at.
But :class:`LiveProposal` is kept as loose as the API will allow: ``action`` is
a plain ``str``, not a closed enum, and ``amount_cents`` is an unconstrained
optional integer.

That is on purpose, and it is the whole point of the adapter. What this project
sets out to show is that a bad proposal is stopped by the *server's*
``ActionProposal`` gate, the policy engine and the permit boundary. If the
provider were asked to enforce a closed enum, an out-of-vocabulary action could
never leave the model, the server-side refusal would never fire, and the
boundary this repository exists to demonstrate would be invisible - working,
but untestable and unwatchable. Constraining the schema here would move the
safety guarantee into the provider, which is exactly where this project argues
it should not live.

So the loose schema buys the parsing convenience and gives up nothing, because
nothing downstream trusts the result. ``action: "wire_transfer"`` and
``amount_cents: -1`` both round-trip through here happily and are both refused
by :func:`app.agent.runner._validate` before the policy is even consulted.

Nothing in this module is used for release-criteria evidence. The safety
numbers in this project come from the scripted planner and the deterministic
oracle, because a model's output is not reproducible and an unreproducible
measurement is not evidence.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

from app.agent.planner import PlannerContext, RawProposal
from app.models.schemas import ActionName

API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-opus-5"

# Thinking is on by default on this model, and thinking tokens are drawn from
# the same budget as the visible answer. 1024 was enough for a bare JSON object
# and is not enough once the model thinks first.
DEFAULT_MAX_TOKENS = 4096

# Choosing between three actions for one order is a routing decision, not a
# research task, so the cheapest effort level is the honest setting.
DEFAULT_EFFORT = "low"

SYSTEM_PROMPT = """You are a support planner for an order system.

You do not perform actions. You propose exactly one action and the server
decides whether it may happen. Name the action you believe is right, the order
it applies to, the amount in cents when the action is a refund, and one short
sentence of reasoning.
"""


class LiveProposal(BaseModel):
    """The provider-side output schema. Loose on purpose - see the module docstring."""

    action: str
    order_id: str
    amount_cents: int | None
    reason: str


class LivePlannerDisabled(RuntimeError):
    """Raised when the adapter is asked to exist without a credential."""


class LivePlanner:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
        effort: str | None = DEFAULT_EFFORT,
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
        self.effort = effort
        self._client = anthropic.Anthropic(api_key=resolved)

    def propose(self, context: PlannerContext) -> RawProposal:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _render_request(context)}],
            "output_format": LiveProposal,
        }
        if self.effort is not None:
            kwargs["output_config"] = {"effort": self.effort}

        response = self._client.messages.parse(**kwargs)
        return _as_raw_proposal(response)


def _render_request(context: PlannerContext) -> str:
    return (
        f"order_id: {context.order_id}\n"
        f"attempt: {context.attempt}\n"
        f"allowed actions: {', '.join(sorted(str(a) for a in ActionName))}\n"
        f"customer says: {context.user_request}"
    )


def _as_raw_proposal(response: Any) -> RawProposal:
    """Hand the server a plain mapping. Validation belongs to the server, not here.

    ``parsed_output`` is absent whenever the provider did not produce a
    schema-conforming object - a refusal, a ``max_tokens`` cut-off, a future
    SDK that reports parse failure differently. In that case fall back to the
    raw text, and if that is not a JSON object either, return ``None`` so the
    runner denies the turn with ``invalid_proposal``. Nothing here repairs,
    defaults or retries a bad answer.
    """
    parsed = getattr(response, "parsed_output", None)
    if isinstance(parsed, LiveProposal):
        return parsed.model_dump()
    if isinstance(parsed, dict):
        return parsed
    return _parse(_first_text(response))


def _first_text(response: Any) -> str:
    for block in getattr(response, "content", []):
        if getattr(block, "type", "") == "text":
            return str(getattr(block, "text", ""))
    return ""


def _parse(text: str) -> RawProposal:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
