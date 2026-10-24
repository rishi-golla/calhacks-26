import json

import pytest
from fastapi.testclient import TestClient

from common.envelope import DelegationMeta, TaskEnvelope
from common.voice import CallStore, VoiceError, redact_transcript, start_call, valid_call_token


async def test_call_attempt_survives_restart_and_can_only_dial_once(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'calls.sqlite'}"
    store = CallStore(url)
    await store.initialize()
    env = TaskEnvelope(task_type="place_call", case_id="arbitrary-case")
    row = await store.prepare("attempt", env, "task", {"counterparty": "carrier"}, "private-token")
    assert valid_call_token(row, "private-token") and not valid_call_token(row, "wrong")
    assert await store.claim_dial("attempt") is True
    await store.close()
    reopened = CallStore(url)
    await reopened.initialize()
    assert (await reopened.get("attempt"))["status"] == "dialing"
    assert await reopened.claim_dial("attempt") is False
    await reopened.close()


async def test_fixture_call_uses_directory_not_payload_phone(monkeypatch, tmp_path):
    source = tmp_path / "directory.json"
    source.write_text(json.dumps({"companies": {"C-1": {
        "id": "C-1", "phone": "+14155550111", "verified": True,
        "source": "fmcsa", "source_url": "fixture://directory/C-1",
    }}}))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(source))
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15005550006")
    env = TaskEnvelope(task_type="place_call", case_id="anything", subject={
        "purpose": "verification", "counterparty_id": "C-1", "phone": "+19999999999"})
    _, result = await start_call(env, DelegationMeta(case_id=env.case_id, from_agent="verification"), "task")
    assert result.intended_to == "+14155550111" and result.routed_to == "+15005550006"
    assert result.findings["simulated"] is True


async def test_call_purpose_cannot_be_forged():
    env = TaskEnvelope(task_type="place_call", case_id="c", subject={
        "purpose": "approvals", "counterparty_id": "carrier"})
    with pytest.raises(VoiceError, match="purpose"):
        await start_call(env, DelegationMeta(case_id="c", from_agent="verification"), "task")


def test_transcript_redacts_contact_details():
    value = redact_transcript([{"role": "caller", "text": "Email a@example.com or +1 415 555 0111"}])
    assert "a@example.com" not in value and "555" not in value


def test_gateway_credentials_are_required_even_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("A2A_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("CARTESIA_WEBHOOK_SECRET", raising=False)
    from gateway.app import app
    with TestClient(app) as client:
        assert client.post("/voice/webhook", json={"id": "call"}).status_code == 401
        assert client.post("/voice/tools/get_case_summary", json={}).status_code == 401
        assert client.post("/voice/tools/approve_payment", json={}).status_code == 404
        assert client.post("/demo/phone").status_code == 503


async def test_call_projection_replays_frozen_payload_after_remote_failure(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from common import voice
    store = CallStore(f"sqlite+aiosqlite:///{tmp_path / 'replay.sqlite'}")
    await store.initialize()
    row = await store.prepare("attempt", TaskEnvelope(task_type="place_call", case_id="case"), "task", {
        "questions": [], "intended_to": "+14155550111", "routed_to": "+15005550006",
    }, "token")
    await store.set("attempt", provider_id="provider-call")
    row = await store.get("attempt")
    inference = AsyncMock(return_value={"assessment": {"summary": "caller confirmed", "claim_confirmed": True,
        "authorized_change": None, "supporting_quote": "Yes, that is my charge."}, "model_used": "test/model"})
    monkeypatch.setattr(voice, "infer", inference)
    mutate = AsyncMock(side_effect=[TimeoutError("remote write outcome unknown"), None])
    monkeypatch.setattr(voice.SpacetimeClient, "mutate", mutate)
    external = {"id": "provider-call", "status": "completed", "end_reason": "agent_hangup",
                "transcript": [{"role": "user", "text": "Yes, that is my charge."}]}
    with pytest.raises(TimeoutError):
        await voice.finish_call(store, row, external)
    row = await store.get("attempt")
    assert not row["result"] and json.loads(row["payload"])["finalization"]
    assert await voice.finish_call(store, row, external)
    assert inference.await_count == 1
    assert mutate.await_args_list[0] == mutate.await_args_list[1]
    assert json.loads((await store.get("attempt"))["result"])["findings"]["receipt_provided"] is False
    await store.close()


async def test_completed_call_replays_without_directory_or_quiet_hour_checks(monkeypatch, tmp_path):
    import uuid
    from unittest.mock import AsyncMock

    from common import voice
    from common.envelope import CallResult
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    url = f"sqlite+aiosqlite:///{tmp_path / 'complete.sqlite'}"
    monkeypatch.setenv("A2A_DATABASE_URL", url)
    task_id = "task-completed"
    attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"freightops:call:{task_id}"))
    env = TaskEnvelope(task_type="place_call", case_id="c", subject={
        "purpose": "verification", "counterparty_id": "carrier"})
    store = CallStore(url)
    await store.initialize()
    await store.prepare(attempt_id, env, task_id, {}, "secret")
    await store.set(attempt_id, result=CallResult(reached=True).model_dump_json(), status="completed")
    from common.source_repository import SourceRepository
    source = AsyncMock(side_effect=AssertionError("completed calls must not re-read fresh source"))
    monkeypatch.setattr(SourceRepository, "get_company", source)
    replay_id, result = await voice.start_call(env, DelegationMeta(case_id="c", from_agent="verification"), task_id)
    assert replay_id == attempt_id and result.reached
    source.assert_not_awaited()
    await store.close()
