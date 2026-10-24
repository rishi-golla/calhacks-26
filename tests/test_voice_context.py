"""Voice conversations receive bounded, source-grounded load facts, never private source dumps."""
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime_store import RuntimeStore
from common.source_repository import SourceRepository
from common.voice_context import MAX_CONTEXT_BYTES, build_voice_context, verification_questions

FIXTURE = Path(__file__).resolve().parents[1] / "sim/fixtures/team_call_source.json"
CASE = "SYN-CARTESIA-TEAM-CALL"


@pytest.fixture
def data(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    return json.loads(FIXTURE.read_text())


async def test_demo_context_has_realistic_source_facts_and_correct_detention_math(data):
    source = data["cases"][CASE]
    context = await build_voice_context(source, SourceRepository(data))
    assert context["synthetic"] is True
    assert context["load_reference"] == "GG-1042"
    assert context["load_id"] == "SYN-CARTESIA-TEAM-CALL-LOAD"
    assert context["invoice_reference"] == "SVT-1042"
    assert context["bill_of_lading_reference"] == "BOL-GG-1042"
    assert context["parties"]["broker"]["name"] == "Golden Gate Freight"
    assert context["parties"]["carrier"]["name"] == "Sierra Valley Transport"
    assert context["route"]["origin"]["city"] == "Oakland"
    assert context["route"]["destination"]["city"] == "Sacramento"
    assert context["stops"][1]["appointment_at"] == "2026-10-23T09:00:00-07:00"
    assert context["stops"][1]["timezone"] == "America/Los_Angeles"
    assert context["cargo"]["pallets"] == 18 and context["cargo"]["weight_lb"] == 36000
    assert context["invoice"]["amount"] == 1225 and context["invoice"]["linehaul_amount"] == 975
    assert context["invoice"]["claimed_accessorial_amount"] == 250
    assert context["invoice"]["paid_amount"] == 0 and context["invoice"]["payment_status"] == "unpaid"
    assert context["claim"]["billable_minutes"] == 75
    assert context["claim"]["supported_amount"] == 125 and context["claim"]["difference_from_claim"] == 125
    assert context["claim"]["calculation_status"] == "provisional_source_calculation"
    assert context["claim"]["payment_authorized"] is False
    packet = {document["kind"]: document for document in context["documents"]}
    assert packet["pod"]["present"] and packet["pod"]["signed"]
    assert packet["rate_confirmation"]["present"] and packet["rate_confirmation"]["signed"]
    assert packet["detention_log"]["status"] == "unsigned"
    assert packet["detention_receipt"]["status"] == "missing"
    assert context["contacts"]["email"] is None and context["contacts"]["submission_channels"] == []


async def test_context_excludes_source_secrets_destinations_and_unrelated_documents(data):
    source = data["cases"][CASE]
    source.update(api_key="sk-privatevalue12345", bank_account="12345678901234", phone="+14155550123",
                  email="private@carrier.example", raw_document={"password": "private-document-secret"})
    source["contacts"].update(email="private@carrier.example", phone="+14155550123")
    source["parties"]["broker"]["name"] = "Broker private@carrier.example +14155550123"
    source["documents"][0]["raw_text"] = "private-document-secret"
    source["documents"].extend([
        {"kind": "bank_statement", "ref": "source:private-bank"},
        {"kind": "invoice", "ref": "source:other-load", "load_id": "OTHER-LOAD"},
    ])
    source["evidence_refs"].append("https://records.example/doc?token=private-secret")
    encoded = json.dumps(await build_voice_context(source, SourceRepository(data)))
    for secret in ("sk-privatevalue12345", "12345678901234", "+14155550123", "private@carrier.example",
                   "private-document-secret", "source:private-bank", "source:other-load", "private-secret"):
        assert secret not in encoded


@pytest.mark.parametrize(("arrival", "release", "expected"), [
    ("2026-10-23T08:45:00-07:00", "2026-10-23T12:15:00-07:00", 125),
    ("2026-10-23T09:30:00-07:00", "2026-10-23T12:15:00-07:00", 75),
    ("2026-10-23T08:45:00-07:00", "2026-10-23T10:00:00-07:00", 0),
    ("2026-10-23T08:45:00-07:00", "2026-10-23T07:00:00-07:00", None),
    ("2026-10-23T08:45:00", "2026-10-23T12:15:00-07:00", None),
])
async def test_detention_uses_recorded_appointment_terms_not_a_fixed_demo_answer(data, arrival, release, expected):
    source = data["cases"][CASE]
    source["detention"].update(arrival_at=arrival, released_at=release)
    assert (await build_voice_context(source))["claim"]["supported_amount"] == expected


async def test_context_does_not_invent_missing_amounts_dates_or_party_names():
    context = await build_voice_context({"case_id": "unknown", "load_id": "LOAD-2"})
    assert context["invoice"]["amount"] is None and context["invoice"]["paid_amount"] is None
    assert context["invoice"]["payment_status"] == "unknown"
    assert context["invoice"]["claimed_accessorial_amount"] is None
    assert context["claim"]["supported_amount"] is None
    assert context["parties"]["carrier"]["name"] is None
    assert context["load_reference"] == "LOAD-2"
    assert context["stops"] == context["documents"] == []


async def test_bounded_context_handles_oversized_and_malformed_source_fields(data):
    source = data["cases"][CASE]
    source["scenario"]["summary"] = "x" * 100_000
    source["documents"] *= 100
    source["evidence_refs"] *= 100
    source["stops"] *= 100
    source["line_items"] *= 100
    source["detention"]["rate_per_hour"] = True
    source["cargo"]["weight_lb"] = float("inf")
    context = await build_voice_context(source)
    assert len(json.dumps(context, allow_nan=False).encode()) <= MAX_CONTEXT_BYTES
    assert len(context["documents"]) <= 8 and len(context["stops"]) <= 4
    assert len(context["invoice"]["line_items"]) <= 12
    assert context["invoice"]["line_items_complete"] is False
    assert context["invoice"]["claimed_accessorial_amount"] is None
    assert context["claim"]["supported_amount"] is None and context["cargo"]["weight_lb"] is None


async def test_verification_questions_reference_known_facts_and_actual_document_status(data):
    context = await build_voice_context(data["cases"][CASE])
    questions = verification_questions(context, "verify_claim")
    text = " ".join(questions)
    assert "GG-1042" in text and "SVT-1042" in text and "$250.00" in text
    assert "75 billable minutes" in text and "$125.00" in text
    assert "missing or unsigned" in text and "does not approve" in text
    context["documents"].append({"kind": "detention_receipt", "present": True, "signed": True})
    assert "missing or unsigned" not in " ".join(verification_questions(context, "verify_claim"))


async def test_verification_delegates_source_context_instead_of_incoming_claims(data, monkeypatch):
    from agents.verification import main
    source = data["cases"][CASE]
    data["companies"]["DEMO-TEAM"]["phone"] = "+15005550006"
    repository = SourceRepository(copy.deepcopy(data))
    monkeypatch.setattr(main, "SourceRepository", lambda: repository)
    monkeypatch.setattr(main, "finish", AsyncMock(return_value=ResultArtifact(outcome="unverified")))
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = main.VerificationAgent(store=store)
    agent.delegate = AsyncMock(return_value=[])
    env = TaskEnvelope(task_type="verify_claim", case_id=CASE,
                       subject={"invoice_amount": 1, "facts": {"load_reference": "FORGED"}})
    try:
        await agent.run_workflow(env, DelegationMeta(case_id=CASE, from_agent="router"), "test-task", None)
        child = agent.delegate.await_args.args[1]
        assert child.subject["facts"]["load_reference"] == source["load_reference"]
        assert child.subject["facts"]["invoice"]["amount"] == 1225
        assert "+15005550006" not in json.dumps(child.subject["facts"])
        assert agent.delegate.await_args.kwargs["invocation_key"] == "verified_callback"
    finally:
        await store.close()
