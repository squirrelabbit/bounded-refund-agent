"""The executor's authority token.

The write tools only accept an authorization object built with this exact
object. It lives in the executor package because the executor is the only
component allowed to mint write authorizations.

This is a process-level boundary, not an OS or language-level capability. A
caller inside the same interpreter could import this private name; SCOPE.md says
so plainly. What the tests demonstrate is that the call paths a planner actually
has cannot reach a mutation.
"""

from __future__ import annotations


class _ExecutorAuthority:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<executor authority>"


_EXECUTOR_TOKEN = _ExecutorAuthority()
