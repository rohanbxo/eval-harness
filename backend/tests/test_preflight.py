"""The pre-flight balance check (DECISIONS D36).

Credits running out mid-run does not fail cleanly: some attempts complete and
others error, so the run reports reduced coverage that reads like a capability
difference. One run lost 22 attempts across two models exactly this way. These
tests fix the arithmetic and the refusal, and never touch the network -- the
fetcher is injected.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer

from evalharness.cli import _check_balance
from evalharness.runner.preflight import (
    Balance,
    BalanceCheck,
    Fetcher,
    PreflightError,
    check_balance,
    parse_balance,
    worst_case_usd,
)
from evalharness.schema.registry import ModelEntry


def fetcher(total: float, usage: float) -> Fetcher:
    def fetch(api_key: str) -> dict[str, object]:
        assert api_key, "the key must be passed through"
        return {"data": {"total_credits": total, "total_usage": usage}}

    return fetch


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #


def test_remaining_is_granted_minus_used() -> None:
    assert Balance(total_credits=10.0, total_usage=2.5).remaining == 7.5


def test_the_envelope_is_unwrapped() -> None:
    balance = parse_balance({"data": {"total_credits": 5.0, "total_usage": 1.0}})
    assert balance.remaining == 4.0


def test_a_bare_body_also_parses() -> None:
    """Some gateways return the object without the data wrapper."""
    assert parse_balance({"total_credits": 3.0, "total_usage": 0.0}).remaining == 3.0


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"total_credits": 5.0}},
        {"data": {"total_usage": 5.0}},
        {"data": "nonsense"},
        {},
    ],
)
def test_a_missing_field_is_an_error_not_a_zero(payload: dict[str, object]) -> None:
    """Guessing either way defeats the check: zero blocks everything, and a
    large default waves through the run this exists to stop."""
    with pytest.raises(PreflightError):
        parse_balance(payload)


# --------------------------------------------------------------------------- #
# The worst case                                                               #
# --------------------------------------------------------------------------- #


def test_the_worst_case_scales_with_the_number_of_models() -> None:
    assert worst_case_usd(models=4, max_cost_usd=2.0) == 8.0


@pytest.mark.parametrize("ceiling", [None, 0.0])
def test_an_unbounded_run_cannot_be_shown_affordable(ceiling: float | None) -> None:
    with pytest.raises(PreflightError, match="spend ceiling"):
        worst_case_usd(models=1, max_cost_usd=ceiling)


# --------------------------------------------------------------------------- #
# The verdict                                                                  #
# --------------------------------------------------------------------------- #


def test_a_sufficient_balance_passes() -> None:
    check = check_balance(api_key="k", models=4, max_cost_usd=2.0, fetcher=fetcher(20.0, 5.0))
    assert check.sufficient is True
    assert check.remaining_usd == 15.0
    assert check.worst_case_usd == 8.0


def test_an_insufficient_balance_is_refused_with_both_numbers() -> None:
    check = check_balance(api_key="k", models=4, max_cost_usd=2.0, fetcher=fetcher(10.0, 8.0))
    assert check.sufficient is False
    assert check.remaining_usd == 2.0
    assert check.worst_case_usd == 8.0
    assert "short by $6.00" in check.detail
    rendered = check.render()
    assert "$2.00" in rendered and "$8.00" in rendered and "INSUFFICIENT" in rendered


def test_exactly_enough_is_allowed() -> None:
    """The boundary goes to the run: the ceiling is already the pessimistic case."""
    check = check_balance(api_key="k", models=1, max_cost_usd=2.0, fetcher=fetcher(2.0, 0.0))
    assert check.sufficient is True


def test_both_numbers_are_printed_even_when_it_passes() -> None:
    check = check_balance(api_key="k", models=1, max_cost_usd=1.0, fetcher=fetcher(50.0, 0.0))
    assert "balance $50.00" in check.render()
    assert "worst case $1.00" in check.render()


# --------------------------------------------------------------------------- #
# What the CLI actually checks                                                 #
# --------------------------------------------------------------------------- #


def model(**overrides: Any) -> ModelEntry:
    base: dict[str, Any] = {
        "key": "paid",
        "display_name": "Paid",
        "litellm_model": "openrouter/vendor/model",
        "api_key_env": "OPENROUTER_API_KEY",
    }
    base.update(overrides)
    return ModelEntry.model_validate(base)


def test_a_fake_model_is_never_asked_about_money(monkeypatch: pytest.MonkeyPatch) -> None:
    """FakeModel replays a transcript: nothing is spent and nobody is called.

    This is also what keeps the whole CLI test suite off the network.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a fake model must not reach the credits endpoint")

    monkeypatch.setattr("evalharness.cli.check_balance", explode)
    _check_balance(model(litellm_model="fake/replay"), max_cost_usd=2.0)


def test_a_missing_key_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no request may be made without a key")

    monkeypatch.setattr("evalharness.cli.check_balance", explode)
    with pytest.raises(typer.Exit):
        _check_balance(model(), max_cost_usd=2.0)


def test_an_insufficient_balance_stops_the_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setattr(
        "evalharness.cli.check_balance",
        lambda **kwargs: BalanceCheck(
            remaining_usd=1.0, worst_case_usd=8.0, sufficient=False, detail="short by $7.00"
        ),
    )
    with pytest.raises(typer.Exit):
        _check_balance(model(), max_cost_usd=2.0)


def test_a_sufficient_balance_lets_the_launch_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setattr(
        "evalharness.cli.check_balance",
        lambda **kwargs: BalanceCheck(remaining_usd=50.0, worst_case_usd=8.0, sufficient=True),
    )
    _check_balance(model(), max_cost_usd=2.0)
