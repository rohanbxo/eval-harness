"""End-to-end API tests (SPEC Phase 4 acceptance).

Launching a run via the API with FakeModel must execute through Celery, persist
every event and assertion result, stream progress, and support cancellation.

Everything here runs against a temporary SQLite database with Celery in eager
mode, so the tests need neither Postgres nor a broker. No test in this file (or
any other) touches a real model API.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "scenarios"
# The fixture registry: the real one holds only models under evaluation.
MODELS_FILE = Path(__file__).resolve().parent / "data" / "fake_models.yaml"


@pytest.fixture(scope="module")
def _env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Point the whole stack at a temp SQLite file and an eager Celery."""
    db_path = tmp_path_factory.mktemp("db") / "evalharness-test.sqlite"
    # A file, not :memory:, because each Celery task opens its own engine.
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    previous = {
        key: os.environ.get(key)
        for key in (
            "DATABASE_URL",
            "REDIS_URL",
            "EVALHARNESS_SCENARIOS_DIR",
            "EVALHARNESS_MODELS_FILE",
            "EVALHARNESS_CELERY_ALWAYS_EAGER",
        )
    }
    os.environ.update(
        {
            "DATABASE_URL": url,
            "REDIS_URL": "memory://",
            "EVALHARNESS_SCENARIOS_DIR": str(SCENARIOS_DIR),
            "EVALHARNESS_MODELS_FILE": str(MODELS_FILE),
            "EVALHARNESS_CELERY_ALWAYS_EAGER": "1",
        }
    )

    from evalharness.api import deps
    from evalharness.config import get_settings

    get_settings.cache_clear()
    deps.reset_caches()

    yield url

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
    deps.reset_caches()


@pytest_asyncio.fixture
async def client(_env: str) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the app, with the schema created up front."""
    from evalharness.api.app import create_app
    from evalharness.api.events import MemoryEventBus, set_event_bus
    from evalharness.db.session import create_database, set_database

    database = create_database(_env)
    await database.create_all()
    set_database(database)
    set_event_bus(MemoryEventBus())

    app = create_app()
    # The app's own lifespan would install a second engine on another loop; the
    # fixture owns the database here instead.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    set_event_bus(None)
    set_database(None)
    await database.dispose()


async def _launch(
    client: AsyncClient, *, scenario: str = "travel-booking", k: int = 1, transcript: str = "golden"
) -> dict[str, Any]:
    response = await client.post(
        "/api/runs",
        json={
            "model_key": "fake",
            "scenario_ids": [scenario],
            "k": k,
            "transcript": transcript,
            # Test runs are throwaway; the tree is routinely dirty in development
            # and the guard is exercised directly in test_manifest.py.
            "allow_dirty": True,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ------------------------------------------------------------------ read-only routes


async def test_health_is_ok(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_scenarios_list_all_five(client: AsyncClient) -> None:
    response = await client.get("/api/scenarios")
    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    ids = {item["id"] for item in scenarios}
    assert ids == {
        "travel-booking",
        "refund-policy",
        "data-analyst",
        "meeting-scheduler",
        "research-injection",
    }
    assert all(item["valid"] for item in scenarios)
    assert all(item["config_hash"] for item in scenarios)


async def test_scenario_detail_includes_tools_and_fixtures(client: AsyncClient) -> None:
    response = await client.get("/api/scenarios/travel-booking")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert {tool["name"] for tool in body["tools"]} == {
        "search_flights",
        "get_fare_rules",
        "book_flight",
        "cancel_booking",
    }


async def test_unknown_scenario_is_404(client: AsyncClient) -> None:
    assert (await client.get("/api/scenarios/nope")).status_code == 404


async def test_models_report_key_presence_without_leaking_keys(client: AsyncClient) -> None:
    response = await client.get("/api/models")
    assert response.status_code == 200
    body = response.json()
    fake = next(item for item in body["models"] if item["key"] == "fake")
    # FakeModel needs no key, so it is always runnable.
    assert fake["api_key_present"] is True
    # Whatever the environment holds, no key material may appear in the payload.
    assert "sk-" not in json.dumps(body)
    for item in body["models"]:
        assert set(item) >= {"key", "display_name", "litellm_model", "api_key_present"}


# ----------------------------------------------------------------- the full run path


async def test_run_executes_through_celery_and_persists_everything(client: AsyncClient) -> None:
    """SPEC Phase 4 acceptance, in one test."""
    run = await _launch(client, k=2)
    assert run["model_key"] == "fake"
    assert run["k"] == 2
    assert run["config_hashes"]["travel-booking"]
    assert run["git_commit"]
    assert len(run["attempts"]) == 2

    # Eager Celery means the run has already finished by the time POST returns.
    detail = (await client.get(f"/api/runs/{run['id']}")).json()
    assert detail["status"] == "completed"
    assert all(attempt["status"] == "completed" for attempt in detail["attempts"])
    assert all(attempt["passed"] for attempt in detail["attempts"])

    summary = detail["summary"]
    assert summary is not None
    assert summary["total_attempts"] == 2
    assert summary["passed_attempts"] == 2
    assert summary["pass_at_1"] == 1.0
    assert summary["pass_hat_k"] == 1.0
    # The golden transcript is defined as scoring every axis (SPEC 6.4).
    assert summary["axis_scores"]
    assert all(score == 1.0 for score in summary["axis_scores"].values())

    # The trace must be complete enough to render the viewer without more calls.
    attempt_id = detail["attempts"][0]["id"]
    trace = (await client.get(f"/api/attempts/{attempt_id}")).json()
    assert trace["scenario_id"] == "travel-booking"
    assert trace["scenario_title"]
    assert trace["turns"]
    assert trace["assertion_results"]
    assert all(row["passed"] for row in trace["assertion_results"])

    events = trace["events"]
    assert events, "an attempt with no events has no audit trail"
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
    types = {event["type"] for event in events}
    assert {"model_request", "model_response", "tool_call", "tool_result"} <= types

    # Every tool call the transcript scripts must be recorded with its arguments.
    tool_calls = [event for event in events if event["type"] == "tool_call"]
    assert {call["payload"]["name"] for call in tool_calls} == {
        "search_flights",
        "get_fare_rules",
        "book_flight",
    }


async def test_failing_transcript_is_recorded_with_its_reasons(client: AsyncClient) -> None:
    run = await _launch(client, transcript="fail_cheapest")
    detail = (await client.get(f"/api/runs/{run['id']}")).json()
    assert detail["status"] == "completed"
    assert detail["summary"]["passed_attempts"] == 0
    assert detail["summary"]["pass_hat_k"] == 0.0

    trace = (await client.get(f"/api/attempts/{detail['attempts'][0]['id']}")).json()
    failed = [row for row in trace["assertion_results"] if not row["passed"]]
    assert {row["assertion_id"] for row in failed} == {
        "booked-correct-flight",
        "checked-cheaper-fares",
        "rules-before-booking",
    }
    # A failure with no explanation cannot be rendered in the trace viewer.
    assert all(row["reason"] for row in failed)


async def test_unknown_attempt_is_404(client: AsyncClient) -> None:
    assert (await client.get("/api/attempts/does-not-exist")).status_code == 404


# ---------------------------------------------------------------------- run listing


async def test_runs_are_listed_and_filterable(client: AsyncClient) -> None:
    await _launch(client)
    await _launch(client, scenario="refund-policy")

    listing = (await client.get("/api/runs", params={"limit": 50})).json()
    assert listing["total"] >= 2
    assert listing["items"], "listing must be newest-first and non-empty"

    by_model = (await client.get("/api/runs", params={"model_key": "fake"})).json()
    assert all(item["model_key"] == "fake" for item in by_model["items"])

    none_match = (await client.get("/api/runs", params={"model_key": "no-such-model"})).json()
    assert none_match["total"] == 0
    assert none_match["items"] == []


async def test_unknown_run_is_404(client: AsyncClient) -> None:
    assert (await client.get("/api/runs/does-not-exist")).status_code == 404


# -------------------------------------------------------------------- bad requests


async def test_unknown_model_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/runs",
        json={
            "model_key": "nope",
            "scenario_ids": ["travel-booking"],
            "k": 1,
            "allow_dirty": True,
        },
    )
    assert response.status_code == 404


async def test_unknown_scenario_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/runs",
        json={"model_key": "fake", "scenario_ids": ["nope"], "k": 1, "allow_dirty": True},
    )
    assert response.status_code == 400
    assert "nope" in response.text


# -------------------------------------------------------------------- cancellation


async def test_cancelling_a_finished_run_reports_nothing_to_cancel(client: AsyncClient) -> None:
    run = await _launch(client)
    response = await client.post(f"/api/runs/{run['id']}/cancel")
    assert response.status_code == 200
    body = response.json()
    # The run already completed under eager Celery, so no attempts were pending.
    assert body["cancelled_attempts"] == 0


async def test_cancel_flag_stops_queued_attempts(client: AsyncClient) -> None:
    """Cancellation is checked between model calls, so a pre-cancelled run does nothing."""
    from evalharness.db import repository
    from evalharness.db.session import get_database
    from evalharness.schema.enums import RunStatus
    from evalharness.worker.tasks import execute_run

    database = get_database()
    async with database.session() as session:
        run = await repository.create_run(
            session,
            model_key="fake",
            litellm_model="fake/transcript",
            params={"transcript": "golden"},
            k=2,
            scenario_ids=["travel-booking"],
            config_hashes={},
            git_commit="test",
            harness_version="0.0.0",
            scenario_snapshots={},
        )
        run_id = run.id
        await repository.set_run_status(session, run_id, RunStatus.CANCELLED)

    result = execute_run.apply(args=[run_id]).get()
    assert result["cancelled"] is True
    assert result["dispatched"] == 0


# --------------------------------------------------------------------------- SSE


async def test_stream_reports_a_completed_run(client: AsyncClient) -> None:
    run = await _launch(client)
    async with client.stream("GET", f"/api/runs/{run['id']}/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in response.aiter_text():
            body += chunk
            if "run_completed" in body:
                break
    assert "run_completed" in body
    assert run["id"] in body


# ------------------------------------------------------------ leaderboard + compare


async def test_leaderboard_aggregates_completed_runs(client: AsyncClient) -> None:
    await _launch(client, scenario="travel-booking", k=2)
    await _launch(client, scenario="research-injection")

    board = (await client.get("/api/leaderboard")).json()
    assert board["generated_at"]
    assert {item["scenario_id"] for item in board["scenarios"]} >= {"travel-booking"}

    row = next(item for item in board["rows"] if item["model_key"] == "fake")
    assert row["display_name"]
    assert row["scenarios_covered"] >= 1
    assert 0.0 <= row["pass_at_1"] <= 1.0
    assert 0.0 <= row["pass_hat_k"] <= 1.0
    # Every cell must name the run and config hash it came from, so the UI can
    # show what is being compared rather than silently mixing definitions.
    for cell in row["cells"]:
        assert cell["run_id"]
        assert cell["config_hash"]
        assert cell["config_changed"] is False


async def test_compare_shows_deltas_and_flipped_assertions(client: AsyncClient) -> None:
    good = await _launch(client, transcript="golden")
    bad = await _launch(client, transcript="fail_cheapest")

    response = await client.get("/api/compare", params={"run_a": good["id"], "run_b": bad["id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["run_a"]["id"] == good["id"]
    assert body["run_b"]["id"] == bad["id"]

    diff = next(d for d in body["scenarios"] if d["scenario_id"] == "travel-booking")
    assert diff["pass_at_1_a"] == 1.0
    assert diff["pass_at_1_b"] == 0.0
    assert diff["pass_at_1_delta"] == -1.0
    # Same scenario on both sides, so nothing changed underneath the comparison.
    assert diff["config_changed"] is False

    broken = {f["assertion_id"] for f in body["flipped_assertions"] if f["direction"] == "broken"}
    assert broken == {"booked-correct-flight", "checked-cheaper-fares", "rules-before-booking"}


async def test_compare_with_unknown_run_is_404(client: AsyncClient) -> None:
    run = await _launch(client)
    response = await client.get("/api/compare", params={"run_a": run["id"], "run_b": "nope"})
    assert response.status_code == 404


# ------------------------------------------- leaderboard: two models and drift


async def _seed_completed_run(
    *,
    model_key: str,
    scenario_id: str,
    config_hash: str,
    pass_at_1: float,
    pass_hat_k: float,
    k: int = 1,
) -> str:
    """Write a finished run straight to the database.

    Going through the API would force the config hash to match the scenario on
    disk, and a fabricated hash is exactly what the "config changed" badge needs.
    """
    from evalharness.api.schemas import RunSummary, ScenarioStats
    from evalharness.db import repository
    from evalharness.db.session import get_database
    from evalharness.schema.enums import RunStatus

    stats = ScenarioStats(
        scenario_id=scenario_id,
        config_hash=config_hash,
        attempts=k,
        completed=k,
        passed=round(pass_at_1 * k),
        pass_at_1=pass_at_1,
        pass_hat_k=pass_hat_k,
        axis_scores={"selection": pass_at_1, "safety": 1.0},
        cost_usd=0.0,
        latency_p50_ms=120.0,
        latency_p95_ms=200.0,
    )
    summary = RunSummary(
        k=k,
        total_attempts=k,
        completed_attempts=k,
        passed_attempts=round(pass_at_1 * k),
        pass_at_1=pass_at_1,
        pass_hat_k=pass_hat_k,
        axis_scores={"selection": pass_at_1, "safety": 1.0},
        cost_usd=0.0,
        latency_p50_ms=120.0,
        latency_p95_ms=200.0,
        per_scenario=[stats],
    )

    database = get_database()
    async with database.session() as session:
        run = await repository.create_run(
            session,
            model_key=model_key,
            litellm_model="fake/transcript",
            params={},
            k=k,
            scenario_ids=[scenario_id],
            config_hashes={scenario_id: config_hash},
            git_commit="test",
            harness_version="0.1.0",
            scenario_snapshots={},
        )
        run_id = str(run.id)
    async with database.session() as session:
        await repository.set_run_summary(
            session, run_id, summary.model_dump(mode="json"), RunStatus.COMPLETED
        )
    return run_id


async def _current_hash(client: AsyncClient, scenario_id: str) -> str:
    listing = (await client.get("/api/scenarios")).json()["scenarios"]
    return str(next(item for item in listing if item["id"] == scenario_id)["config_hash"])


async def test_leaderboard_ranks_two_models(client: AsyncClient) -> None:
    """SPEC Phase 7 acceptance: two model keys, rendered side by side."""
    live_hash = await _current_hash(client, "travel-booking")
    await _seed_completed_run(
        model_key="fake",
        scenario_id="travel-booking",
        config_hash=live_hash,
        pass_at_1=1.0,
        pass_hat_k=1.0,
        k=3,
    )
    await _seed_completed_run(
        model_key="fake-b",
        scenario_id="travel-booking",
        config_hash=live_hash,
        pass_at_1=1 / 3,
        pass_hat_k=0.0,
        k=3,
    )

    board = (await client.get("/api/leaderboard")).json()
    rows = {row["model_key"]: row for row in board["rows"]}
    assert {"fake", "fake-b"} <= set(rows)

    assert rows["fake"]["pass_hat_k"] == 1.0
    assert rows["fake-b"]["pass_hat_k"] == 0.0
    # The stronger model sorts first: rows order by pass^k, then pass@1.
    ordered = [row["model_key"] for row in board["rows"]]
    assert ordered.index("fake") < ordered.index("fake-b")

    # Passing 1 of 3 is not the same as passing every time, which is the whole
    # reason pass^k is reported next to pass@1.
    assert rows["fake-b"]["pass_at_1"] == pytest.approx(1 / 3)
    assert rows["fake-b"]["display_name"] == "FakeModel B (scripted transcripts)"

    assert all(cell["config_changed"] is False for row in board["rows"] for cell in row["cells"])


async def test_leaderboard_flags_a_scenario_whose_config_changed(client: AsyncClient) -> None:
    """A run graded against an older definition must be badged, not silently mixed."""
    await _seed_completed_run(
        model_key="fake-b",
        scenario_id="refund-policy",
        config_hash="0" * 64,  # not the definition now on disk
        pass_at_1=1.0,
        pass_hat_k=1.0,
    )

    board = (await client.get("/api/leaderboard")).json()
    row = next(row for row in board["rows"] if row["model_key"] == "fake-b")
    cell = next(cell for cell in row["cells"] if cell["scenario_id"] == "refund-policy")

    assert cell["config_changed"] is True
    assert row["has_config_drift"] is True

    # The UI needs both hashes to explain the badge.
    assert cell["config_hash"] == "0" * 64
    scenario = next(s for s in board["scenarios"] if s["scenario_id"] == "refund-policy")
    assert scenario["current_config_hash"] not in (None, "0" * 64)


async def test_leaderboard_keeps_only_the_latest_run_per_model_and_scenario(
    client: AsyncClient,
) -> None:
    live_hash = await _current_hash(client, "meeting-scheduler")
    await _seed_completed_run(
        model_key="fake",
        scenario_id="meeting-scheduler",
        config_hash=live_hash,
        pass_at_1=0.0,
        pass_hat_k=0.0,
    )
    newer = await _seed_completed_run(
        model_key="fake",
        scenario_id="meeting-scheduler",
        config_hash=live_hash,
        pass_at_1=1.0,
        pass_hat_k=1.0,
    )

    board = (await client.get("/api/leaderboard")).json()
    row = next(row for row in board["rows"] if row["model_key"] == "fake")
    cells = [cell for cell in row["cells"] if cell["scenario_id"] == "meeting-scheduler"]
    assert len(cells) == 1
    assert cells[0]["run_id"] == newer
    assert cells[0]["pass_at_1"] == 1.0


async def test_compare_marks_a_scenario_whose_config_changed(client: AsyncClient) -> None:
    live_hash = await _current_hash(client, "data-analyst")
    old = await _seed_completed_run(
        model_key="fake",
        scenario_id="data-analyst",
        config_hash="1" * 64,
        pass_at_1=1.0,
        pass_hat_k=1.0,
    )
    new = await _seed_completed_run(
        model_key="fake-b",
        scenario_id="data-analyst",
        config_hash=live_hash,
        pass_at_1=0.0,
        pass_hat_k=0.0,
    )

    body = (await client.get("/api/compare", params={"run_a": old, "run_b": new})).json()
    diff = next(d for d in body["scenarios"] if d["scenario_id"] == "data-analyst")

    assert diff["config_changed"] is True
    assert diff["config_hash_a"] == "1" * 64
    assert diff["config_hash_b"] == live_hash
    # The delta is still computed; the badge is what says not to trust it.
    assert diff["pass_at_1_delta"] == -1.0


async def test_compare_reports_axis_deltas(client: AsyncClient) -> None:
    live_hash = await _current_hash(client, "travel-booking")
    strong = await _seed_completed_run(
        model_key="fake",
        scenario_id="travel-booking",
        config_hash=live_hash,
        pass_at_1=1.0,
        pass_hat_k=1.0,
    )
    weak = await _seed_completed_run(
        model_key="fake-b",
        scenario_id="travel-booking",
        config_hash=live_hash,
        pass_at_1=0.0,
        pass_hat_k=0.0,
    )

    body = (await client.get("/api/compare", params={"run_a": strong, "run_b": weak})).json()
    diff = next(d for d in body["scenarios"] if d["scenario_id"] == "travel-booking")
    assert diff["axis_deltas"]["selection"] == -1.0
    # safety scored 1.0 on both sides, so it is not reported as a change.
    assert "safety" not in diff["axis_deltas"]


# ------------------------------------------- errored attempts and coverage


async def _seed_partial_run(
    *, model_key: str, scenario_id: str, config_hash: str, graded: int, errored: int
) -> str:
    """A run where some attempts never produced a verdict (DECISIONS D19)."""
    from evalharness.api.schemas import RunSummary, ScenarioStats
    from evalharness.db import repository
    from evalharness.db.session import get_database
    from evalharness.schema.enums import AttemptStatus, RunStatus

    k = graded + errored
    stats = ScenarioStats(
        scenario_id=scenario_id,
        config_hash=config_hash,
        attempts=k,
        completed=graded,
        errored=errored,
        coverage=graded / k if k else 0.0,
        complete=errored == 0,
        passed=graded,
        pass_at_1=1.0 if graded else 0.0,
        pass_hat_k=1.0 if errored == 0 else 0.0,
        axis_scores={"safety": 1.0},
        cost_usd=0.0,
    )
    summary = RunSummary(
        k=k,
        total_attempts=k,
        completed_attempts=graded,
        coverage=graded / k if k else 0.0,
        incomplete=errored > 0,
        scenarios_scored=0 if errored else 1,
        scenarios_total=1,
        passed_attempts=graded,
        errored_attempts=errored,
        pass_at_1=1.0 if graded else 0.0,
        pass_hat_k=1.0 if errored == 0 else 0.0,
        axis_scores={"safety": 1.0},
        cost_usd=0.0,
        per_scenario=[stats],
    )

    database = get_database()
    async with database.session() as session:
        run = await repository.create_run(
            session,
            model_key=model_key,
            litellm_model="fake/transcript",
            params={},
            k=k,
            scenario_ids=[scenario_id],
            config_hashes={scenario_id: config_hash},
            git_commit="test",
            harness_version="0.1.0",
            scenario_snapshots={},
        )
        run_id = str(run.id)
        for attempt in await repository.list_attempts(session, run_id):
            if attempt.repetition > graded:
                await repository.set_attempt_status(
                    session, attempt.id, AttemptStatus.ERRORED, error="provider gave up"
                )
    async with database.session() as session:
        await repository.set_run_summary(
            session, run_id, summary.model_dump(mode="json"), RunStatus.COMPLETED
        )
    return run_id


async def test_a_partial_run_is_reported_as_incomplete(client: AsyncClient) -> None:
    live_hash = await _current_hash(client, "travel-booking")
    run_id = await _seed_partial_run(
        model_key="fake",
        scenario_id="travel-booking",
        config_hash=live_hash,
        graded=1,
        errored=2,
    )

    summary = (await client.get(f"/api/runs/{run_id}")).json()["summary"]
    assert summary["incomplete"] is True
    assert summary["errored_attempts"] == 2
    assert summary["coverage"] == pytest.approx(1 / 3)
    # Everything that ran passed, so the rate is 1.0 -- the coverage flag is what
    # stops that being read as "this model is perfect".
    assert summary["pass_at_1"] == 1.0
    assert summary["scenarios_scored"] == 0


async def test_errored_attempts_carry_their_own_status(client: AsyncClient) -> None:
    live_hash = await _current_hash(client, "travel-booking")
    run_id = await _seed_partial_run(
        model_key="fake",
        scenario_id="travel-booking",
        config_hash=live_hash,
        graded=1,
        errored=1,
    )
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    statuses = {a["status"] for a in detail["attempts"]}
    assert "errored" in statuses
    errored = next(a for a in detail["attempts"] if a["status"] == "errored")
    assert errored["error"]


async def test_leaderboard_flags_an_incomplete_cell(client: AsyncClient) -> None:
    live_hash = await _current_hash(client, "meeting-scheduler")
    await _seed_partial_run(
        model_key="fake-b",
        scenario_id="meeting-scheduler",
        config_hash=live_hash,
        graded=1,
        errored=2,
    )

    board = (await client.get("/api/leaderboard")).json()
    row = next(r for r in board["rows"] if r["model_key"] == "fake-b")
    cell = next(c for c in row["cells"] if c["scenario_id"] == "meeting-scheduler")

    assert cell["incomplete"] is True
    assert cell["coverage"] == pytest.approx(1 / 3)
    assert row["has_incomplete"] is True


async def test_compare_flags_an_incomplete_side(client: AsyncClient) -> None:
    live_hash = await _current_hash(client, "research-injection")
    whole = await _seed_completed_run(
        model_key="fake",
        scenario_id="research-injection",
        config_hash=live_hash,
        pass_at_1=1.0,
        pass_hat_k=1.0,
    )
    partial = await _seed_partial_run(
        model_key="fake-b",
        scenario_id="research-injection",
        config_hash=live_hash,
        graded=1,
        errored=1,
    )

    body = (await client.get("/api/compare", params={"run_a": whole, "run_b": partial})).json()
    diff = next(d for d in body["scenarios"] if d["scenario_id"] == "research-injection")
    assert diff["incomplete_a"] is False
    assert diff["incomplete_b"] is True
