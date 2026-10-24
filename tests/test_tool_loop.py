import json

import pytest

from common import tool_loop, tool_registry
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.providers import ProviderConfig, ProviderError
from common.runtime import TaskParked
from common.tool_registry import EMPTY_PARAMETERS, ToolSpec


class Agent:
    name = "carrier_ap"

    def __init__(self):
        self.checkpoints = {}

    async def step(self, task_id, key, operation):
        if key not in self.checkpoints:
            self.checkpoints[key] = await operation()
        return self.checkpoints[key]


def response(name, call_id, arguments=None):
    args = arguments or {}
    return {"output": [{"type": "function_call", "name": name, "call_id": call_id,
                        "arguments": json.dumps(args)}],
            "calls": [{"id": call_id, "name": name, "arguments": args}], "assessment": None, "usage": {}}


def final():
    return {"output": [], "calls": [], "assessment": {"summary": "Payment remains held.",
            "issues": [], "evidence_refs": ["source:1"], "confidence": .7}, "usage": {}}


@pytest.fixture
def setup(monkeypatch):
    async def settings(*_):
        return {"provider": "openai", "model": "test", "instructions": "Evidence only",
                "instr_version": 1, "memories": []}

    events = []

    async def mutate(agent, env, task_id, operation, payload):
        assert operation == "append_audit"
        events.append(json.loads(payload["summary"]))
        return {"executed": True}

    monkeypatch.setattr(tool_loop, "_settings", settings)
    monkeypatch.setattr(tool_loop, "configuration", lambda *_: ProviderConfig("openai", "test", "secret", ""))
    monkeypatch.setattr(tool_loop, "mutate", mutate)
    return Agent(), TaskEnvelope(task_type="answer_payment_status", case_id="case"), \
        DelegationMeta(case_id="case", from_agent="router"), events


async def test_model_selects_tools_and_guarded_artifact_controls_outcome(setup, monkeypatch):
    agent, env, meta, events = setup
    executions = []

    async def read(ctx, args):
        executions.append(("read", ctx.tool_call_id))
        return {"evidence_refs": ["source:1"], "invoice": 2750}

    async def execute(ctx, args):
        executions.append(("execute", ctx.tool_call_id))
        return {"artifact": ResultArtifact(outcome="hold", confidence=.8,
                findings={"payment_executed": False, "source_simulated": True},
                policy_ids=["P-002"], evidence_refs=["source:1"]).model_dump()}

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "read": ToolSpec("read", "Read", EMPTY_PARAMETERS, read),
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})
    turns = iter([response("read", "call_1"), response("execute_current_skill", "call_2"), final()])
    seen_history = []

    async def model(*args, **kwargs):
        seen_history.append(list(args[2]))
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert result.outcome == "hold" and result.policy_ids == ["P-002"]
    assert result.findings["payment_executed"] is False
    assert result.findings["source_simulated"] is True and result.findings["simulated"] is False
    assert executions == [("read", "call_1"), ("execute", "call_2")]
    assert "tool_loop:turn:0" in agent.checkpoints
    assert len(seen_history) == 3
    assert seen_history[1][-1]["type"] == "function_call_output"
    assert [(e["tool_call_id"], e["status"]) for e in events] == [
        ("call_1", "started"), ("call_1", "completed"), ("call_2", "started"), ("call_2", "completed")]


async def test_wait_resume_reuses_model_decision_and_prior_tools(setup, monkeypatch):
    agent, env, meta, events = setup
    counts = {"read": 0, "execute": 0, "model": 0}

    async def read(ctx, args):
        counts["read"] += 1
        return {"evidence_refs": ["source:1"]}

    async def execute(ctx, args):
        assert "tool_loop:turn:1" in agent.checkpoints  # durable before any side effect
        counts["execute"] += 1
        if counts["execute"] == 1:
            raise TaskParked("awaiting_other_agent", "child", "child1", {})
        return {"artifact": ResultArtifact(outcome="clear", evidence_refs=["source:1"]).model_dump()}

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "read": ToolSpec("read", "Read", EMPTY_PARAMETERS, read),
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})
    turns = iter([response("read", "one"), response("execute_current_skill", "two"), final()])

    async def model(*args, **kwargs):
        counts["model"] += 1
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(TaskParked):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert counts == {"read": 1, "execute": 1, "model": 2}
    assert events[-1]["status"] == "waiting"
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert result.outcome == "clear"
    assert counts == {"read": 1, "execute": 2, "model": 3}
    assert [c["id"] for c in result.findings["tool_loop"]["calls"]] == ["one", "two"]


@pytest.mark.parametrize("name,args,error", [("arbitrary_sql", {}, "tool_not_permitted"),
                                             ("read", {"case_id": "another"}, "invalid_tool_arguments")])
async def test_unknown_tool_and_cross_case_arguments_are_blocked(setup, monkeypatch, name, args, error):
    agent, env, meta, events = setup

    async def forbidden(*_):
        pytest.fail("invalid call executed")

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "read": ToolSpec("read", "Read", EMPTY_PARAMETERS, forbidden)})
    turns = iter([response(name, "bad", args), {"output": [], "calls": [], "assessment": {}, "usage": {}}])

    async def model(*args, **kwargs):
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(ProviderError, match="without executing"):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert events[-1]["status"] == "blocked"
    assert events[-1]["result"]["error"] == error


async def test_tool_errors_are_safe_and_not_success(setup, monkeypatch):
    agent, env, meta, events = setup

    async def error(*_):
        raise RuntimeError("api_key=secret-name email=private@example.com")

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "read": ToolSpec("read", "Read", EMPTY_PARAMETERS, error)})
    turns = iter([response("read", "bad"), {"output": [], "calls": [], "assessment": {}, "usage": {}}])

    async def model(*args, **kwargs):
        return next(turns)

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(ProviderError):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert events[-1]["status"] == "failed"
    assert "secret-name" not in json.dumps(events)
    assert "private@example.com" not in json.dumps(events)


def test_public_tool_details_redact_secrets_personal_data_and_reasoning():
    data = tool_loop.public_value({"api_key": "secret", "phone": "+14155551234",
          "bank_account": "123456789", "reasoning": "private", "transcript": "private",
          "note": "Contact private@example.com or +1 (415) 555-1234 with sk-proj-ABCdef1234567",
          "amount": 2750, "evidence_refs": ["source:invoice:1"]})
    serialized = json.dumps(data)
    assert "private" not in serialized and "123456789" not in serialized and "ABCdef" not in serialized
    assert data["amount"] == 2750 and data["evidence_refs"] == ["source:invoice:1"]


async def test_completed_domain_action_survives_final_model_failure(setup, monkeypatch):
    agent, env, meta, _events = setup

    async def execute(ctx, args):
        return {"artifact": ResultArtifact(outcome="approve", findings={"payment_proposal": {
            "executed": True, "write_id": "persisted-write"}}, evidence_refs=["source:1"]).model_dump()}

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})
    count = 0

    async def model(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return response("execute_current_skill", "action")
        raise ProviderError("provider HTTP 503")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert result.outcome == "approve"
    assert result.findings["payment_proposal"]["executed"] is True
    assert result.findings["tool_assessment"] is None
    assert "preserved" in result.findings["tool_loop"]["assessment_error"]


async def test_audit_total_size_is_bounded(setup):
    agent, env, _, events = setup
    enormous = {str(i): {str(j): "x" * 600 for j in range(25)} for i in range(25)}
    await tool_loop._audit(agent, env, "task", {"id": "call", "name": "inspect", "arguments": enormous},
                          status="completed", model="openai/test", result=enormous)
    assert len(json.dumps(events[-1])) < 12_000
    assert events[-1]["arguments"] == {"summary": "Large arguments omitted."}


async def test_repeated_tool_calls_hit_explicit_budget(setup, monkeypatch):
    agent, env, meta, events = setup
    calls = 0

    async def read(ctx, args):
        return {}

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "read": ToolSpec("read", "Read", EMPTY_PARAMETERS, read)})

    async def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        return response("read", f"call_{calls}")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with pytest.raises(ProviderError, match="call budget"):
        await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert len([e for e in events if e["status"] == "completed"]) == tool_loop.MAX_TOOL_CALLS


async def test_settings_are_snapshotted_and_credentials_never_checkpointed(monkeypatch):
    from types import SimpleNamespace

    calls = []

    class DB:
        disabled = False

        def __init__(self, *, actor):
            calls.append(actor)

        async def get_agent_config(self, agent):
            return SimpleNamespace(enabled=True, model="openai/test")

        async def get_instruction_history(self, agent):
            return []

        async def get_agent_memory(self, agent, *, scope, limit):
            assert scope == {"skill": "answer_payment_status"}
            return [{"id": "memory1", "text": "Source evidence only"}]

    monkeypatch.setattr(tool_loop, "mode", lambda: "live")
    monkeypatch.setattr(tool_loop, "SpacetimeClient", DB)
    monkeypatch.setattr(tool_loop, "configuration", lambda *_: ProviderConfig(
        "openai", "test", "never-checkpoint-this-key", "https://api.openai.com/v1"))
    agent = Agent()
    env = TaskEnvelope(task_type="answer_payment_status", case_id="case")
    first = await agent.step("task", "tool_loop:settings", lambda: tool_loop._settings(agent, env))
    second = await agent.step("task", "tool_loop:settings", lambda: tool_loop._settings(agent, env))
    assert first == second and calls == ["carrier_ap"]
    assert first["model"] == "test" and first["memories"][0]["id"] == "memory1"
    assert "never-checkpoint-this-key" not in json.dumps(agent.checkpoints)


@pytest.mark.parametrize("reused_id", [True, False])
async def test_completion_closes_tools_and_preserves_artifact_on_extra_call(setup, monkeypatch, reused_id):
    agent, env, meta, _events = setup
    executions = 0
    model_calls = 0

    async def execute(ctx, args):
        nonlocal executions
        executions += 1
        return {"artifact": ResultArtifact(outcome="approve", findings={
            "payment_proposal": {"executed": True}}).model_dump()}

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})
    monkeypatch.setattr(tool_loop, "MAX_TOOL_CALLS", 1)

    async def model(*args, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            assert args[3] and kwargs["require_tool"] is True
            return response("execute_current_skill", "first")
        assert args[3] == [] and kwargs["require_tool"] is False
        # A cached/malformed provider decision cannot reopen completed tools,
        # even with a duplicate ID or an exhausted call budget.
        return response("execute_current_skill", "first" if reused_id else "second")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert executions == 1 and model_calls == 2
    assert result.outcome == "approve" and result.findings["payment_proposal"]["executed"] is True
    assert "after completion" in result.findings["tool_loop"]["assessment_error"]
    assert len(result.findings["tool_loop"]["calls"]) == 1


async def test_large_guarded_artifact_is_preserved_while_provider_context_is_bounded(setup, monkeypatch):
    agent, env, meta, _events = setup
    full_details = "x" * (tool_loop.MAX_TOOL_RESULT_BYTES + 1)

    async def execute(ctx, args):
        return {"artifact": ResultArtifact(outcome="approve", findings={
            "payment_proposal": {"executed": True}, "detailed_evidence": full_details},
            evidence_refs=["source:1"]).model_dump()}

    monkeypatch.setattr(tool_registry, "registry_for", lambda _: {
        "execute_current_skill": ToolSpec("execute_current_skill", "Execute", EMPTY_PARAMETERS, execute)})
    count = 0

    async def model(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return response("execute_current_skill", "action")
        assert len(json.dumps(args[2])) < tool_loop.MAX_TOOL_RESULT_BYTES
        assert json.loads(args[2][-1]["output"])["artifact"]["findings"]["details_omitted"] is True
        return final()

    monkeypatch.setattr(tool_loop, "model_turn", model)
    result = await tool_loop.run_tool_loop(agent, env, meta, "task")
    assert result.outcome == "approve"
    assert result.findings["payment_proposal"]["executed"] is True
    assert result.findings["detailed_evidence"] == full_details
    assert agent.checkpoints["tool_loop:call:action"]["artifact"]["findings"]["detailed_evidence"] == full_details


async def test_runtime_audit_outbox_is_preferred_to_direct_mutation(setup):
    agent, env, _, events = setup
    queued = []

    async def queue(payload):
        queued.append(payload)

    agent.queue_tool_audit = queue
    await tool_loop._audit(agent, env, "task", {"id": "call", "name": "inspect", "arguments": {}},
                          status="waiting", model="openai/test")
    assert events == []
    assert queued[0]["task_id"] == "task" and queued[0]["case_id"] == "case"
    assert queued[0]["reason_code"] == "tool_waiting"
