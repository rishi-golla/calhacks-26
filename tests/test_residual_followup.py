"""Follow-up to #44 from its cold review: the checks survive any clipping, retries respect the task deadline, a
transient model-review failure is retried before it is checkpointed, and the deterministic fallback never writes
a review twice."""
import asyncio
import json
import random
from types import SimpleNamespace

import pytest

from agents.validator import main as validator_main
from agents.validator.main import ValidatorAgent
from agents.validator.tools import (
    REVIEW_TOOL_CHARS,
    deterministic_review,
    encoded_size,
    history_view,
    review_view,
)
from common import review_worker, tool_loop, tool_registry
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.providers import ProviderConfig, ProviderError
from common.runtime_store import RuntimeStore
from common.tool_registry import EMPTY_PARAMETERS, ToolSpec
from tests.test_residual_failures import guarded, live_bundle, loop  # noqa: F401 -- `loop` is a fixture
from tests.test_tool_loop import response


def test_clipping_never_cuts_the_checks():
    """The money checks come first in a review; clipping the whole view cut them before anything else."""
    bundle = live_bundle()
    bundle["source"]["notes"] = ["z" * 5000 for _ in range(100)]  # forces the progressive clip
    checks = deterministic_review(bundle).checks
    checks.append({"name": "huge_check", "ok": False, "severity": "issue", "basis": "test",
                   "expected": "e" * 10_000, "actual": {"rows": [{"amount": i} for i in range(500)]}})
    view = review_view(bundle, checks, limit=REVIEW_TOOL_CHARS)
    assert encoded_size(view) <= REVIEW_TOOL_CHARS
    assert [(c["name"], c["ok"], c["severity"]) for c in view["checks"]] == \
        [(c["name"], c["ok"], c["severity"]) for c in checks]
    money = {c["name"]: c for c in checks if c["name"] in {"net_cash", "invoiced_margin"}}
    assert money and all(view["checks"][checks.index(c)] == c for c in money.values()), "short values stay whole"
    huge = view["checks"][-1]
    assert len(huge["expected"]) < 1000 and encoded_size(huge["actual"]) < 2000


def test_the_minimal_view_keeps_whole_citations_within_its_limit():
    bundle = live_bundle()
    bundle["source"]["notes"] = ["z" * 5000 for _ in range(100)]
    bundle["artifact"]["evidence_refs"] = [f"doc:{i:03d}:" + "r" * 400 for i in range(500)]
    for limit in (REVIEW_TOOL_CHARS, 8_000, 3_000):
        view = review_view(bundle, deterministic_review(bundle).checks, limit=limit)
        assert encoded_size(view) <= limit
        assert view.get("details_omitted", False) is (limit < REVIEW_TOOL_CHARS)
        refs = view["artifact"]["evidence_refs"]
        assert refs == bundle["artifact"]["evidence_refs"][:len(refs)], "whole citations, in order, never cut"


def test_history_keeps_the_latest_events_by_sequence():
    events = [{"sequence": i, "kind": "upsert_task", "summary": f"event {i}"} for i in range(60)]
    random.Random(7).shuffle(events)  # SQL returns rows in no particular order
    view = history_view(events)
    assert view[0] == {"omitted_earlier_events": 20}
    assert [item["sequence"] for item in view[1:]] == list(range(20, 60))


def test_reviews_get_the_first_hop_deadline():
    assert review_worker.REVIEW_DEADLINE_S == 300


async def test_three_consecutive_provider_5xx_turns_fail_the_task(loop, monkeypatch):  # noqa: F811
    agent, env, meta = loop
    attempts = 0

    async def model(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ProviderError("provider HTTP 503")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(ProviderError, match="HTTP 503"):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert attempts == tool_loop.INCOMPLETE_TURN_RETRIES + 1 == 3


async def test_a_retry_that_cannot_finish_before_the_deadline_is_not_made(loop, monkeypatch):  # noqa: F811
    """Three 120 s timeouts outlive a 300 s task; scaled down: 0.8 s attempts against a 2 s deadline."""
    agent, _, meta = loop
    env = TaskEnvelope(task_type="answer_payment_status", case_id="case", deadline_s=2)
    attempts = 0

    async def slow_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.8)
        raise ProviderError("provider transport or protocol failure")

    monkeypatch.setattr(tool_loop, "model_turn", slow_failure)
    with pytest.raises(ProviderError, match="transport"):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert attempts == 2, "the third attempt would have ended after the deadline"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    state = {"bundle": live_bundle(), "writes": [], "infer": [], "fail_memory": 0}

    async def load(*args):
        return json.loads(json.dumps(state["bundle"]))

    async def get_history(self, agent):
        return []

    async def write(self, operation, payload):
        if operation == "upsert_memory" and state["fail_memory"]:
            state["fail_memory"] -= 1
            raise RuntimeError("SpacetimeDB write failed mid-workflow")
        state["writes"].append((operation, payload["write_id"]))
        return SimpleNamespace(write_id=payload["write_id"], status_code=200)

    async def infer(agent, env, evidence, *, purpose="assessment"):
        if purpose == "lesson":
            return {"assessment": {}}
        outcome = state["infer"].pop(0) if state["infer"] else None
        if isinstance(outcome, Exception):
            raise outcome
        return {"assessment": {"issues": [], "summary": "ok"}, "model_used": "openai/test"}

    monkeypatch.setattr("agents.validator.tools.load_review", load)
    monkeypatch.setattr(validator_main, "infer", infer)
    monkeypatch.setattr(validator_main, "ASSESS_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr("common.spacetime_client.SpacetimeClient.get_instruction_history", get_history)
    monkeypatch.setattr("common.spacetime_client.SpacetimeClient.mutate", write)
    state["store"] = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'followup.sqlite'}")
    state["agent"] = ValidatorAgent(store=state["store"])
    state["env"] = TaskEnvelope(task_type="review_output", case_id="case-varied")
    state["meta"] = DelegationMeta(case_id="case-varied", from_agent="router")
    yield state


@pytest.mark.parametrize("first,recovered", [(ProviderError("provider HTTP 503"), True),
                                             (ProviderError("provider transport or protocol failure"), True),
                                             (ProviderError("provider HTTP 400"), False)])
async def test_a_transient_model_review_failure_is_retried_before_it_is_saved(world, first, recovered):
    world["infer"] = [first]
    try:
        result = await world["agent"].run_workflow(world["env"], world["meta"], "review-retry", None)
    finally:
        await world["store"].close()
    assert result.outcome == "pass"
    if recovered:
        assert "assessment_error" not in result.findings and result.findings["assessment"]["summary"] == "ok"
    else:
        assert result.findings["assessment_error"] == "provider HTTP 400"


async def test_the_fallback_after_a_mid_workflow_failure_never_writes_a_review_twice(world, monkeypatch):
    """The guarded call writes add_review, then fails on its lesson write; a model limit ends the loop and the
    deterministic fallback runs the workflow again. Every write is checkpointed, so add_review is sent once."""
    world["bundle"]["artifact"]["findings"]["net_cash"] = 123  # an issue: the workflow also writes a lesson
    world["bundle"]["target"]["artifact_json"] = json.dumps(world["bundle"]["artifact"])
    world["fail_memory"] = 1

    async def settings(*_):
        return {"provider": "openai", "model": "test", "instructions": "Review", "instr_version": 0, "memories": []}

    async def queue(payload):
        return None

    turns = 0

    async def model(*args, **kwargs):
        nonlocal turns
        turns += 1
        if turns == 1:
            return response("execute_current_skill", "x1")
        raise ProviderError("tool response incomplete (max_messages)")

    async def execute(ctx, args):
        return {"artifact": (await ctx.agent.run_workflow(ctx.env, ctx.meta, ctx.task_id, None)).model_dump()}

    monkeypatch.setattr("common.providers.mode", lambda: "live")
    monkeypatch.setattr(tool_loop, "_settings", settings)
    monkeypatch.setattr(tool_loop, "configuration", lambda *_: ProviderConfig("openai", "test", "secret", ""))
    monkeypatch.setattr(tool_loop, "model_turn", model)
    monkeypatch.setattr(tool_loop, "TURN_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})
    world["agent"].queue_tool_audit = queue
    try:
        result = await world["agent"].handle(world["env"], world["meta"], "review-mid", None)
    finally:
        await world["store"].close()
    assert result.outcome == "issue" and "tool_loop_unavailable" in result.findings
    operations = [operation for operation, _ in world["writes"]]
    assert operations.count("add_review") == 1
    assert len({write_id for _, write_id in world["writes"]}) == len(world["writes"]), "no write sent twice"
    assert "upsert_memory" in operations, "the fallback finished the write that failed"
    assert isinstance(ResultArtifact.model_validate(result.model_dump()), ResultArtifact)


# Devin on #46: optional model calls and retries budget against the executor's own deadline.

def test_hundreds_of_checks_keep_every_failure_and_count_the_passes_left_out():
    bundle = live_bundle()
    bundle["source"]["notes"] = ["z" * 5000 for _ in range(100)]
    checks = [{"name": f"receivable_journal_requested_{i}", "ok": True, "severity": "issue",
               "expected": "journal", "actual": "journal", "basis": "every matched deposit is journaled"}
              for i in range(450)]
    failures = [{"name": f"unique_deposits_{i}", "ok": False, "severity": "issue", "expected": "unique",
                 "actual": "duplicate", "basis": "a deposit counts once"} for i in range(5)]
    checks[200:200] = failures
    for limit in (REVIEW_TOOL_CHARS * 2, REVIEW_TOOL_CHARS):
        view = review_view(bundle, checks, limit=limit)
        assert encoded_size(view) <= limit
        kept = [c for c in view["checks"] if "name" in c]
        assert [c["name"] for c in kept[:5]] == [c["name"] for c in failures], "failures first, all of them"
        marker = view["checks"][-1]
        assert marker["omitted_checks"] == len(checks) - len(kept)
        assert marker["note"] == f"{len(checks) - len(kept)} more passed checks omitted"


async def test_the_executor_publishes_its_deadline_to_the_handler(tmp_path, monkeypatch):
    import time

    from common.runtime import FreightAgent, task_deadline
    from tests.test_stuck_delegations import serving

    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    seen = {}

    class Timed(FreightAgent):
        name = "voice_desk"

        async def handle(self, env, meta, task_id, updater):
            seen["left"] = task_deadline() - time.monotonic()
            return ResultArtifact(outcome="unverified")

    from a2a.helpers import new_data_message
    from a2a.server.context import ServerCallContext
    from a2a.types import Role, SendMessageRequest
    async with serving(tmp_path, Timed) as (handler, _agent):
        message = new_data_message({"task_type": "place_call", "case_id": "case-d", "deadline_s": 300},
                                   context_id="case-d", role=Role.ROLE_USER)
        message.metadata.update({"case_id": "case-d", "from_agent": "router", "idempotency_key": "timed"})
        await handler.on_message_send(SendMessageRequest(message=message), ServerCallContext())
    assert 290 < seen["left"] <= 300
    assert task_deadline() is None, "reset once the handler returns"


async def test_turn_retries_budget_against_the_executor_deadline(loop, monkeypatch):  # noqa: F811
    import time

    from common import runtime
    agent, _, meta = loop
    env = TaskEnvelope(task_type="answer_payment_status", case_id="case", deadline_s=600)  # its own clock: 600 s
    attempts = 0

    async def slow_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.8)
        raise ProviderError("provider transport or protocol failure")

    monkeypatch.setattr(tool_loop, "model_turn", slow_failure)
    token = runtime._deadline.set(time.monotonic() + 1.5)  # but the executor ends the task in 1.5 s
    try:
        with pytest.raises(ProviderError, match="transport"):
            await tool_loop.run_tool_loop(agent, env, meta, "task")
    finally:
        runtime._deadline.reset(token)
    assert attempts == 1


@pytest.mark.parametrize("case", ["slow", "late_retry"])
async def test_the_verdict_is_saved_before_the_executor_deadline(world, monkeypatch, case):
    import time

    from common import runtime
    calls = []

    async def infer(agent, env, evidence, *, purpose="assessment"):
        calls.append(purpose)
        if case == "slow":
            await asyncio.sleep(30)  # would run past the deadline
        await asyncio.sleep(0.8)
        raise ProviderError("provider HTTP 503")

    monkeypatch.setattr(validator_main, "infer", infer)
    world["bundle"]["artifact"]["findings"]["net_cash"] = 123  # an issue: the lesson draft is a model call too
    world["bundle"]["target"]["artifact_json"] = json.dumps(world["bundle"]["artifact"])
    token = runtime._deadline.set(time.monotonic() + validator_main.REVIEW_PERSIST_RESERVE_S + 1.5)
    started = time.monotonic()
    try:
        result = await world["agent"].run_workflow(world["env"], world["meta"], f"review-{case}", None)
    finally:
        runtime._deadline.reset(token)
        await world["store"].close()
    assert time.monotonic() - started < 2.5, "the reserve for saving the verdict was kept"
    assert result.outcome == "issue" and [op for op, _ in world["writes"]].count("add_review") == 1
    if case == "slow":
        assert result.findings["assessment_error"] == "model commentary ran out of time before the task deadline"
    else:
        assert result.findings["assessment_error"] == "provider HTTP 503"
        assert calls.count("review") == 1, "a retry as long as the first attempt would not fit"
