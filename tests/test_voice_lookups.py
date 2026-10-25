"""Hosted voice tools stay bound to one active call and expose read-only facts."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from common.envelope import TaskEnvelope
from common.policy import Decision
from common.runtime import projection_target
from common.runtime_store import RuntimeStore
from common.source_repository import SourceUnavailable
from common.voice import CallStore
from gateway import voice_tools
from gateway.voice_tools_public import app

CASE = "SYN-CARTESIA-TEAM-CALL"
ATTEMPT = "lookup-attempt"
TOKEN = "synthetic-private-call-capability"
BODY = {"attempt_id": ATTEMPT, "case_id": CASE}
HEADERS = {"x-call-token": TOKEN}
SOURCE = Path(__file__).resolve().parents[1] / "sim/fixtures/team_call_source.json"
REAL_AUDIT = voice_tools.audit_lookup


@pytest.fixture
async def rig(monkeypatch, tmp_path):
    dataset = json.loads(SOURCE.read_text())
    source = dataset["cases"][CASE]
    source.update(bank_account="private-bank-value", api_key="private-api-value")
    source["parties"]["carrier"].update(phone="+14155550123", email="private@example.test")
    source["contacts"]["carrier_dispatcher"].update(phone="+14155550123", email="private@example.test")
    path = tmp_path / "source.json"
    path.write_text(json.dumps(dataset))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    store = CallStore(f"sqlite+aiosqlite:///{tmp_path / 'calls.sqlite'}")
    await store.initialize()
    await store.prepare(ATTEMPT, TaskEnvelope(task_type="place_call", case_id=CASE), "voice-task", {
        "purpose": "verification", "phone": "+14155550123", "api_key": "private-api-value",
    }, TOKEN)
    await store.set(ATTEMPT, status="ringing")
    monkeypatch.setattr(app.state, "calls", store, raising=False)
    invoice = {"id": source["invoice_id"], "case_id": CASE, "amount": 1225, "contract_amount": 975,
               "paid_amount": 0, "status": "unpaid", "issued_at": source["issued_at"],
               "bank_account": "private-bank-value", "company_id": "private-company-value",
               "phone": "+14155550123", "email": "private@example.test", "token": TOKEN}
    proposals = [
        {"invoice_id": source["invoice_id"], "amount": 975, "status": "proposed",
         "bank_account": "private-bank-value", "token": TOKEN},
        {"invoice_id": "another-invoice", "amount": 8888, "status": "approved"},
    ]

    async def query(sql):
        assert sql.startswith("SELECT ") and f"WHERE case_id = '{CASE}'" in sql
        return proposals if "FROM payment_proposal " in sql else [invoice]

    db = SimpleNamespace(disabled=False, query_rows=AsyncMock(side_effect=query), mutate=AsyncMock())
    actors = []

    def client(*, actor):
        actors.append(actor)
        return db

    monkeypatch.setattr(voice_tools, "SpacetimeClient", client)
    audit = AsyncMock()
    monkeypatch.setattr(voice_tools, "audit_lookup", audit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice.test") as client:
        yield SimpleNamespace(client=client, store=store, dataset=dataset, source=source, path=path,
                              db=db, audit=audit, actors=actors, invoice=invoice, proposals=proposals)
    await store.close()


@pytest.mark.parametrize("name", sorted(voice_tools.READ_ONLY_TOOLS))
async def test_all_five_tools_return_the_current_case_source_without_private_directory_data(rig, name):
    response = await rig.client.post(f"/voice/tools/{name}", headers=HEADERS, json=BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == name and body["synthetic"] is True and body["as_of"]
    result = body["result"]
    if name in {"get_case_summary", "get_load_facts"}:
        assert result["load_reference"] == "GG-1042"
        assert result["bill_of_lading_reference"] == "BOL-GG-1042"
        assert result["route"]["origin"]["city"] == "Oakland"
        assert result["route"]["destination"]["city"] == "Sacramento"
        assert result["parties"]["carrier"]["name"] == "Sierra Valley Transport"
        assert result["cargo"]["pallets"] == 18 and result["cargo"]["weight_lb"] == 36000
        delivery = next(stop for stop in result["stops"] if stop["kind"] == "delivery")
        assert delivery["appointment_at"] == "2026-10-23T09:00:00-07:00"
    if name in {"get_case_summary", "get_document_status"}:
        documents = {item["kind"]: item for item in result["documents"]}
        assert documents["pod"]["signed"] and documents["rate_confirmation"]["signed"]
        assert documents["detention_log"]["status"] == "unsigned"
        assert documents["detention_receipt"]["status"] == "missing"
        assert result["contacts"]["email"] is None and result["contacts"]["submission_channels"] == []
        assert "No email, SMS, or document-upload intake" in result["contacts"]["note"]
    if name == "get_case_summary":
        assert result["claim"]["billable_minutes"] == 75
        assert result["claim"]["supported_amount"] == 125
        assert result["claim"]["difference_from_claim"] == 125
        assert result["claim"]["calculation_status"] == "provisional_source_calculation"
        assert result["claim"]["payment_authorized"] is False
    if name in {"get_invoice_status", "get_payment_status"}:
        assert result["invoice_reference"] == "SVT-1042"
        assert result["source_record"]["amount"] == 1225
        assert result["source_record"]["linehaul_amount"] == 975
        assert result["source_record"]["claimed_accessorial_amount"] == 250
        assert result["ledger_available"] and result["current_payment_status"] == "unpaid"
        assert set(result["ledger_invoice"]) == {"amount", "contract_amount", "paid_amount", "status", "issued_at"}
        if name == "get_payment_status":
            assert result["payment_proposals"] == [{"amount": 975, "status": "proposed"}]
        else:
            assert "payment_proposals" not in result
    serialized = json.dumps(body)
    for secret in (TOKEN, "private-bank-value", "private-api-value", "private-company-value",
                   "private@example.test", "+14155550123"):
        assert secret not in serialized
    rig.audit.assert_awaited_once()
    args = rig.audit.await_args.args
    assert args[1]["id"] == ATTEMPT and args[1]["case_id"] == CASE
    assert args[2] == name and args[3] == body
    assert set(rig.actors) <= {"voice_desk"}
    rig.db.mutate.assert_not_awaited()


async def test_load_facts_are_rebuilt_from_updated_source_instead_of_a_fixed_demo_script(rig):
    rig.source["load_reference"] = "GG-SECOND"
    rig.source["route"]["destination"]["city"] = "Fresno"
    rig.source["cargo"]["pallets"] = 7
    rig.path.write_text(json.dumps(rig.dataset))
    body = (await rig.client.post("/voice/tools/get_load_facts", headers=HEADERS, json=BODY)).json()
    assert body["result"]["load_reference"] == "GG-SECOND"
    assert body["result"]["route"]["destination"]["city"] == "Fresno"
    assert body["result"]["cargo"]["pallets"] == 7


@pytest.mark.parametrize("state", ["dialing", "ringing", "uncertain"])
async def test_only_active_attempt_with_its_own_capability_can_read(rig, state):
    await rig.store.set(ATTEMPT, status=state)
    assert (await rig.client.post("/voice/tools/get_load_facts", headers=HEADERS, json=BODY)).status_code == 200


@pytest.mark.parametrize("change", [
    {"status": "prepared"}, {"status": "completed"}, {"status": "failed"}, {"status": "no_answer"},
    {"status": "canceled"}, {"status": "rejected"}, {"created_at": 0}, {"result": '{"reached":true}'},
])
async def test_terminal_unstarted_expired_or_finalized_attempts_are_rejected_before_source_lookup(rig, change, monkeypatch):
    await rig.store.set(ATTEMPT, **change)
    source = AsyncMock(side_effect=AssertionError("unauthorized request must not reach source"))
    monkeypatch.setattr(voice_tools.SourceRepository, "load", source)
    response = await rig.client.post("/voice/tools/get_case_summary", headers=HEADERS, json=BODY)
    assert response.status_code == 401
    source.assert_not_awaited()
    rig.audit.assert_not_awaited()


async def test_capability_is_bound_to_attempt_and_case_and_cannot_be_forged(rig):
    await rig.store.prepare("other-attempt", TaskEnvelope(task_type="place_call", case_id="other-case"),
                            "other-task", {"purpose": "verification"}, "other-capability")
    await rig.store.set("other-attempt", status="ringing")
    assert (await rig.client.post("/voice/tools/get_case_summary", json=BODY)).status_code == 401
    assert (await rig.client.post("/voice/tools/get_case_summary", headers={"x-call-token": "forged"},
                                  json=BODY)).status_code == 401
    assert (await rig.client.post("/voice/tools/get_case_summary", headers=HEADERS,
                                  json={**BODY, "attempt_id": "other-attempt"})).status_code == 401
    assert (await rig.client.post("/voice/tools/get_case_summary", headers=HEADERS,
                                  json={**BODY, "attempt_id": "missing"})).status_code == 401
    assert (await rig.client.post("/voice/tools/get_case_summary", headers=HEADERS,
                                  json={**BODY, "case_id": "other-case"})).status_code == 403
    rig.audit.assert_not_awaited()
    rig.db.query_rows.assert_not_awaited()


@pytest.mark.parametrize("body", [
    [], None, "string", {}, {"attempt_id": ATTEMPT}, {**BODY, "amount": 1},
    {**BODY, "phone": "+14155550123"}, {**BODY, "case_id": 12},
    {**BODY, "attempt_id": ""}, {**BODY, "case_id": "x" * 201},
])
async def test_model_cannot_supply_extra_arguments_or_malformed_identifiers(rig, body):
    response = await rig.client.post("/voice/tools/get_payment_status", headers=HEADERS,
                                     content=json.dumps(body))
    assert response.status_code == 400
    rig.audit.assert_not_awaited()
    rig.db.query_rows.assert_not_awaited()


async def test_bad_json_and_oversized_requests_are_rejected_without_data_access(rig):
    for content, expected in [(b"not-json", 400), (b"\xff", 400), (b"x" * 2049, 413)]:
        result = await rig.client.post("/voice/tools/get_load_facts", headers=HEADERS, content=content)
        assert result.status_code == expected
    rig.audit.assert_not_awaited()


async def test_public_app_exposes_no_operator_controls_mutations_or_documentation(rig):
    assert voice_tools.READ_ONLY_TOOLS == {
        "get_load_facts", "get_payment_status", "get_invoice_status", "get_case_summary", "get_document_status",
    }
    assert (await rig.client.post("/voice/tools/get_case_summary")).status_code == 401
    for path in ("/demo", "/demo/team-call", "/demo/phone", "/voice/webhook", "/docs", "/redoc", "/openapi.json"):
        for method in ("GET", "POST"):
            assert (await rig.client.request(method, path, headers=HEADERS, json=BODY)).status_code == 404
    for name in ("approve_payment", "place_call", "release_hold", "mutate", "not-a-tool"):
        assert (await rig.client.post(f"/voice/tools/{name}", headers=HEADERS, json=BODY)).status_code == 404
    assert (await rig.client.get("/voice/tools/get_load_facts", headers=HEADERS)).status_code == 405
    assert (await rig.client.get("/healthz")).json() == {"ok": True, "service": "voice-lookups"}
    rig.audit.assert_not_awaited()


async def test_missing_source_is_explicitly_unavailable_instead_of_inventing_case_facts(rig, monkeypatch):
    monkeypatch.setattr(voice_tools.SourceRepository, "load", AsyncMock(side_effect=SourceUnavailable("private path")))
    result = await rig.client.post("/voice/tools/get_case_summary", headers=HEADERS, json=BODY)
    assert result.status_code == 503 and "do not invent" in result.json()["detail"]
    assert "private path" not in result.text
    rig.audit.assert_not_awaited()


@pytest.mark.parametrize("failure", ["disabled", "missing_invoice", "invoice_query", "proposal_query"])
async def test_unavailable_or_missing_live_ledger_is_unknown_even_when_source_says_unpaid(rig, failure):
    if failure == "disabled":
        rig.db.disabled = True
    elif failure == "missing_invoice":
        rig.invoice["id"] = "different-invoice"
    elif failure == "invoice_query":
        rig.db.query_rows.side_effect = RuntimeError("private database failure")
    else:
        rig.db.query_rows.side_effect = [[rig.invoice], RuntimeError("private proposal failure")]
    response = await rig.client.post("/voice/tools/get_payment_status", headers=HEADERS, json=BODY)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["source_record"]["payment_status"] == "unpaid"
    assert result["current_payment_status"] == "unknown"
    assert "ledger_invoice" not in result
    if failure != "missing_invoice":
        assert result["ledger_available"] is False and "payment_proposals" not in result
    if failure.endswith("query"):
        assert "Do not assert current payment status" in result["note"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("amount,paid,expected", [
    (1225, 0, "unpaid"), (1225, 225, "partially_paid"), (1225, 1225, "paid"),
    (1225, 1500, "paid"), (0, 0, "unknown"), (1225, -1, "unknown"),
    ("NaN", 0, "unknown"), (1225, "Infinity", "unknown"), (1225, None, "unknown"),
])
async def test_current_payment_status_uses_valid_live_ledger_values_not_source_or_proposals(rig, amount, paid, expected):
    rig.invoice.update(amount=amount, paid_amount=paid, status="arbitrary-provider-status")
    result = (await rig.client.post("/voice/tools/get_payment_status", headers=HEADERS, json=BODY)).json()["result"]
    assert result["current_payment_status"] == expected
    assert result["source_record"]["payment_status"] == "unpaid"


async def test_payment_proposals_are_bounded_and_never_include_other_invoice_rows(rig):
    rig.proposals[:] = [{"invoice_id": rig.source["invoice_id"], "amount": value, "status": "proposed",
                        "bank_account": "private-bank-value"} for value in range(30)]
    result = (await rig.client.post("/voice/tools/get_payment_status", headers=HEADERS, json=BODY)).json()["result"]
    assert len(result["payment_proposals"]) == 20
    assert all(set(row) == {"amount", "status"} for row in result["payment_proposals"])
    assert result["current_payment_status"] == "unpaid"


@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_public_lookup_audit_uses_the_durable_outbox_and_only_acknowledges_success(rig, monkeypatch, delivery_fails):
    monkeypatch.setattr(voice_tools, "audit_lookup", REAL_AUDIT)
    if delivery_fails:
        rig.db.mutate.side_effect = TimeoutError("private transport detail")
    response = await rig.client.post("/voice/tools/get_case_summary", headers=HEADERS, json=BODY)
    assert response.status_code == 200
    rig.db.mutate.assert_awaited_once()
    operation, payload = rig.db.mutate.await_args.args
    assert operation == "append_audit" and payload["kind"] == "tool_call"
    assert str(uuid.UUID(payload["write_id"])) == payload["write_id"]
    assert len(payload["write_id"]) <= 36  # PostgreSQL enforces this; SQLite does not.
    assert payload["task_id"] == "voice-task" and payload["case_id"] == CASE
    assert payload["evidence_refs"] == [f"call://{ATTEMPT}"]
    event = json.loads(payload["summary"])
    assert event["tool_name"] == "get_case_summary" and event["status"] == "completed"
    assert event["arguments"] == {"case_id": CASE} and event["duration_ms"] >= 0
    assert event["model"] == "cartesia/hosted-agent"
    assert TOKEN not in payload["summary"] and "private" not in payload["summary"]
    pending = await RuntimeStore(engine=rig.store.engine).pending_projections("voice_desk")
    assert len(pending) == int(delivery_fails)
    if pending:
        stored = json.loads(pending[0]["payload"])
        # Queued for the database it was written to, so a re-pointed service never delivers it elsewhere.
        assert stored == {**payload, "_operation": "append_audit"}
        assert pending[0]["target"] == projection_target()


async def test_audit_redacts_nested_contact_secrets_and_obeys_policy_before_writing(rig, monkeypatch):
    request = Request({"type": "http", "app": app})
    row = await rig.store.get(ATTEMPT)
    result = {"phone": "+14155550123", "nested": {"api_key": "private-api-value", "bank_account": "private-bank-value"},
              "message": "Contact private@example.test or +14155550123", "safe": "GG-1042"}
    await REAL_AUDIT(request, row, "get_case_summary", result, time.monotonic())
    payload = rig.db.mutate.await_args.args[1]
    assert "private" not in payload["summary"] and "55550123" not in payload["summary"]
    assert json.loads(payload["summary"])["result"]["safe"] == "GG-1042"
    assert "[redacted]" in payload["summary"]
    rig.db.mutate.reset_mock()
    monkeypatch.setattr(voice_tools, "check", lambda *args: Decision("deny"))
    await REAL_AUDIT(request, row, "get_case_summary", result, time.monotonic())
    rig.db.mutate.assert_not_awaited()
    assert await RuntimeStore(engine=rig.store.engine).pending_projections("voice_desk") == []
