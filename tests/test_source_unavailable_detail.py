"""A SourceUnavailable names why the source is unavailable (live Burst, Oct 25 2026, Maincloud at350-freightops).

Burst cases failed as "GuardedSkillFailed: SourceUnavailable after 3 attempts". The tool boundary kept only the
exception type, so the tool result, the public audit row, the task's findings.detail and the agent log all lacked
the reason, and an operator could not tell a missing source record from a misconfigured source backend. Every
SourceUnavailable message is code-authored ("case is absent from the configured source dataset", "source dataset
could not be read: SpacetimeDB request timed out", ...), so the reason is now recorded wherever the type is.
"""
import json
import logging

import pytest

from common import tool_loop
from common.runtime import failure_details
from common.source_repository import NotInDirectory, SourceUnavailable
from tests.test_residual_failures import guarded, loop  # noqa: F401 -- shared tool-loop fixture
from tests.test_tool_loop import response

ABSENT = "case is absent from the configured source dataset"


def tool_outputs(history) -> list[dict]:
    return [json.loads(item["output"]) for item in history if item.get("type") == "function_call_output"]


async def test_a_source_failure_names_its_reason_in_the_tool_result_task_detail_and_log(loop, monkeypatch, caplog):  # noqa: F811
    agent, env, meta = loop
    seen = []

    async def execute(ctx, args):
        raise SourceUnavailable(ABSENT)

    guarded(monkeypatch, execute)
    turns = 0

    async def model(config, system, history, *args, **kwargs):
        nonlocal turns
        seen[:] = tool_outputs(history)
        turns += 1
        return response("execute_current_skill", f"call_{turns}")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with caplog.at_level(logging.WARNING, logger="common.tool_loop"), \
            pytest.raises(tool_loop.GuardedSkillFailed) as failure:
        await tool_loop.run_tool_loop(agent, env, meta, "task-src")

    # What the model saw (and what the public audit summary carries).
    assert seen[0] == {"error": "tool_execution_failed", "error_type": "SourceUnavailable",
                       "execution_status": "unknown", "detail": ABSENT}
    # The task's findings.detail, through the executor.
    reason = f"SourceUnavailable: {ABSENT} after {tool_loop.MAX_SKILL_FAILURES} attempts"
    assert failure.value.reason == reason
    assert failure_details(failure.value, 300) == {"error": "GuardedSkillFailed", "detail": reason}
    # And the agent log, once per failed call.
    lines = [r.getMessage() for r in caplog.records if "tool execute_current_skill failed" in r.getMessage()]
    assert len(lines) == tool_loop.MAX_SKILL_FAILURES
    assert lines[0] == f"carrier_ap task task-src tool execute_current_skill failed: SourceUnavailable: {ABSENT}"


async def test_other_exceptions_still_record_only_their_type(loop, monkeypatch, caplog):  # noqa: F811
    agent, env, meta = loop
    seen = []

    async def execute(ctx, args):
        raise ValueError("api_key=sk-secret private@example.com")

    guarded(monkeypatch, execute)
    turns = 0

    async def model(config, system, history, *args, **kwargs):
        nonlocal turns
        seen[:] = tool_outputs(history)
        turns += 1
        return response("execute_current_skill", f"call_{turns}")

    monkeypatch.setattr(tool_loop, "model_turn", model)
    with caplog.at_level(logging.WARNING, logger="common.tool_loop"), pytest.raises(tool_loop.GuardedSkillFailed):
        await tool_loop.run_tool_loop(agent, env, meta, "task-other")
    assert seen[0] == {"error": "tool_execution_failed", "error_type": "ValueError", "execution_status": "unknown"}
    assert "secret" not in caplog.text and "example.com" not in caplog.text


@pytest.mark.parametrize("error", [SourceUnavailable(ABSENT),
                                   NotInDirectory("no independently verified contact is available")])
def test_a_task_that_fails_on_its_source_records_the_reason(error):
    assert failure_details(error, 300) == {"error": type(error).__name__, "detail": str(error)}
