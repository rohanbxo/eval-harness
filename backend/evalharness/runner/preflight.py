"""Refuse to launch a run the account cannot pay for (DECISIONS D36).

A run that exhausts its credits mid-flight does not fail cleanly. It fails
*partially*: some attempts complete, others error, and the resulting coverage
gap looks exactly like a capability difference until someone reads the error
text. One run lost 22 attempts across two models this way, and the scores it
produced were not comparable to the two models that finished.

So the balance is checked before anything is launched, against the worst case
the spend ceilings allow, and both numbers are printed either way. The check is
conservative on purpose: the ceiling is what the run is *permitted* to spend,
not what it is expected to spend, because "expected" is exactly the estimate
that was wrong when the credits ran out.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

#: OpenRouter's credits endpoint. Returns total granted and total consumed.
CREDITS_URL = "https://openrouter.ai/api/v1/credits"

REQUEST_TIMEOUT_S = 15.0


class PreflightError(RuntimeError):
    """The run must not start."""


@dataclass(frozen=True)
class Balance:
    """What the account has left to spend."""

    total_credits: float
    total_usage: float

    @property
    def remaining(self) -> float:
        return self.total_credits - self.total_usage


@dataclass(frozen=True)
class BalanceCheck:
    """The verdict, with both numbers so a caller can always show its work."""

    remaining_usd: float
    worst_case_usd: float
    sufficient: bool
    detail: str = ""

    def render(self) -> str:
        verdict = "ok" if self.sufficient else "INSUFFICIENT"
        return (
            f"balance ${self.remaining_usd:.2f} vs worst case ${self.worst_case_usd:.2f}"
            f" -- {verdict}"
        )


Fetcher = Callable[[str], dict[str, object]]


def _fetch(api_key: str) -> dict[str, object]:
    """GET the credits endpoint. Never logs the key."""
    request = urllib.request.Request(
        CREDITS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network shape
        raise PreflightError(f"credits endpoint returned HTTP {exc.code}") from exc
    except Exception as exc:  # pragma: no cover - network shape
        raise PreflightError(f"could not reach the credits endpoint: {exc}") from exc
    if not isinstance(payload, dict):
        raise PreflightError("credits endpoint returned an unexpected shape")
    return payload


def parse_balance(payload: dict[str, object]) -> Balance:
    """Pull the two numbers out of OpenRouter's envelope.

    A missing field is an error rather than a zero: treating "unknown" as "no
    credits" would block every run, and treating it as "plenty" would defeat
    the check entirely.
    """
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise PreflightError("credits response had no data object")
    try:
        total = float(data["total_credits"])
        usage = float(data["total_usage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError(
            "credits response did not carry total_credits and total_usage"
        ) from exc
    return Balance(total_credits=total, total_usage=usage)


def worst_case_usd(*, models: int, max_cost_usd: float | None) -> float:
    """The most a launch is permitted to spend.

    ``max_cost_usd`` is a per-run ceiling, so launching several models multiplies
    it. Without a ceiling there is no worst case to compare against, which is
    itself worth refusing on: an unbounded run cannot be shown to be affordable.
    """
    if max_cost_usd is None or max_cost_usd <= 0:
        raise PreflightError(
            "cannot check affordability without a spend ceiling; "
            "set --max-cost-usd (or max_cost_usd on the run)"
        )
    return max_cost_usd * max(1, models)


def check_balance(
    *,
    api_key: str,
    models: int,
    max_cost_usd: float | None,
    fetcher: Fetcher | None = None,
) -> BalanceCheck:
    """Compare the account balance against the worst case. Does not raise on
    insufficiency -- the caller decides, and must print both numbers first."""
    worst_case = worst_case_usd(models=models, max_cost_usd=max_cost_usd)
    balance = parse_balance((fetcher or _fetch)(api_key))
    remaining = balance.remaining
    return BalanceCheck(
        remaining_usd=remaining,
        worst_case_usd=worst_case,
        sufficient=remaining >= worst_case,
        detail=(
            ""
            if remaining >= worst_case
            else (
                f"short by ${worst_case - remaining:.2f}: "
                f"{models} model(s) x ${max_cost_usd:.2f} ceiling"
            )
        ),
    )
