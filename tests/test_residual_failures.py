"""Residual live failures after PR #41 (Oct 25 2026, Maincloud at350-freightops).

* Every Settlement Quality Review failed: a reviewed task's bundle (its artifact twice, every audit event with its
  hash chain) was 104 to 126 KB, so inspect_review_target returned tool_result_exceeds_limit and the guarded
  workflow's model pass raised "evidence exceeds inference input limit" on every attempt.
* The model then called the failing execute_current_skill up to 9 times, and the task failed as "model tool call
  budget exhausted" or "tool response incomplete (max_messages)", which hid the workflow's own error. The same loop
  ended the pre-#41 bank-change and invoice chains (a refused delegation, then ChildFailed in every parent).
* One provider transport blip failed a CFO turn outright, and with it the parent reconciliation.
"""
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from agents.validator import main as validator_main
from agents.validator.main import ValidatorAgent
from agents.validator.tools import (
    REVIEW_EVIDENCE_CHARS,
    REVIEW_TOOL_CHARS,
    deterministic_review,
    encoded_size,
    review_view,
)
from common import tool_loop, tool_registry
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.providers import ProviderConfig, ProviderError
from common.runtime import ChildFailed, DelegationRefused, FreightAgent, failure_details
from common.runtime_store import RuntimeStore
from common.tool_registry import EMPTY_PARAMETERS, ToolContext, ToolSpec
from tests.test_tool_loop import Agent, final, response
from tests.test_validator import cash_bundle


@pytest.fixture
def loop(monkeypatch):
    async def settings(*_):
        return {"provider": "openai", "model": "test", "instructions": "Evidence only",
                "instr_version": 1, "memories": []}

    async def mutate(agent, env, task_id, operation, payload):
        return {"executed": True}

    monkeypatch.setattr(tool_loop, "_settings", settings)
    monkeypatch.setattr(tool_loop, "configuration", lambda *_: ProviderConfig("openai", "test", "secret", ""))
    monkeypatch.setattr(tool_loop, "mutate", mutate)
    monkeypatch.setattr(tool_loop, "TURN_RETRY_BACKOFF_S", 0)
    return Agent(), TaskEnvelope(task_type="answer_payment_status", case_id="case"), \
        DelegationMeta(case_id="case", from_agent="router")


def guarded(monkeypatch, execute):
    async def read(ctx, args):
        return {"evidence_refs": ["source:1"]}
    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "read": ToolSpec("read", "Read", EMPTY_PARAMETERS, read),
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})


async def test_a_workflow_that_keeps_failing_ends_with_its_own_error_not_the_budget(loop, monkeypatch):
    agent, env, meta = loop
    runs = []

    async def execute(ctx, args):
        runs.append(ctx.tool_call_id)
        raise ProviderError("evidence exceeds inference input limit")

    guarded(monkeypatch, execute)
    turns = 0

    async def model(*args, **kwargs):
        nonlocal turns
        turns += 1
        return response("execute_current_skill", f"call_{turns}")  # the live model retried it 9 times

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(tool_loop.GuardedSkillFailed) as failure:
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert len(runs) == tool_loop.MAX_SKILL_FAILURES < tool_loop.MAX_TOOL_CALLS
    assert failure.value.reason == ("ProviderError: evidence exceeds inference input limit after "
                                    f"{tool_loop.MAX_SKILL_FAILURES} attempts")
    # The executor records the reason as the failed task's detail.
    assert failure_details(failure.value, 300) == {"error": "GuardedSkillFailed", "detail": failure.value.reason}


async def test_a_failed_child_ends_the_parent_at_once(loop, monkeypatch):
    agent, env, meta = loop
    runs = []

    async def execute(ctx, args):
        runs.append(ctx.tool_call_id)
        raise ChildFailed("failed", "provider transport or protocol failure")

    guarded(monkeypatch, execute)
    turns = iter([response("read", "r"), response("execute_current_skill", "x1"),
                  response("execute_current_skill", "x2")])

    async def model(*args, **kwargs):
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(tool_loop.GuardedSkillFailed, match="ChildFailed: provider transport or protocol failure$"):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert runs == ["x1"], "a closed reservation answers every retry with the same ChildFailed"


async def test_a_retry_that_succeeds_still_completes_and_replays_the_same_way(loop, monkeypatch):
    agent, env, meta = loop
    runs = []

    async def execute(ctx, args):
        runs.append(ctx.tool_call_id)
        if len(runs) < tool_loop.MAX_SKILL_FAILURES:
            raise RuntimeError("transient source read")
        return {"artifact": ResultArtifact(outcome="clear", evidence_refs=["source:1"]).model_dump()}

    guarded(monkeypatch, execute)
    decisions = [response("execute_current_skill", f"x{i}") for i in range(tool_loop.MAX_SKILL_FAILURES)] + [final()]
    turns = iter(decisions)

    async def model(*args, **kwargs):
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert result.outcome == "clear" and len(runs) == tool_loop.MAX_SKILL_FAILURES
    # A replay reads the checkpointed failures and decisions; nothing runs or is asked again.
    replay = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert replay.outcome == "clear" and len(runs) == tool_loop.MAX_SKILL_FAILURES


@pytest.mark.parametrize("message,retried", [
    ("provider transport or protocol failure", True), ("provider HTTP 503", True), ("provider HTTP 429", True),
    ("tool response incomplete (max_output_tokens)", True), ("provider HTTP 400", False),
    ("provider declined tool task", False), ("tool response exceeds size limit", False)])
def test_only_unanswered_turns_are_retryable(message, retried):
    assert tool_loop.retryable_turn_error(ProviderError(message)) is retried


async def test_a_provider_transport_blip_is_asked_again(loop, monkeypatch):
    agent, env, meta = loop

    async def execute(ctx, args):
        return {"artifact": ResultArtifact(outcome="clear").model_dump()}

    guarded(monkeypatch, execute)
    turns = iter([ProviderError("provider transport or protocol failure"), response("execute_current_skill", "x"),
                  ProviderError("provider HTTP 502"), final()])

    async def model(*args, **kwargs):
        item = next(turns)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(tool_loop, "model_turn", model)
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert result.outcome == "clear" and result.findings["tool_assessment"] is not None

    async def refused(*args, **kwargs):
        raise ProviderError("provider HTTP 400")

    monkeypatch.setattr(tool_loop, "model_turn", refused)
    with pytest.raises(ProviderError, match="HTTP 400"):
        await tool_loop.run_tool_loop(Agent(), env, meta, "task-2")


@pytest.mark.parametrize("error,detail", [
    (ProviderError("model tool call budget exhausted"), "model tool call budget exhausted"),
    (TimeoutError(), "task deadline of 300s exceeded"),
    (ChildFailed("canceled"), "child task canceled"),
    (ChildFailed("failed", "voice_desk refused the delegation: bad"), "voice_desk refused the delegation: bad"),
    (DelegationRefused("max delegation depth"), "max delegation depth"),
    # Other messages can carry source data or credentials: only the kind is recorded.
    (ValueError("api_key=sk-secret private@example.com"), "ValueError raised while executing the task"),
])
def test_every_failure_records_an_error_and_a_detail(error, detail):
    assert failure_details(error, 300) == {"error": type(error).__name__, "detail": detail}


class Worker(FreightAgent):
    name = "carrier_ap"


@pytest.fixture
def dashboard(monkeypatch):
    from common.spacetime_client import SpacetimeClient
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("A2A_PROJECTION_DELIVERY", "inline")
    state = {"rows": [], "written": []}

    async def query_rows(_self, sql):
        wanted = sql.rsplit("'", 2)[-2]
        return [row for row in state["rows"] if row["a2a_state"] == wanted]

    async def mutate(_self, reducer, payload):
        state["written"].append(payload)
    monkeypatch.setattr(SpacetimeClient, "query_rows", query_rows)
    monkeypatch.setattr(SpacetimeClient, "mutate", mutate)
    return state


async def test_the_sweep_never_reprojects_a_failure_without_a_reason(tmp_path, dashboard):
    """A task the A2A store recorded as failed, with no `__failure` checkpoint, still says it failed and why."""
    agent = Worker(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    try:
        env = TaskEnvelope(task_type="process_carrier_invoice", case_id="case-1")
        meta = DelegationMeta(case_id="case-1", from_agent="router")
        row, _ = await agent.store.admit("carrier_ap", "router", "k", env.model_dump(),
                                         meta.model_dump(exclude_none=True), {})
        await agent.store.update(row["id"], status="failed")
        stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        dashboard["rows"] = [{"task_id": row["id"], "case_id": "case-1", "skill": "process_carrier_invoice",
                              "parent_task_id": "", "a2a_state": "working", "updated_at": stale}]
        assert await agent.sweep_orphaned_tasks() == 1
        await agent.flush_projections()
        [written] = dashboard["written"]
        assert written["a2a_state"] == "failed"
        assert written["artifact_json"]["findings"] == {"error": "failed",
                                                        "detail": "the task failed without a recorded reason"}
    finally:
        await agent.store.close()


def live_bundle(events: int = 30) -> dict:
    """A bundle shaped like the live ones: a large artifact stored twice and audit events with hash chains."""
    bundle = cash_bundle()
    bundle["simulated"] = False
    findings = bundle["artifact"]["findings"]
    findings["controller"] = {"notes": ["x" * 400 for _ in range(30)]}
    findings["tool_loop"] = {"calls": [{"id": f"call_{i}", "name": "read", "status": "completed"} for i in range(14)],
                             "usage": [{"input_tokens": 1000} for _ in range(16)]}
    bundle["target"]["artifact_json"] = json.dumps(bundle["artifact"])
    summary = json.dumps({"version": 1, "tool_name": "match_carrier_invoice", "status": "completed",
                          "result": {"note": "y" * 3500}})
    bundle["history"] = [{"id": f"e{i}", "kind": "tool_call", "reason_code": "tool_completed", "sequence": i,
                          "summary": summary, "hash": "h" * 64, "prev_hash": "p" * 64, "hash_body": "b" * 600,
                          "actor_identity": "c" * 64, "evidence_refs": ["invoice.pdf"]} for i in range(events)]
    return bundle


def test_the_model_sees_a_bounded_review_bundle_with_the_checks_first():
    bundle = live_bundle()
    assert encoded_size(bundle) > 100_000, "the shape that failed every live review"
    checks = deterministic_review(bundle).checks
    view = review_view(bundle, checks)
    assert encoded_size(view) <= REVIEW_EVIDENCE_CHARS
    assert next(iter(view)) == "checks" and view["checks"] == checks
    assert "artifact_json" not in view["target"] and "tool_loop" not in view["artifact"]["findings"]
    assert view["artifact"]["evidence_refs"] == bundle["artifact"]["evidence_refs"]
    event = view["history"][-1]
    assert event["tool"] == {"tool_name": "match_carrier_invoice", "status": "completed"}
    assert not {"hash", "prev_hash", "hash_body", "actor_identity", "summary"} & set(event)
    tool = review_view(bundle, limit=REVIEW_TOOL_CHARS)
    assert encoded_size(tool) <= REVIEW_TOOL_CHARS and "checks" not in tool


def test_an_oversized_view_is_clipped_and_never_invents_citations():
    bundle = live_bundle()
    bundle["source"]["notes"] = ["z" * 5000 for _ in range(100)]
    bundle["source"]["evidence_refs"] = [f"doc:{i}" for i in range(500)]
    view = review_view(bundle, limit=REVIEW_TOOL_CHARS)
    assert encoded_size(view) <= REVIEW_TOOL_CHARS
    assert all(ref.startswith("doc:") for ref in view["source"]["evidence_refs"])
    tiny = review_view(bundle, deterministic_review(bundle).checks, limit=2_500)
    assert encoded_size(tiny) <= 2_500 and tiny["details_omitted"] is True
    assert tiny["target"]["task_id"] == "task-cash" and tiny["checks"][0]["name"] == "target_terminal"


@pytest.fixture
def review_world(monkeypatch, tmp_path):
    """Validator run_workflow against a live-shaped bundle; SpacetimeDB writes are collected."""
    from types import SimpleNamespace
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    world = {"bundle": live_bundle(), "loads": 0, "writes": {}, "evidence": []}

    async def load(*args):
        world["loads"] += 1
        return copy.deepcopy(world["bundle"])

    async def get_history(self, agent):
        return []

    async def write(self, operation, payload):
        world["writes"][operation] = payload
        return SimpleNamespace(write_id=payload["write_id"], status_code=200)

    async def infer(agent, env, evidence, *, purpose="assessment"):
        if purpose == "lesson":
            return {"assessment": {}}
        world["evidence"].append(evidence)
        if len(json.dumps(evidence, default=str)) > 100_000:  # complete()'s own limit
            raise ProviderError("evidence exceeds inference input limit")
        if world.get("infer_error"):
            raise ProviderError(world["infer_error"])
        return {"assessment": {"issues": [], "summary": "ok"}, "model_used": "openai/test"}

    monkeypatch.setattr(validator_main, "load_review", load)
    monkeypatch.setattr(validator_main, "infer", infer)
    monkeypatch.setattr("common.spacetime_client.SpacetimeClient.get_instruction_history", get_history)
    monkeypatch.setattr("common.spacetime_client.SpacetimeClient.mutate", write)
    world["store"] = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'review.sqlite'}")
    world["agent"] = ValidatorAgent(store=world["store"])
    world["env"] = TaskEnvelope(task_type="review_output", case_id="case-varied")
    world["meta"] = DelegationMeta(case_id="case-varied", from_agent="router")
    yield world


async def test_a_live_sized_review_reaches_the_model_and_completes(review_world):
    world = review_world
    try:
        result = await world["agent"].run_workflow(world["env"], world["meta"], "review-live", None)
    finally:
        await world["store"].close()
    assert result.outcome == "pass" and result.findings["assessment"]["summary"] == "ok"
    assert "assessment_error" not in result.findings
    [evidence] = world["evidence"]
    assert "checks" in evidence and encoded_size(evidence) <= REVIEW_EVIDENCE_CHARS
    assert world["writes"]["add_review"]["verdict"] == "pass"


@pytest.mark.parametrize("net_cash,verdict", [(999.89, "pass"), (123, "issue")])
async def test_a_failed_model_pass_keeps_the_deterministic_verdict(review_world, net_cash, verdict):
    world = review_world
    world["bundle"]["artifact"]["findings"]["net_cash"] = net_cash
    world["bundle"]["target"]["artifact_json"] = json.dumps(world["bundle"]["artifact"])
    world["infer_error"] = "provider HTTP 503"
    try:
        result = await world["agent"].run_workflow(world["env"], world["meta"], "review-down", None)
    finally:
        await world["store"].close()
    assert result.outcome == verdict == deterministic_review(world["bundle"]).verdict
    assert result.findings["assessment_error"] == "provider HTTP 503"
    advisory = [c for c in result.findings["review"]["checks"] if c["name"] == "qualitative_review"]
    assert advisory and advisory[0]["severity"] == "advisory" and advisory[0]["ok"] is False
    assert world["writes"]["add_review"]["verdict"] == verdict


async def test_a_failed_model_tool_loop_runs_the_deterministic_review(review_world, monkeypatch):
    world = review_world

    async def cut_short(*args, **kwargs):
        raise ProviderError("tool response incomplete (max_messages)")

    monkeypatch.setattr("common.providers.mode", lambda: "live")
    monkeypatch.setattr(tool_loop, "run_tool_loop", cut_short)
    try:
        result = await world["agent"].handle(world["env"], world["meta"], "review-loop", None)
    finally:
        await world["store"].close()
    assert result.outcome == "pass"
    assert result.findings["tool_loop_unavailable"] == "tool response incomplete (max_messages)"
    assert result.findings["simulated"] is False and world["writes"]["add_review"]["verdict"] == "pass"


async def test_review_tools_return_a_bounded_view_and_share_one_load(review_world):
    world = review_world
    agent, env = world["agent"], world["env"]
    ctx = ToolContext(agent=agent, env=env, meta=world["meta"], task_id="review-tools")
    try:
        inspected = await tool_registry.inspect_review_target(ctx, {})
        checked = await tool_registry.check_review_quality(ctx, {})
        result = await agent.run_workflow(env, world["meta"], "review-tools", None)
    finally:
        await world["store"].close()
    assert encoded_size(inspected) <= REVIEW_TOOL_CHARS < tool_loop.MAX_TOOL_RESULT_BYTES
    assert checked["review"]["verdict"] == result.outcome == "pass"
    assert world["loads"] == 1, "the tools and the guarded workflow judge one checkpointed bundle"


async def test_a_budget_that_runs_out_after_failed_executions_reports_the_workflow_error(loop, monkeypatch):
    # Devin on #44: 12 inspections leave room for only two failed executions before the call budget ends.
    agent, env, meta = loop

    async def execute(ctx, args):
        raise ProviderError("evidence exceeds inference input limit")

    guarded(monkeypatch, execute)
    calls = [response("read", f"r{i}") for i in range(tool_loop.MAX_TOOL_CALLS - 2)]
    calls += [response("execute_current_skill", f"x{i}") for i in range(3)]
    turns = iter(calls)

    async def model(*args, **kwargs):
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(tool_loop.GuardedSkillFailed) as failure:
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert failure.value.reason == "ProviderError: evidence exceeds inference input limit after 2 attempts"


@pytest.mark.parametrize("limit", ["budget", "cut_short"])
async def test_a_model_limit_after_a_failed_workflow_still_reports_the_workflow_error(loop, monkeypatch, limit):
    """Devin on #44: 12 inspections leave room for only two failed workflow calls before the call budget ends the
    loop; the task must still say why the workflow failed."""
    agent, env, meta = loop

    async def execute(ctx, args):
        raise ProviderError("evidence exceeds inference input limit")

    guarded(monkeypatch, execute)
    reads = tool_loop.MAX_TOOL_CALLS - 2
    turns = 0

    async def model(*args, **kwargs):
        nonlocal turns
        turns += 1
        if turns <= reads:
            return response("read", f"r{turns}")
        if limit == "cut_short" and turns > reads + 1:
            raise ProviderError("tool response incomplete (max_messages)")
        return response("execute_current_skill", f"x{turns}")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(tool_loop.GuardedSkillFailed) as failure:
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    expected = ("model tool call budget exhausted" if limit == "budget"
                else "tool response incomplete (max_messages)")
    assert failure.value.reason == f"ProviderError: evidence exceeds inference input limit; then {expected}"
