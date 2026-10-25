"""Task-scoped model tools; authority stays in domain code and reducer policy.

Models choose reads, checks and delegated specialist work. The final action executes
one assigned skill through the same reviewed workflow used by fixture tests. It
cannot substitute an amount, invoice, payee, approval, phone number or case.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from common.agent_support import child_envelope, evidence, money, number, rows
from common.config import AGENTS, CALL_PURPOSES, SKILL_OWNER
from common.envelope import DelegationMeta, TaskEnvelope
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode, source_fixtures_only


@dataclass(frozen=True)
class ToolContext:
    agent: Any
    env: TaskEnvelope
    meta: DelegationMeta
    task_id: str
    tool_call_id: str = ""


EMPTY_PARAMETERS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
ToolHandler = Callable[[ToolContext, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    # Catalog metadata is explanatory, never an authorization override.
    effects: tuple[str, ...] = field(default_factory=tuple)

    def validate(self, arguments: Any) -> dict:
        """Reject extras and type coercion before any handler can run."""
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        properties = self.parameters["properties"]
        if set(arguments) != set(self.parameters["required"]) or set(arguments) - set(properties):
            raise ValueError("tool arguments do not match the required schema")
        for key, value in arguments.items():
            schema = properties[key]
            if schema.get("type") == "string" and not isinstance(value, str):
                raise ValueError("tool argument must be a string")
            if "enum" in schema and value not in schema["enum"]:
                raise ValueError("tool argument is not an allowed selection")
        return dict(arguments)


async def _source(ctx: ToolContext) -> dict:
    return await SourceRepository().load(ctx.env)


def _with_refs(source: dict, ctx: ToolContext, result: dict) -> dict:
    return {**result, "evidence_refs": evidence(source, ctx.env),
            "source_simulated": source.get("source_simulated", fixture_mode())}


async def read_load_record(ctx: ToolContext, arguments: dict) -> dict:
    """Return relevant ingested facts, excluding raw bank/payment credentials."""
    source = await _source(ctx)
    fields = ("case_id", "load_id", "carrier_id", "shipper_id", "invoice_id", "shipper_invoice_id",
              "invoice_amount", "contract_rate", "shipper_invoice_amount", "paid_amount",
              "shipper_paid_amount", "period", "lane", "issued_at", "overdue", "claim_type",
              "dispute", "bank_change_requested", "line_items", "required_documents", "documents",
              "gps_dwell_min", "gps", "free_time_min", "detention_hours_claimed",
              "carrier_identity_mismatch", "carrier_identity")
    return _with_refs(source, ctx, {key: source[key] for key in fields if key in source})


async def match_carrier_invoice(ctx: ToolContext, arguments: dict) -> dict:
    from agents.carrier_ap.tools import three_way_match
    source = await _source(ctx)
    return _with_refs(source, ctx, three_way_match(source))


async def find_duplicate_invoices(ctx: ToolContext, arguments: dict) -> dict:
    from agents.carrier_ap.tools import duplicate_invoices
    source = await _source(ctx)
    return _with_refs(source, ctx, {"duplicate_invoice_ids": duplicate_invoices(source)})


async def audit_accessorials(ctx: ToolContext, arguments: dict) -> dict:
    from agents.risk.tools import audit_charges
    source = await _source(ctx)
    return _with_refs(source, ctx, audit_charges(source))


async def inspect_document_packet(ctx: ToolContext, arguments: dict) -> dict:
    from agents.documents.tools import check_packet
    source = await _source(ctx)
    required = ctx.env.subject.get("required_documents", source.get("required_documents"))
    return _with_refs(source, ctx, check_packet(source, required))


async def read_settlement_state(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    result = {}
    for table, fixture_key in (("hold", "holds"), ("payment_proposal", "payment_proposals"),
                               ("approval", "approvals")):
        result[fixture_key] = source.get(fixture_key, []) if source_fixtures_only() else await rows(
            ctx.agent.name, table, "case_id", ctx.env.case_id)
    result["paid_amount"] = source.get("paid_amount", 0)
    return _with_refs(source, ctx, result)


async def find_similar_cases(ctx: ToolContext, arguments: dict) -> dict:
    """Advisory recall of resolved precedents and lessons; never authorizes or changes an outcome."""
    from common import agent_recall

    source = await _source(ctx)
    if not agent_recall.enabled():
        return _with_refs(source, ctx, {"available": False, "cases": [],
                                        "missing_configuration": ["ELASTIC_CLOUD_URL",
                                                                  "ELASTIC_AGENT_MEMORY_READER_KEY"]})
    dispute = source.get("dispute")
    text = " ".join(str(v) for v in (source.get("claim_type"), source.get("lane"),
                                     dispute if isinstance(dispute, str) else "") if v)
    cases = await agent_recall.similar_cases(
        carrier_id=str(source.get("carrier_id") or ""), claim_type=str(source.get("claim_type") or ""),
        lane=str(source.get("lane") or ""), skill=ctx.env.task_type, text=text, actor=ctx.agent.name)
    # A case never counts as its own precedent.
    cases = [c for c in cases if c.get("case_id") != ctx.env.case_id]
    return _with_refs(source, ctx, {"available": True, "cases": cases})


def _matched_deposits(source: dict) -> tuple[list[dict], Decimal]:
    deposits = source.get("deposits", [])
    if not isinstance(deposits, list) or any(not isinstance(item, dict) for item in deposits):
        raise SourceUnavailable("source deposits must be records")
    invoice_id = source.get("shipper_invoice_id")
    matched = [item for item in deposits if invoice_id and item.get("invoice_id") == invoice_id
               and item.get("shipper_id") == source.get("shipper_id")]
    ids = [item.get("id") for item in matched]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        raise SourceUnavailable("duplicate or missing source deposit ID")
    return matched, sum((money(item.get("amount")) for item in matched), Decimal(0))


async def match_customer_deposits(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    matched, collected = _matched_deposits(source)
    owed = money(source.get("shipper_invoice_amount"))
    return _with_refs(source, ctx, {"shipper_invoice_id": source.get("shipper_invoice_id"),
        "invoice_amount": number(owed), "matched_amount": number(collected),
        "balance": number(owed - collected), "matched_deposit_ids": [item["id"] for item in matched],
        "overdue": source.get("overdue") is True})


async def calculate_load_margin(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    revenue, cost, contract = (money(source.get(key)) for key in
                               ("shipper_invoice_amount", "invoice_amount", "contract_rate"))
    return _with_refs(source, ctx, {"revenue": number(revenue), "invoiced_carrier_cost": number(cost),
        "contracted_carrier_cost": number(contract), "invoiced_margin": number(revenue - cost),
        "contracted_margin": number(revenue - contract)})


async def calculate_cash_exposure(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    receivables = source.get("receivables")
    if receivables is None:
        collected = source.get("shipper_paid_amount")
        if collected is None:
            _, collected = _matched_deposits(source)
        receivables = [{"amount": source.get("shipper_invoice_amount"), "collected": collected}]
    payables = source.get("payables", [{"amount": source.get("invoice_amount"),
                                       "paid": source.get("paid_amount", 0)}])
    incoming = sum((money(row["amount"]) - money(row.get("collected", 0)) for row in receivables), Decimal(0))
    outgoing = sum((money(row["amount"]) - money(row.get("paid", 0)) for row in payables), Decimal(0))
    if incoming < 0 or outgoing < 0:
        raise SourceUnavailable("source balances cannot be negative")
    return _with_refs(source, ctx, {"period": source.get("period"), "expected_collections": number(incoming),
        "outstanding_carrier_cost": number(outgoing), "net_cash": number(incoming - outgoing),
        "assumptions": ["Holds do not erase the outstanding carrier obligation."]})


async def read_credit_evidence(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    report = source.get("credit_report")
    if not isinstance(report, dict) or not report.get("source_ref"):
        return _with_refs(source, ctx, {"available": False, "reason": "No sourced credit report was ingested."})
    return _with_refs(source, ctx, {"available": True, "credit_report": report,
                                   "credit_limit_changed": False})


async def inspect_journal(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    requests = source.get("journal_requests", []) if source_fixtures_only() else await rows(
        ctx.agent.name, "journal_request", "case_id", ctx.env.case_id)
    lines = source.get("journal_lines", [])
    debit = sum((money(line.get("debit", 0)) for line in lines), Decimal(0))
    credit = sum((money(line.get("credit", 0)) for line in lines), Decimal(0))
    return _with_refs(source, ctx, {"journal_requests": requests, "debits": number(debit),
                                   "credits": number(credit), "balanced": bool(lines) and debit == credit})


async def read_verified_directory(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    repository = SourceRepository()
    counterparty = (source.get("shipper_id") if ctx.agent.name == "shipper_ar" else
                    source.get("carrier_id") or source.get("counterparty_id"))
    if ctx.agent.name == "voice_desk":
        requested = ctx.env.subject.get("counterparty_id")
        allowed = {source.get("carrier_id"), source.get("shipper_id"), source.get("counterparty_id")}
        if requested and requested not in allowed:
            raise SourceUnavailable("outreach counterparty is not part of the case source")
        counterparty = requested or counterparty
    if not counterparty:
        raise SourceUnavailable("source counterparty identifier is missing")
    try:
        company = await repository.get_company(counterparty)
        contact = await repository.get_contact(counterparty)
    except SourceUnavailable:
        return _with_refs(source, ctx, {"available": False, "counterparty_id": counterparty,
                                       "reason": "Independently verified directory contact is unavailable."})
    # The call adapter resolves the phone itself. Models need provenance and
    # verification signals, not an editable dial target or bank credentials.
    return _with_refs(source, ctx, {"available": True, "counterparty_id": counterparty,
        "authority_status": company.get("authority_status"), "insurance_verified": company.get("insurance_verified"),
        "owner_operator": company.get("owner_operator"), "directory_source": contact.get("source"),
        "contact_verified": contact.get("verified") is True, "phone_available": bool(contact.get("phone")),
        "timezone": contact.get("timezone") or company.get("timezone"),
        "submitted_phone_matches": (source.get("submitted_phone") == contact.get("phone"))
                                   if source.get("submitted_phone") else None})


async def inspect_verification_evidence(ctx: ToolContext, arguments: dict) -> dict:
    source = await _source(ctx)
    return _with_refs(source, ctx, {"verification": source.get("verification", {}),
        "bank_change_requested": bool(source.get("bank_change_requested")),
        "identity_verified": source.get("identity_verified") is True,
        "dispute": source.get("dispute"), "bank_details_changed": False})


async def read_inbound_transcript(ctx: ToolContext, arguments: dict) -> dict:
    from common.voice import redact_transcript
    source = await _source(ctx)
    inbound = source.get("inbound_call")
    if not inbound:
        return _with_refs(source, ctx, {"available": False, "requires_intake": True})
    refs = evidence(source, ctx.env)
    if not isinstance(inbound, dict) or inbound.get("source_ref") not in refs:
        raise SourceUnavailable("inbound transcript requires a source evidence reference")
    transcript = inbound.get("transcript")
    if (not isinstance(transcript, list) or not transcript or len(transcript) > 200
            or any(not isinstance(item, dict) or not isinstance(item.get("text"), str)
                   or not isinstance(item.get("role"), str) for item in transcript)):
        raise SourceUnavailable("inbound transcript must contain source speaker turns")
    return _with_refs(source, ctx, {"available": True, "transcript_ref": inbound["source_ref"],
        "transcript_redacted": redact_transcript(transcript), "financial_authorization": False})


async def _review_bundle(ctx: ToolContext) -> dict:
    from agents.validator.tools import review_bundle
    return await review_bundle(ctx.agent, ctx.env, ctx.task_id)


async def inspect_review_target(ctx: ToolContext, arguments: dict) -> dict:
    # A bounded view: the raw bundle (artifact twice, every audit event) exceeded the tool result limit live.
    from agents.validator.tools import REVIEW_TOOL_CHARS, review_view
    return review_view(await _review_bundle(ctx), limit=REVIEW_TOOL_CHARS)


async def check_review_quality(ctx: ToolContext, arguments: dict) -> dict:
    from agents.validator.tools import deterministic_review
    bundle = await _review_bundle(ctx)
    return {"review": deterministic_review(bundle).model_dump(mode="json"),
            "target_task_id": bundle["target"].get("task_id"),
            "evidence_refs": bundle.get("artifact", {}).get("evidence_refs", [])}


async def _case_company(ctx: ToolContext, *, carrier: bool = False) -> tuple[dict, dict]:
    """Resolve public lookup targets from ingested case/directory facts only."""
    source = await _source(ctx)
    side = "shipper" if ctx.agent.name == "shipper_ar" and not carrier else "carrier"
    nested = source.get(side, {})
    if not isinstance(nested, dict):
        raise SourceUnavailable("source company must be a record")
    company_id = source.get(f"{side}_id") or nested.get("id")
    company = {}
    if company_id:
        try:
            company = await SourceRepository().get_company(company_id)
        except SourceUnavailable:
            pass
    # Ingested directory fields take precedence; task subjects are never used.
    return source, {**nested, **company}


def _carrier_identifier(source: dict, company: dict, key: str) -> Any:
    aliases = (key, "mc_number") if key == "mc" else (key, "usdot_number")
    values = [company[name] for name in aliases if company.get(name) is not None]
    values.extend(source[name] for name in (f"carrier_{key}", key) if source.get(name) is not None)
    if len({str(value).strip().upper() for value in values}) > 1:
        raise SourceUnavailable("source contains conflicting carrier identifiers")
    return values[0] if values else None


async def _external_lookup(ctx: ToolContext, kind: str, operation: Callable[[], Awaitable[dict]]) -> dict:
    """Freeze terminal evidence, but let a new tool call poll an existing remote run."""
    key = f"external:{kind}"
    await ctx.agent._assert_execution(ctx.task_id)
    cached = await ctx.agent.store.get_step(ctx.task_id, key)
    if cached is not None:
        return cached["value"]
    result = await operation()
    if result.get("can_resume_lookup") is True:
        # The adapter has already journaled the paid start. A per-call tool-loop
        # checkpoint keeps this response, while a *new* model call may poll it.
        # Preserve an incomplete check for domain outcome gating if the model
        # chooses to finish before a terminal response has been obtained.
        async def incomplete():
            return {"available": False, "provider": "apify", "error": "external_lookup_incomplete",
                    "can_resume_lookup": True, "contact_authority_verified": False,
                    "financial_authorization": False, "evidence_refs": [],
                    "note": "No terminal external lookup result was obtained for this task."}

        await ctx.agent.step(ctx.task_id, f"{key}:incomplete", incomplete)
        return result

    async def terminal():
        return result

    return await ctx.agent.step(ctx.task_id, key, terminal)


async def lookup_carrier_authority(ctx: ToolContext, arguments: dict) -> dict:
    """A case-bound public FMCSA observation; never a contact/financial authorization."""
    from integrations.apify import lookup_carrier
    source, company = await _case_company(ctx, carrier=True)
    usdot = _carrier_identifier(source, company, "usdot")
    mc = _carrier_identifier(source, company, "mc")
    async def lookup():
        if usdot is None and mc is None:
            return {"available": False, "reason": "The case has no ingested USDOT or MC identifier.",
                    "missing_source_fields": ["carrier.usdot or carrier.mc"], "evidence_refs": []}
        result = await lookup_carrier(ctx.agent, ctx.env, ctx.task_id, usdot=usdot, mc=mc)
        return {**result, "contact_authority_verified": False, "financial_authorization": False}

    return await _external_lookup(ctx, "apify_carrier", lookup)


async def inspect_company_website(ctx: ToolContext, arguments: dict) -> dict:
    from integrations.apify import scrape_company
    source, company = await _case_company(ctx)
    side = "shipper" if ctx.agent.name == "shipper_ar" else "carrier"
    url = company.get("website") or company.get("website_url") or source.get(f"{side}_website")
    async def scrape():
        if not isinstance(url, str) or not url.strip():
            return {"available": False, "reason": "The case has no ingested company website.",
                    "missing_source_fields": [f"{side}.website"], "evidence_refs": []}
        result = await scrape_company(ctx.agent, ctx.env, ctx.task_id, url=url)
        return {**result, "contact_authority_verified": False, "financial_authorization": False}

    return await _external_lookup(ctx, "apify_company", scrape)


def _outreach_purpose(ctx: ToolContext) -> str:
    if ctx.agent.name == "voice_desk":
        return str(ctx.env.subject.get("purpose", CALL_PURPOSES.get(ctx.meta.from_agent, "verification")))
    return CALL_PURPOSES.get(ctx.agent.name, "verification")


async def inspect_outreach_readiness(ctx: ToolContext, arguments: dict) -> dict:
    from common.voice import voice_configuration_status
    purpose = _outreach_purpose(ctx)
    status = voice_configuration_status(purpose)
    return {**status, "provider": "cartesia", "purpose": purpose, "call_placed": False,
            "note": "Readiness is configuration only. Calls still require a case-bound verified directory "
                    "contact, demo routing, calling hours, and policy authorization."}


async def integration_availability(ctx: ToolContext, arguments: dict) -> dict:
    from integrations.apify import apify_configuration_status
    return {"outbound_voice": await inspect_outreach_readiness(ctx, {}),
            "external_carrier_lookup": apify_configuration_status("carrier"),
            "company_website_lookup": apify_configuration_status("company"),
            "external_credit_and_sanctions": {"available": False, "reason": "CRS integration is deferred."},
            "bank_transfer": {"available": False, "reason": "Nessie integration is deferred; payment proposals only."},
            "email_outreach": {"available": False, "reason": "No sending connector is configured."},
            "document_ocr": {"available": False, "reason": "Tools inspect ingested document metadata only."}}


async def _attach_external_checks(ctx: ToolContext, artifact):
    """Keep performed observations visible to parent agents and independent review."""
    store = getattr(ctx.agent, "store", None)
    if store is None:
        return artifact
    checks = {}
    for name in ("apify_carrier", "apify_company"):
        step = await store.get_step(ctx.task_id, f"external:{name}")
        if step is None:
            step = await store.get_step(ctx.task_id, f"external:{name}:incomplete")
        if step and isinstance(step.get("value"), dict):
            checks[name] = step["value"]
    if not checks:
        return artifact
    refs = list(artifact.evidence_refs)
    for result in checks.values():
        refs.extend(ref for ref in result.get("evidence_refs", []) if isinstance(ref, str))
    signals = []
    carrier = checks.get("apify_carrier")
    if carrier:
        records = carrier.get("records", [])
        records = [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []
        if carrier.get("available") is not True:
            signals.append("external_carrier_lookup_unavailable")
        if carrier.get("identity_match") is False:
            signals.append("external_carrier_identity_mismatch")
        if any(record.get("authority_confirmed_active") is False for record in records):
            signals.append("external_carrier_authority_not_active")
        elif carrier.get("available") is True and not any(
                record.get("authority_confirmed_active") is True for record in records):
            signals.append("external_carrier_authority_unconfirmed")
        if any(record.get("out_of_service") is True for record in records):
            signals.append("external_carrier_out_of_service")
    outcome = artifact.outcome
    if signals and ctx.agent.name == "verification" and outcome == "verified":
        outcome = "unverified"
    elif signals and ctx.agent.name == "risk" and outcome == "clear":
        outcome = "suspicious"
    return artifact.model_copy(update={"outcome": outcome,
        "findings": {**artifact.findings, "external_checks": checks, "external_risk_signals": signals},
        "evidence_refs": list(dict.fromkeys(refs))})


async def execute_current_skill(ctx: ToolContext, arguments: dict) -> dict:
    if ctx.env.task_type not in AGENTS[ctx.agent.name].skills:
        raise ValueError("assigned skill does not belong to this agent")
    if ctx.agent.name == "voice_desk" and ctx.env.task_type == "place_call" and not fixture_mode():
        availability = await integration_availability(ctx, {})
        if not availability["outbound_voice"]["available"]:
            from common.envelope import ResultArtifact
            artifact = ResultArtifact(outcome="unverified", findings={"outbound_call_placed": False,
                "available": False, "reason": "Outbound voice configuration is incomplete.",
                "missing_configuration": availability["outbound_voice"]["missing_configuration"],
                "invalid_configuration": availability["outbound_voice"]["invalid_configuration"]})
            return {"artifact": artifact.model_dump(mode="json")}
    result = await ctx.agent.run_workflow(ctx.env, ctx.meta, ctx.task_id, None)
    result = await _attach_external_checks(ctx, result)
    return {"artifact": result.model_dump(mode="json")}


# These are optional specialist checks. Existing action workflows still perform
# mandatory dependencies. Matching invocation keys reuse their durable children.
_DELEGATIONS = {
    # Carrier Settlement may need all three slots for packet, audit and approval.
    # Its workflow exclusively owns delegation, so optional checks cannot starve it.
    "shipper_ar": {"assess_financial_impact": "receivable_cash_impact"},
    "cfo": {"answer_payment_status": "tool_payment_status", "check_already_paid": "tool_paid_status"},
    "risk": {"verify_dispute": "carrier_trust", "verify_bank_change": "carrier_trust",
             "verify_claim": "charge_verification", "check_document_packet": "tool_documents"},
    "controller": {"assess_financial_impact": "settlement_cash_impact",
                   "check_document_packet": "tool_documents"},
}


def _delegations_for(ctx: ToolContext) -> dict[str, str]:
    available = _DELEGATIONS.get(ctx.agent.name, {})
    if ctx.agent.name == "shipper_ar" and ctx.env.task_type == "set_credit_limit":
        return {}
    if ctx.agent.name == "risk":
        skill = {"screen_bank_change": "verify_bank_change", "screen_counterparty": "verify_dispute",
                 "investigate_charge": "verify_claim"}[ctx.env.task_type]
        return {name: key for name, key in available.items() if name in {skill, "check_document_packet"}}
    return available


async def delegate_specialist(ctx: ToolContext, arguments: dict) -> dict:
    skill = arguments["skill"]
    allowed = _delegations_for(ctx)
    if skill not in allowed or SKILL_OWNER.get(skill) not in AGENTS[ctx.agent.name].may_delegate_to:
        raise ValueError("specialist is not permitted for this agent")
    # Bind the child to this case and the source contract; do not allow the model
    # to pass its own amount, bank data, approval or arbitrary task subject.
    child = child_envelope(ctx.env, skill)
    result = await ctx.agent.delegate(skill, child, ctx.meta, ctx.task_id,
                                      invocation_key=allowed[skill])
    return {"skill": skill, "agent": SKILL_OWNER[skill], "results": result}


# A tool belongs only to the roles that need it. Every exposed handler has a
# narrow source/state contract; neither arbitrary SQL nor a generic mutation exists.
_ROLE_TOOLS = {
    "carrier_ap": (match_carrier_invoice, find_duplicate_invoices, inspect_document_packet,
                   read_settlement_state, audit_accessorials, find_similar_cases),
    "shipper_ar": (match_customer_deposits, read_credit_evidence, inspect_journal, read_verified_directory,
                   inspect_company_website, inspect_outreach_readiness),
    "cfo": (calculate_load_margin, calculate_cash_exposure, read_settlement_state),
    "risk": (audit_accessorials, find_duplicate_invoices, inspect_document_packet, inspect_verification_evidence,
             lookup_carrier_authority, find_similar_cases),
    "controller": (read_settlement_state, inspect_journal, audit_accessorials, find_similar_cases),
    "verification": (read_verified_directory, inspect_verification_evidence, inspect_document_packet,
                     lookup_carrier_authority, inspect_company_website, inspect_outreach_readiness),
    "voice_desk": (read_verified_directory, read_inbound_transcript, inspect_outreach_readiness),
    "validator": (inspect_review_target, check_review_quality, find_similar_cases),
    "documents": (inspect_document_packet, read_verified_directory, inspect_company_website, inspect_outreach_readiness),
}
_DESCRIPTIONS = {
    "read_load_record": "Read authoritative facts for the assigned load; caller subject is not a source of live facts.",
    "match_carrier_invoice": "Match invoice totals, rate confirmation, tolerance and required delivery evidence.",
    "find_duplicate_invoices": "Check source invoice history for duplicate carrier/load/amount within 14 days.",
    "audit_accessorials": "Compute detention timing and unsupported fees using this load's recorded terms and receipts.",
    "inspect_document_packet": "Check ingested packet metadata for missing documents, signed POD and matching load IDs.",
    "read_settlement_state": "Read current case holds, payment proposals and approvals; this does not authorize payment.",
    "match_customer_deposits": "Match unique source deposits to this shipper invoice and calculate its exact-cent balance.",
    "calculate_load_margin": "Calculate contracted and invoiced brokerage margin from authoritative source amounts.",
    "calculate_cash_exposure": "Calculate outstanding collections, carrier payables and net cash without writing forecasts.",
    "read_credit_evidence": "Read an ingested sourced credit report; does not fetch credit data or change credit limits.",
    "inspect_journal": "Inspect this case's journal requests and check source journal debits equal credits.",
    "read_verified_directory": "Read case-bound verified contact provenance, carrier authority and insurance signals.",
    "inspect_verification_evidence": "Read ingested verification and bank-change evidence; never updates bank details.",
    "read_inbound_transcript": "Read and redact an already ingested inbound call transcript; never dials a phone.",
    "inspect_review_target": "Load the assigned review target, its persisted artifact, source facts and audit history.",
    "check_review_quality": "Run independent exact-cent, provenance and policy-result checks on the assigned review target.",
    "lookup_carrier_authority": "Use Apify to look up public FMCSA records for this case's ingested USDOT/MC. "
                                "Use for carrier authority or identity questions. Public data never authorizes payment "
                                "or makes a contact verified; missing identifiers or credentials are reported explicitly.",
    "inspect_company_website": "Use Apify to inspect the case company's ingested public website for contact evidence. "
                               "Discovered contacts remain unverified; do not dial or email them directly.",
    "inspect_outreach_readiness": "Check Cartesia configuration for this role's call purpose without placing a call "
                                  "or exposing credentials. Actual calls use the guarded assigned workflow.",
    "find_similar_cases": "Recall resolved precedents and approved lessons for this carrier, claim type and lane "
                          "from shared agent memory. Advisory: cite them, but the guarded workflow decides.",
    "integration_availability": "Report actual external connector availability and missing configuration names, never keys.",
}


def registry_for(ctx: ToolContext) -> dict[str, ToolSpec]:
    role = ctx.agent.name
    if role not in _ROLE_TOOLS or ctx.env.task_type not in AGENTS[role].skills:
        raise ValueError("agent or assigned skill is not registered")
    handlers = (read_load_record, *_ROLE_TOOLS[role], integration_availability)
    registry = {handler.__name__: ToolSpec(handler.__name__, _DESCRIPTIONS[handler.__name__],
                                          EMPTY_PARAMETERS.copy(), handler) for handler in handlers}
    delegations = _delegations_for(ctx)
    if delegations:
        skills = sorted(delegations)
        registry["delegate_specialist"] = ToolSpec("delegate_specialist",
            "Ask an allowed brokerage specialist about this case over authenticated A2A. "
            "Optional check; the final action performs mandatory dependencies. Do not redelegate for chatter.",
            {"type": "object", "properties": {"skill": {"type": "string", "enum": skills}},
             "required": ["skill"], "additionalProperties": False}, delegate_specialist, ("a2a",))
    registry["execute_current_skill"] = ToolSpec("execute_current_skill",
        f"Complete the assigned {ctx.env.task_type} skill through the guarded {AGENTS[role].title} workflow. "
        "Call this after useful inspections and before finalizing. Re-reads authoritative facts, enforces policy, "
        "delegates mandatory checks, and may wait for approval or evidence. Only its artifact establishes the outcome.",
        EMPTY_PARAMETERS.copy(), execute_current_skill, ("guarded_workflow",))
    return registry
