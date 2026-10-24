"""The demo console grants one bounded team call without making real phone calls."""
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from common import demo_calls as demo
from common import voice
from common.envelope import DelegationMeta, TaskEnvelope

PHONE = "+15005550006"


@pytest.fixture
async def store(monkeypatch, tmp_path):
    for key, value in {"DEMO_PHONE_ENABLED": "1", "DEMO_CONTROL_TOKEN": "private-test-control",
                       "FREIGHTOPS_MODE": "live", "OUTREACH_MODE": "demo",
                       "A2A_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'demo.sqlite'}"}.items():
        monkeypatch.setenv(key, value)

    async def ready():
        return ({"enabled": True, "ready": True, "reason": "Ready", "destination_suffix": "0006",
                 "hours_override": True, "case_id": demo.CASE_ID}, {"phone": PHONE, "contact_id": ""})

    monkeypatch.setattr(demo, "readiness", ready)
    result = demo.DemoCallStore()
    await result.initialize()

    async def accept(agent, env, meta):
        row, _ = await result.runtime.admit(agent, meta.from_agent, meta.idempotency_key,
                                           env.model_dump(), meta.model_dump(exclude_none=True), {})
        return SimpleNamespace(id=row["id"])

    monkeypatch.setattr(demo, "send_task", AsyncMock(side_effect=accept))
    yield result
    await result.runtime.close()


async def child(store, request_id):
    root_id = demo.root_task_id(request_id)
    await store.runtime.update(root_id, status="waiting")
    env = TaskEnvelope(task_type="place_call", case_id=demo.CASE_ID, subject={
        "purpose": "verification", "counterparty_id": demo.COMPANY_ID,
        "questions": ["What supports the charge?"], "facts": {"load_id": "synthetic"}})
    meta = DelegationMeta(case_id=demo.CASE_ID, from_agent="verification", parent_task_id=root_id,
        delegation_depth=1, visited=["verification"], idempotency_key=f"{root_id}:verified_callback",
        causation_id=root_id, correlation_id=demo.CASE_ID)
    task_id = demo.child_task_id(root_id)
    await store.runtime.add_dependency(root_id, "verified_callback", "voice_desk", task_id,
                                      env.model_dump(), meta.model_dump(exclude_none=True), max_fanout=4)
    await store.runtime.admit("voice_desk", "verification", meta.idempotency_key,
                             env.model_dump(), meta.model_dump(exclude_none=True), {})
    await store.runtime.update(task_id, status="working")
    return env, meta, task_id


async def test_get_is_read_only_and_duplicate_post_has_one_root(store, monkeypatch):
    assert (await store.snapshot())["ready"]
    assert await store.latest() is None
    request_id = str(uuid.uuid4())
    first, first_status = await store.submit(request_id)
    second, second_status = await store.submit(request_id)
    assert first_status == 202 and second_status == 200
    assert first["request"]["task_id"] == second["request"]["task_id"] == demo.root_task_id(request_id)
    assert demo.send_task.await_count == 1
    assert PHONE not in json.dumps(first) and "private-test-control" not in json.dumps(first)
    assert not second["ready"] and not second["request"]["retryable"]


async def test_concurrent_posts_share_one_active_request_and_dispatch(store):
    request_id = str(uuid.uuid4())
    results = await asyncio.gather(*(store.submit(request_id) for _ in range(5)))
    assert {result[0]["request"]["task_id"] for result in results} == {demo.root_task_id(request_id)}
    assert demo.send_task.await_count == 1
    blocked, status = await store.submit(str(uuid.uuid4()))
    assert status == 409 and blocked["request"]["request_id"] == request_id


async def test_concurrent_distinct_requests_cannot_both_own_grant(store):
    results = await asyncio.gather(store.submit(str(uuid.uuid4())), store.submit(str(uuid.uuid4())))
    assert sorted(result[1] for result in results) == [202, 409]
    assert demo.send_task.await_count == 1
    async with store.engine.connect() as conn:
        assert len((await conn.execute(select(demo.requests))).all()) == 1


async def test_uncertain_a2a_admission_retries_same_id_without_renewing_grant(store, monkeypatch):
    accept = demo.send_task.side_effect
    transport = AsyncMock(side_effect=httpx.ReadTimeout("private provider diagnostic"))
    monkeypatch.setattr(demo, "send_task", transport)
    request_id = str(uuid.uuid4())
    first, _ = await store.submit(request_id)
    original = await store.get(request_id)
    assert first["request"]["state"] == "uncertain" and first["request"]["retryable"]
    assert "private provider diagnostic" not in json.dumps(first)
    monkeypatch.setattr(demo, "send_task", AsyncMock(side_effect=accept))
    second, _ = await store.submit(request_id)
    assert second["request"]["task_id"] == first["request"]["task_id"]
    assert (await store.get(request_id))["expires_at"] == original["expires_at"]
    assert not second["request"]["retryable"]


async def test_expired_admission_does_not_resend_or_renew(store, monkeypatch):
    monkeypatch.setattr(demo, "send_task", AsyncMock(side_effect=TimeoutError))
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    async with store.engine.begin() as conn:
        await conn.execute(update(demo.requests).values(expires_at=time.time() - 1))
    result, status = await store.submit(request_id)
    assert status == 200 and result["request"]["state"] == "failed"
    assert not result["request"]["retryable"] and demo.send_task.await_count == 1


async def test_queued_root_with_expired_unused_grant_releases_slot_and_late_child_cannot_dial(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    assert (await store.runtime.get(demo.root_task_id(request_id)))["status"] == "queued"
    async with store.engine.begin() as conn:
        await conn.execute(update(demo.requests).values(expires_at=time.time() - 1))
    result = await store.snapshot(request_id)
    assert result["ready"] and result["request"]["state"] == "failed"
    assert "expired" in result["request"]["message"]
    assert (await store.submit(str(uuid.uuid4())))[1] == 202
    env, meta, task_id = await child(store, request_id)
    with pytest.raises(demo.DemoCallBlocked, match="expired"):
        await store.authorize_voice(env, meta, task_id, demo.COMPANY_ID, PHONE, PHONE)


async def test_grant_requires_exact_durable_reserved_child_and_allows_same_task_resume(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    env, meta, task_id = await child(store, request_id)
    assert await store.authorize_voice(env, meta, task_id, demo.COMPANY_ID, PHONE, PHONE)
    assert await store.authorize_voice(env, meta, task_id, demo.COMPANY_ID, PHONE, PHONE)
    assert (await store.get(request_id))["voice_task_id"] == task_id
    for changed_env, changed_meta, changed_task, company, intended, routed in [
        (env, meta, str(uuid.uuid4()), demo.COMPANY_ID, PHONE, PHONE),
        (env.model_copy(update={"case_id": "other"}), meta, task_id, demo.COMPANY_ID, PHONE, PHONE),
        (env, meta, task_id, "other-company", PHONE, PHONE),
        (env, meta, task_id, demo.COMPANY_ID, "+15005550007", PHONE),
        (env, meta, task_id, demo.COMPANY_ID, "+15005550007", "+15005550007"),
        (env, meta.model_copy(update={"from_agent": "cfo"}), task_id, demo.COMPANY_ID, PHONE, PHONE),
    ]:
        with pytest.raises(demo.DemoCallBlocked):
            await store.authorize_voice(changed_env, changed_meta, changed_task, company, intended, routed)


async def test_expired_or_released_grant_fails_closed_after_consumption(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    env, meta, task_id = await child(store, request_id)
    await store.authorize_voice(env, meta, task_id, demo.COMPANY_ID, PHONE, PHONE)
    async with store.engine.begin() as conn:
        await conn.execute(update(demo.requests).values(expires_at=time.time() - 1))
    with pytest.raises(demo.DemoCallBlocked, match="expired"):
        await store.authorize_voice(env, meta, task_id, demo.COMPANY_ID, PHONE, PHONE)


async def test_forged_model_flag_or_parent_without_reservation_cannot_grant_override(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    env = TaskEnvelope(task_type="place_call", case_id=demo.CASE_ID, subject={"hours_override": True})
    meta = DelegationMeta(case_id=demo.CASE_ID, from_agent="verification", parent_task_id=demo.root_task_id(request_id))
    with pytest.raises(demo.DemoCallBlocked, match="durable delegation"):
        await store.authorize_voice(env, meta, demo.child_task_id(meta.parent_task_id), demo.COMPANY_ID, PHONE, PHONE)
    meta.parent_task_id = str(uuid.uuid4())
    assert not await store.authorize_voice(env, meta, str(uuid.uuid4()), demo.COMPANY_ID, PHONE, PHONE)


async def test_provider_uncertainty_remains_pending_after_root_failure_and_grant_expiry(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    env, meta, task_id = await child(store, request_id)
    await store.authorize_voice(env, meta, task_id, demo.COMPANY_ID, PHONE, PHONE)
    async with store.engine.begin() as conn:
        await conn.execute(insert(voice.attempts).values(id=str(uuid.uuid4()), task_id=task_id, case_id=demo.CASE_ID,
            payload="{}", status="uncertain", provider_id="", token_hash="test", created_at=time.time(), result="", delivered="0"))
        await conn.execute(update(demo.requests).values(expires_at=time.time() - 1))
    await store.runtime.update(demo.root_task_id(request_id), status="failed")
    await store.runtime.update(task_id, status="failed")
    result = await store.snapshot(request_id)
    assert result["request"]["state"] == "uncertain" and result["request"]["call_status"] == "uncertain"
    assert not result["request"]["retryable"] and not result["ready"]
    assert (await store.submit(str(uuid.uuid4())))[1] == 409


async def test_terminal_root_does_not_release_slot_while_child_working(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    await child(store, request_id)
    await store.runtime.update(demo.root_task_id(request_id), status="failed")
    result = await store.snapshot()
    assert result["request"]["state"] == "waiting" and not result["ready"]
    assert (await store.submit(str(uuid.uuid4())))[1] == 409


async def test_new_request_allowed_after_both_tasks_finish_without_a_call(store):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    _, _, task_id = await child(store, request_id)
    await store.runtime.update(demo.root_task_id(request_id), status="failed")
    await store.runtime.update(task_id, status="failed")
    next_id = str(uuid.uuid4())
    result, status = await store.submit(next_id)
    assert status == 202 and result["request"]["request_id"] == next_id
    assert (await store.get(request_id))["active_slot"] is None


@pytest.fixture
def source(monkeypatch, tmp_path):
    path = tmp_path / "source.json"
    data = {"cases": {demo.CASE_ID: {"carrier_id": demo.COMPANY_ID}}, "companies": {
        demo.COMPANY_ID: {"phone": PHONE, "verified": True, "source": "operator",
                          "source_url": demo.TEAM_SOURCE, "timezone": "America/Los_Angeles"}}}
    path.write_text(json.dumps(data))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", PHONE)
    return path, data


async def test_verified_destination_rejects_source_or_allowlist_mismatch(source, monkeypatch):
    assert (await demo.verified_destination())["phone"] == PHONE
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15005550007")
    with pytest.raises(demo.DemoCallBlocked):
        await demo.verified_destination()


@pytest.mark.parametrize("problem", ["pending", "human_owned", "disabled", "wrong_phone", "disabled_target"])
async def test_ledger_preflight_preserves_controls(monkeypatch, problem):
    async def rows(query):
        if "FROM company" in query:
            return [{"phone": "+15005550007" if problem == "wrong_phone" else PHONE, "source_url": demo.TEAM_SOURCE}]
        if "FROM demo_target" in query:
            return [{"enabled": problem != "disabled_target"}]
        if "FROM call" in query:
            return [{"status": "queued" if problem == "pending" else "failed",
                     "started_at": datetime.now(UTC).isoformat()}] if problem in {"pending", "cooldown"} else []
        if 'FROM "case"' in query:
            return [{"status": "human_owned" if problem == "human_owned" else "open"}]
        if "FROM agent_config" in query:
            return [{"agent": "voice_desk", "enabled": problem != "disabled"}]
        raise AssertionError("Unexpected read")

    monkeypatch.setattr(demo.SpacetimeClient, "query_rows", AsyncMock(side_effect=rows))
    with pytest.raises(demo.DemoCallBlocked):
        await demo.check_ledger({"phone": PHONE, "contact_id": ""})


async def test_team_preflight_allows_back_to_back_only_after_prior_call_ended(monkeypatch):
    async def rows(query):
        if "FROM company" in query:
            return [{"phone": PHONE, "source_url": demo.TEAM_SOURCE}]
        if "FROM demo_target" in query:
            return [{"enabled": True}]
        if "FROM call" in query:
            return [{"status": "completed", "started_at": datetime.now(UTC).isoformat()}]
        if 'FROM "case"' in query:
            return [{"status": "open"}]
        return []

    monkeypatch.setattr(demo.SpacetimeClient, "query_rows", AsyncMock(side_effect=rows))
    await demo.check_ledger({"phone": PHONE, "contact_id": ""})


async def test_policy_denial_cannot_create_demo_grant(store, monkeypatch):
    monkeypatch.setattr(demo.policy, "check", lambda *_: SimpleNamespace(result="deny"))
    result, status = await store.submit(str(uuid.uuid4()))
    assert status == 403 and not result["ready"]
    assert await store.latest() is None
    demo.send_task.assert_not_awaited()


async def test_expired_demo_grant_does_not_fall_back_even_inside_normal_hours(store, source, monkeypatch):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    env, meta, task_id = await child(store, request_id)
    async with store.engine.begin() as conn:
        await conn.execute(update(demo.requests).values(expires_at=time.time() - 1))
    monkeypatch.setattr(voice, "check_call_window", lambda _: None)
    mutate = AsyncMock()
    monkeypatch.setattr(voice.SpacetimeClient, "mutate", mutate)
    with pytest.raises(demo.DemoCallBlocked, match="expired"):
        await voice.start_call(env, meta, task_id)
    mutate.assert_not_awaited()


async def test_only_valid_demo_grant_bypasses_hours_and_no_model_flag_does(store, source, monkeypatch):
    request_id = str(uuid.uuid4())
    await store.submit(request_id)
    env, meta, task_id = await child(store, request_id)
    for key, value in {"CARTESIA_API_KEY": "test-key", "CARTESIA_AGENT_ID_VERIFICATION": "agent_test",
                       "CARTESIA_FROM_NUMBER_ID": "number_test"}.items():
        monkeypatch.setenv(key, value)
    hours = lambda _: (_ for _ in ()).throw(voice.VoiceError("outside hours"))
    monkeypatch.setattr(voice, "check_call_window", hours)
    mutation = AsyncMock()
    monkeypatch.setattr(voice.SpacetimeClient, "mutate", mutation)
    original = httpx.AsyncClient
    posted = []

    def handler(request):
        posted.append(request)
        assert json.loads(request.content)["outbound_calls"][0]["to_number"] == PHONE
        return httpx.Response(200, json={"calls": [{"agent_call_id": "ac_demo"}]})

    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    attempt, result = await voice.start_call(env, meta, task_id)
    assert attempt and result is None and len(posted) == 1 and mutation.await_count == 1
    assert mutation.await_args.args[1]["team_demo_authorized"] is True
    assert (await voice.start_call(env, meta, task_id))[0] == attempt and len(posted) == 1
    forged = env.model_copy(update={"subject": {**env.subject, "hours_override": True}})
    other = meta.model_copy(update={"parent_task_id": str(uuid.uuid4())})
    with pytest.raises(voice.VoiceError, match="outside hours"):
        await voice.start_call(forged, other, str(uuid.uuid4()))
    assert len(posted) == 1


def test_gateway_auth_strict_input_and_disabled_control(monkeypatch, tmp_path):
    from gateway.app import app
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("A2A_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_CONTROL_TOKEN", "test-control")
    monkeypatch.setenv("DEMO_PHONE_ENABLED", "0")
    headers = {"Authorization": "Bearer test-control"}
    with TestClient(app) as client:
        assert client.get("/demo/team-call").status_code == 401
        assert client.get("/demo/team-call", headers={"Authorization": "test-control"}).status_code == 401
        result = client.get("/demo/team-call", headers=headers)
        assert result.status_code == 200 and not result.json()["enabled"] and not result.json()["ready"]
        for body in [{}, {"request_id": "invalid"}, {"request_id": str(uuid.uuid4()), "phone": PHONE},
                     {"request_id": str(uuid.uuid4()), "hours_override": True}, {"request_id": 12}]:
            assert client.post("/demo/team-call", headers=headers, json=body).status_code == 400
        result = client.post("/demo/team-call", headers=headers, json={"request_id": str(uuid.uuid4())})
        assert result.status_code == 503 and not result.json()["enabled"]
        assert client.get("/demo/team-call?request_id=invalid", headers=headers).status_code == 400
