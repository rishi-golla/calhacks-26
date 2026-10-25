"""Common domain helpers used by role-specific FreightAgent handlers."""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from common import policy
from common.envelope import ResultArtifact, TaskEnvelope
from common.source_repository import SourceUnavailable, fixture_mode
from common.spacetime_client import SpacetimeClient


def money(value: Any, *, signed: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SourceUnavailable("a source monetary amount is required")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SourceUnavailable("source monetary amount is invalid") from exc
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")) or (amount < 0 and not signed):
        raise SourceUnavailable("source monetary amount must be finite and exact cents")
    return amount


def number(value: Decimal) -> float:
    return float(value)


def journal_lines(lines: Any) -> list[dict]:
    """Journal lines as dicts. The SQL API returns stored JournalLine rows positionally
    (``[account, debit, credit]``); reducers and fixtures use objects. Anything else is not a line."""
    if not isinstance(lines, list):
        raise SourceUnavailable("journal lines must be a list")
    out = []
    for line in lines:
        if isinstance(line, list) and len(line) == 3:
            line = dict(zip(("account", "debit", "credit"), line, strict=True))
        if not isinstance(line, dict):
            raise SourceUnavailable("journal line must be an object")
        out.append(line)
    return out


def matched_deposit_total(source: dict) -> Decimal:
    """Cash collected on this shipper invoice from its own uniquely identified deposits."""
    invoice_id = source.get("shipper_invoice_id")
    deposits = [item for item in source.get("deposits", []) if invoice_id
                and item.get("invoice_id") == invoice_id and item.get("shipper_id") == source.get("shipper_id")]
    ids = [item.get("id") for item in deposits]
    if any(not identity for identity in ids) or len(set(ids)) != len(ids):
        raise SourceUnavailable("duplicate or missing source deposit ID")
    return sum((money(item["amount"]) for item in deposits), Decimal(0))


def identifier(task_id: str, operation: str) -> str:
    return hashlib.sha256(f"{task_id}:{operation}".encode()).hexdigest()[:32]


def evidence(source: dict, env: TaskEnvelope) -> list[str]:
    refs = source.get("evidence_refs", env.evidence_refs if fixture_mode() else [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        raise SourceUnavailable("source evidence references must be strings")
    return list(dict.fromkeys(refs))


async def assess(agent: str, env: TaskEnvelope, findings: dict) -> dict:
    from common.providers import infer
    result = await infer(agent, env, findings, purpose="assessment")
    return result


async def finish(agent, env: TaskEnvelope, task_id: str, *, outcome: str, findings: dict,
                 confidence: float = 0.8, policy_ids: list[str] | None = None,
                 evidence_refs: list[str] | None = None, recommended_next: list[str] | None = None) -> ResultArtifact:
    assessment_env = env.model_copy(update={"evidence_refs": list(evidence_refs or [])})
    async def run_assessment():
        return await assess(agent.name, assessment_env, findings)
    inference = await agent.step(task_id, "final_assessment", run_assessment)
    provenance = {key: inference[key] for key in ("instr_version", "memory_ids", "usage", "latency_ms")
                  if key in inference}
    return ResultArtifact(
        outcome=outcome, confidence=confidence, findings={**findings, "simulated": fixture_mode(),
                                                       "assessment": inference.get("assessment", {}),
                                                       "inference": provenance},
        evidence_refs=evidence_refs or [], policy_ids=policy_ids or [], model_used=inference.get("model_used"),
        recommended_next=recommended_next or [],
    )


async def mutate(agent, env: TaskEnvelope, task_id: str, operation: str, payload: dict) -> dict:
    """Journal exactly the intended payload; never report disabled live writes as successful."""
    semantic = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    key = f"{operation}:{hashlib.sha256(semantic.encode()).hexdigest()[:24]}"
    document = {**payload, "task_id": task_id, "case_id": env.case_id,
                "write_id": identifier(task_id, key)}

    async def apply():
        decision = policy.check(operation, agent.name, document)
        # The strict client validates stored approvals. A caller-supplied ID
        # is not proof of authorization, including in fixture execution.
        if decision.result != "allow" and (decision.result == "deny" or fixture_mode()):
            return {"executed": False, "decision": decision.result, "policy_ids": decision.policy_ids,
                    "reason": decision.reason, "simulated": fixture_mode()}
        if os.getenv("SPACETIME_DISABLED", "1") == "1":
            if not fixture_mode():
                raise SourceUnavailable("live financial writes require SpacetimeDB")
            return {"executed": False, "simulated": True, "decision": "allow", "action": operation,
                    "proposed_payload": document}
        receipt = await SpacetimeClient(actor=agent.name).mutate(operation, document)
        return {"executed": True, "simulated": False, "write_id": receipt.write_id,
                "action": operation, "status_code": receipt.status_code}

    return await agent.step(task_id, key, apply)


async def rows(agent: str, table: str, field: str, value: str) -> list[dict]:
    allowed = {"approval", "hold", "payment_proposal", "journal_request", "ledger_entry", "precedent"}
    fields = {"id", "case_id", "invoice_id", "carrier_id"}
    if table not in allowed or field not in fields:
        raise ValueError("unsupported source query")
    escaped = value.replace("'", "''")
    return await SpacetimeClient(actor=agent).query_rows(f"SELECT * FROM {table} WHERE {field} = '{escaped}'")


def billing_journal_id(shipper_invoice_id: str) -> str:
    """One billing journal per shipper invoice, however many reconciliations run."""
    return identifier(f"{shipper_invoice_id}", "revenue_journal_request")


def payable_journal_id(invoice_id: str) -> str:
    """One payable journal per carrier invoice, however many payment proposals run."""
    return identifier(f"{invoice_id}", "payable_journal_request")


def billing_lines(amount: Decimal) -> list[dict]:
    """Customer billing: the shipper owes the invoice and the brokerage earns it."""
    return [{"account": "accounts_receivable", "debit": number(amount), "credit": 0},
            {"account": "freight_revenue", "debit": 0, "credit": number(amount)}]


def payable_lines(amount: Decimal) -> list[dict]:
    """Carrier settlement: the carrier's cost is incurred and owed. Nothing is paid, so AP stays open."""
    return [{"account": "freight_expense", "debit": number(amount), "credit": 0},
            {"account": "accounts_payable", "debit": 0, "credit": number(amount)}]


async def journal_requests(agent: str, env: TaskEnvelope, source: dict, *request_ids: str) -> list[dict]:
    """This case's journal requests, plus any named request wherever it is stored.

    A deterministic request ID can only be inserted once, so a lookup by ID also finds one another case
    already holds; ``request_journal`` then refuses it instead of the reducer failing on the insert.
    """
    from common.source_repository import source_fixtures_only
    if source_fixtures_only():
        return list(source.get("journal_requests", []))
    found = list(await rows(agent, "journal_request", "case_id", env.case_id))
    for request_id in request_ids:
        if not any(item.get("id") == request_id for item in found):
            found.extend(await rows(agent, "journal_request", "id", request_id))
    return found


async def request_journal(agent, env: TaskEnvelope, task_id: str, existing: list[dict], request_id: str, *,
                          period: str, lines: list[dict], memo: str, label: str = "journal") -> dict:
    """Request one journal entry at most once.

    Returns ``{"receipt", "posted_entry_id", "pending"}``. A posted request reports its entry and needs
    nothing more; a stored, unposted one is pending without a second request (the reducer's insert fails
    on a repeated ID); a new one is requested and is pending only once actually recorded, because a
    fixture write is only a proposal until it exists in source data.
    """
    stored = next((item for item in existing if item.get("id") == request_id), None)
    if stored and stored.get("case_id") != env.case_id:
        raise SourceUnavailable(f"{label} journal belongs to a different case")
    if stored and stored.get("posted_entry_id"):
        return {"receipt": None, "posted_entry_id": stored["posted_entry_id"], "pending": False}
    if stored:
        return {"receipt": {"executed": False, "existing": True, "request_id": request_id,
                            "simulated": fixture_mode()}, "posted_entry_id": None, "pending": True}
    receipt = await mutate(agent, env, task_id, "request_journal_entry", {
        "id": request_id, "period": period, "lines": lines, "memo": memo})
    return {"receipt": receipt, "posted_entry_id": None, "pending": bool(receipt.get("executed"))}


def child_envelope(env: TaskEnvelope, skill: str, **extra) -> TaskEnvelope:
    # Extra fields describe a requested operation; destination agents re-read
    # the source and reducer state before executing it.
    return env.model_copy(update={"task_type": skill, "subject": {**env.subject, **extra}})
