"""Workflow-neutral execution identity checks; neither persistence nor live exclusion.

Adapters must obtain ``actual`` inside their own guarded transaction and retain the
appropriate physical fence. A matching durable attempt is necessary authority evidence,
not proof that its process is alive. Each workflow supplies its own writable states.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Attempt:
    owner: str
    token: str | int


class AuthorityLost(ValueError):
    """The expected execution attempt no longer owns an allowed transition."""


def require_current(expected: Attempt, actual: Attempt, state: str,
                    allowed_states: Collection[str]) -> None:
    """Require the exact execution owner, attempt token, and workflow state.

    Tokens are opaque: persistence adapters normalize them, if necessary, before
    constructing attempts. This function does not acquire locks, renew leases, or
    authorize a mutation outside the caller's transaction/fence lifetime.
    """
    if expected != actual:
        raise AuthorityLost("execution attempt differs")
    if state not in allowed_states:
        raise AuthorityLost("execution state does not permit this operation")
