"""Seed explicitly labeled W2 fixtures through real reducers; sends no calls or emails.

This exercises database/UI functionality only. W4 supplies real directory data and the simulator,
W3 runs actual A2A tasks, and W1 supplies real model telemetry. All fixture IDs have a W2 prefix.
"""
from __future__ import annotations

import argparse

from scripts.spacetime_admin import LocalAdmin


def seed(admin: LocalAdmin):
    def write(actor: str, reducer: str, key: str, **payload):
        admin.call(actor, reducer, {"write_id": f"w2-fixture-v1:{reducer}:{key}", **payload})

    case = "CASE-W2-4821"
    write("ingest", "open_case", case, case_id=case, load_id="LOAD-W2-4821", priority="P0",
          title="[W2 fixture] Unsupported detention · Stockton → Reno", exposure_usd=2150)
    write("ingest", "upsert_company", "carrier", id="C-W2-311", kind="carrier",
          name="Sierra Haul · synthetic W2 fixture", phone="+15555550199",
          website="https://example.com", source_url="https://example.com/w2-fixture")
    write("ingest", "upsert_contact", "dispatcher", id="CONTACT-W2-311", company_id="C-W2-311",
          name="Fixture dispatcher", title="Synthetic test contact", phone="+15555550199",
          email="fixture@example.com", source="website", confidence=0,
          source_url="https://example.com/w2-fixture", alternates_json={"fixture": True, "enrichment_performed": False})
    write("ingest", "upsert_invoice", "invoice", id="INV-W2-4821", case_id=case,
          carrier_id="C-W2-311", load_id="LOAD-W2-4821", amount=2150, contract_amount=1850)

    write("carrier_ap", "upsert_task", "ap-start", task_id="TASK-W2-AP", case_id=case,
          skill="process_carrier_invoice", a2a_state="working", business_status="investigating",
          display_id="TASK-W2-AP", model_used="fixture/no-inference")
    write("carrier_ap", "record_doc_read", "invoice-read", id="DOC-W2-INVOICE", task_id="TASK-W2-AP",
          doc_ref="fixture://invoice/INV-W2-4821", page=1, span="line 3: detention $300",
          extracted_json={"detention_usd": 300, "fixture": True})
    write("carrier_ap", "record_delegation", "ap-risk", id="EDGE-W2-AP-RISK", case_id=case,
          parent_task_id="TASK-W2-AP", task_id="TASK-W2-RISK", to_agent="risk", skill="investigate_charge")
    write("risk", "upsert_task", "risk-start", task_id="TASK-W2-RISK", case_id=case,
          parent_task_id="TASK-W2-AP", skill="investigate_charge", a2a_state="working",
          business_status="investigating", model_used="fixture/no-inference")
    write("risk", "record_delegation", "risk-verify", id="EDGE-W2-RISK-VERIFY", case_id=case,
          parent_task_id="TASK-W2-RISK", task_id="TASK-W2-VERIFY", to_agent="verification", skill="verify_claim")
    write("verification", "upsert_task", "verify", task_id="TASK-W2-VERIFY", case_id=case,
          parent_task_id="TASK-W2-RISK", skill="verify_claim", a2a_state="input-required",
          business_status="awaiting_call", artifact_json={"fixture": True, "note": "No call has been placed."})
    write("carrier_ap", "record_delegation", "ap-controller", id="EDGE-W2-AP-CONTROLLER", case_id=case,
          parent_task_id="TASK-W2-AP", task_id="TASK-W2-CONTROLLER", to_agent="controller", skill="review_hold")
    write("controller", "upsert_task", "controller-start", task_id="TASK-W2-CONTROLLER", case_id=case,
          skill="review_hold", a2a_state="working", business_status="investigating")
    write("controller", "record_doc_read", "controller-read", id="DOC-W2-CONTROLLER", task_id="TASK-W2-CONTROLLER",
          doc_ref="fixture://invoice/INV-W2-4821", page=1, span="line 3: detention $300",
          extracted_json={"fixture": True, "detention_usd": 300, "gps_dwell_minutes": 47})
    write("controller", "place_hold", "hold", id="HOLD-W2-300", case_id=case, invoice_id="INV-W2-4821",
          task_id="TASK-W2-CONTROLLER", amount=300, reason_code="P-001",
          summary="W2 fixture: unsupported detention held", policy_ids=["P-001"])
    write("admin", "set_period", "period", period="2026-10", status="open")
    write("carrier_ap", "request_journal_entry", "journal-request", id="JR-W2-4821", case_id=case,
          task_id="TASK-W2-AP", period="2026-10", memo="W2 fixture: recognize contracted freight expense",
          lines=[{"account": "freight_expense", "debit": 1850, "credit": 0},
                 {"account": "accounts_payable", "debit": 0, "credit": 1850}])
    write("controller", "post_journal_entry", "journal", id="JE-W2-4821", request_id="JR-W2-4821",
          task_id="TASK-W2-CONTROLLER")
    write("cfo", "upsert_forecast", "forecast-v1", id="FORECAST-W2-4821", case_id=case,
          period="2026-10", category="Expected carrier payable (fixture)", amount=-2150,
          confidence=0.6, assumptions_json={"fixture": True, "hold_treatment": "before review"})
    write("cfo", "upsert_forecast", "forecast-v2", id="FORECAST-W2-4821", case_id=case,
          period="2026-10", category="Expected carrier payable (fixture)", amount=-1850,
          confidence=0.95, assumptions_json={"fixture": True, "held_usd": 300})
    write("risk", "upsert_task", "risk-complete", task_id="TASK-W2-RISK", case_id=case,
          parent_task_id="TASK-W2-AP", skill="investigate_charge", a2a_state="completed",
          business_status="resolved", outcome="suspicious", confidence=0.95,
          artifact_json={"fixture": True, "detention_claim_hours": 2, "gps_minutes": 47}, policy_ids=["P-001"])
    write("carrier_ap", "upsert_task", "ap-complete", task_id="TASK-W2-AP", case_id=case,
          skill="process_carrier_invoice", a2a_state="completed", business_status="resolved",
          outcome="hold", confidence=0.95, policy_ids=["P-001"],
          evidence_refs=["fixture://invoice/INV-W2-4821"], artifact_json={"fixture": True, "hold_usd": 300})
    write("validator", "add_review", "review", id="REVIEW-W2-AP", target_task_id="TASK-W2-AP",
          case_id=case, verdict="issue", checks_json=[{"name": "detention evidence", "ok": True}],
          friction_json=[{"kind": "missing_doc", "detail": "Fixture: rate con was requested twice", "cost_s": 15}],
          actions=["none"])
    write("validator", "upsert_memory", "memory-v1", id="MEM-W2-AP", agent="carrier_ap",
          scope={"claim_type": "detention"}, text="W2 fixture: retrieve the rate confirmation with the invoice.",
          source_ref="REVIEW-W2-AP", active=True)
    write("validator", "upsert_memory", "memory-v2", id="MEM-W2-AP", agent="carrier_ap",
          scope={"claim_type": "detention"}, text="W2 fixture: retrieve rate confirmation and GPS dwell evidence together.",
          source_ref="REVIEW-W2-AP", active=True)
    write("validator", "publish_instruction_version", "instruction-v1", agent="carrier_ap",
          diff="+ Retrieve the rate confirmation before evaluating detention (W2 fixture).",
          reason="Fixture missing-document friction", source_ref="REVIEW-W2-AP")
    write("validator", "publish_instruction_version", "instruction-v2", agent="carrier_ap",
          diff="+ Include GPS dwell evidence in the detention review (W2 fixture).",
          reason="Fixture review improvement", source_ref="REVIEW-W2-AP")
    write("human:operator", "raise_flag", "flag", id="FLAG-W2-FORECAST", case_id=case,
          target_ref="cash_forecast:FORECAST-W2-4821:amount", kind="unclear",
          note="W2 fixture: explain why held detention is excluded from projected cash outflow.")

    for i, (title, value) in enumerate([
        ("Bank detail change awaiting human approval", 9400),
        ("Missing delivery receipt", 3200), ("Customer short payment", 425),
        ("Clean invoice awaiting AP", 1725),
    ], start=1):
        write("ingest", "open_case", f"case-{i}", case_id=f"CASE-W2-{i}", load_id=f"LOAD-W2-{i}",
              title=f"[W2 fixture] {title}", priority="P1" if i == 1 else "P2", exposure_usd=value)
    write("ingest", "upsert_invoice", "bank-invoice", id="INV-W2-BANK", case_id="CASE-W2-1",
          carrier_id="C-W2-311", load_id="LOAD-W2-1", amount=9400, contract_amount=9400)
    write("controller", "place_hold", "bank-hold", id="HOLD-W2-9400", case_id="CASE-W2-1",
          invoice_id="INV-W2-BANK", amount=9400, reason_code="P-004")
    write("controller", "request_approval", "bank-approval", id="APPROVAL-W2-9400", case_id="CASE-W2-1",
          action="release_hold", target_id="HOLD-W2-9400", amount=9400,
          approver=admin.identity("human:operator")["identity"])
    print("Seeded labeled W2 fixtures through authenticated reducers. No external calls, emails or inference occurred.")
    print("Scoreboard and harness telemetry stay empty until their owners report real measurements.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:3000")
    parser.add_argument("--database", default="freightops")
    args = parser.parse_args()
    admin = LocalAdmin(args.server, args.database)
    try:
        seed(admin)
    finally:
        admin.client.close()


if __name__ == "__main__":
    main()
