"""Real HTTP acceptance checks against a fresh, isolated local SpacetimeDB database.

Runs direct requests deliberately: reducer authorization and financial controls must hold even
when a caller bypasses Python policy preflight. Never publishes over a user's existing database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from scripts.spacetime_admin import ROOT, LocalAdmin


def run(admin: LocalAdmin) -> dict:
    call = admin.raw_call
    checks: list[str] = []

    def passed(label: str):
        checks.append(label)
        print(f"PASS {label}")

    def denied(actor: str, reducer: str, payload: dict):
        before = {table: len(admin.rows(table)) for table in ("change_set", "cell_change", "audit_event")}
        response = call(actor, reducer, payload, expected_error=True)
        assert response.status_code >= 400
        after = {table: len(admin.rows(table)) for table in before}
        assert before == after, f"{reducer} left partial audit writes"

    admin.publish()
    admin.provision()
    for table in (
        "case", "task", "agent", "agent_config", "ledger_entry", "hold", "payment_proposal",
        "approval", "audit_event", "call", "precedent", "llm_call", "metric", "delegation_edge",
        "change_set", "cell_change", "doc_read", "review", "agent_memory", "instruction_version",
        "flag", "company", "contact", "routing_outbox", "dispatch",
    ):
        admin.rows(table)
    passed("all PRD public tables query through the real HTTP API")

    dispatch = {"event_id": "EVENT-INTAKE", "case_id": "REMOTE-CASE", "skill": "process_carrier_invoice",
                "agent": "carrier_ap", "task_id": "REMOTE-TASK", "status": "accepted",
                "reason": "Invoice received", "queue_ms": 120, "policy_version": "first-hop-v1"}
    denied("harness", "record_dispatch", dispatch)
    call("router", "record_dispatch", dispatch)
    assert admin.rows("dispatch")[0]["status"] == "accepted"
    call("router", "record_dispatch", {**dispatch, "queue_ms": 500})
    rows = admin.rows("dispatch")
    assert len(rows) == 1 and rows[0]["status"] == "duplicate" and rows[0]["queue_ms"] == 120
    denied("router", "record_dispatch", {**dispatch, "task_id": "CHANGED-TASK"})
    passed("router dispatch accepts remote tasks, deduplicates events and freezes accepted routes")

    base = {"case_id": "CASE-W2-TEST", "load_id": "LOAD-W2-TEST", "title": "W2 acceptance test",
            "priority": "P0", "exposure_usd": 12000}
    denied("outsider", "open_case", base)
    denied("carrier_ap", "open_case", base)
    call("ingest", "open_case", base)
    passed("anonymous/unassigned identities and wrong roles cannot write")

    invoice = {"id": "INV-W2-TEST", "case_id": base["case_id"], "carrier_id": "C-W2-TEST",
               "load_id": base["load_id"], "amount": 12000, "contract_amount": 12000, "paid_amount": 0}
    call("ingest", "upsert_invoice", invoice)
    hold = {"id": "HOLD-W2-TEST", "case_id": base["case_id"], "invoice_id": invoice["id"],
            "amount": 9400, "reason_code": "P-004"}
    denied("validator", "place_hold", hold)
    denied("controller", "place_hold", {**hold, "amount": 12000.001})
    denied("controller", "place_hold", {**hold, "amount": -1})
    call("controller", "place_hold", hold)
    denied("controller", "place_hold", {**hold, "id": "OVER-HOLD", "amount": 2600.01})
    denied("carrier_ap", "propose_payment", {
        "id": "PAY-HELD", "case_id": base["case_id"], "invoice_id": invoice["id"],
        "carrier_id": invoice["carrier_id"], "amount": 100,
    })
    denied("controller", "release_hold", {"id": hold["id"], "amount": 9400})
    denied("controller", "release_hold", {"id": hold["id"], "amount": 4700})
    denied("controller", "release_hold", {"id": hold["id"], "amount": 9400, "approval_id": "made-up"})
    passed("invoice bounds, exact cents, role restrictions, held-payment and $5k release controls")

    approval = {"id": "APPROVE-W2-TEST", "case_id": base["case_id"], "action": "release_hold",
                "target_id": hold["id"], "amount": 9400,
                "approver": admin.identity("human:operator")["identity"]}
    call("controller", "request_approval", approval)
    denied("controller", "decide_approval", {"id": approval["id"], "decision": "approved", "code": "123456"})
    call("admin", "issue_approval_code", {
        "approval_id": approval["id"], "code": "123456", "expires_in_seconds": 300,
    })
    call("human:operator", "decide_approval", {
        "id": approval["id"], "decision": "approved", "code": "999999", "code_ok": True,
    })
    rejected_attempt = next(row for row in admin.rows("approval") if row["id"] == approval["id"])
    assert rejected_attempt["decision"] == "pending" and rejected_attempt["failed_attempts"] == 1
    assert rejected_attempt["last_error"]
    call("human:operator", "decide_approval", {"id": approval["id"], "decision": "approved", "code": "123456"})
    denied("human:operator", "decide_approval", {"id": approval["id"], "decision": "approved", "code": "123456"})
    release = {"write_id": "release-once", "id": hold["id"], "amount": 9400, "approval_id": approval["id"]}
    call("controller", "release_hold", release)
    count = len(admin.rows("change_set"))
    call("controller", "release_hold", release)
    assert len(admin.rows("change_set")) == count
    denied("controller", "release_hold", {**release, "amount": 1})
    assert admin.rows("hold")[0]["status"] == "released"
    passed("stored human approval, one-time code, atomic release and stable-id replay")

    payment_approval = {**approval, "id": "APPROVE-PAYMENT", "action": "propose_payment",
                        "target_id": "PAY-HIGH", "invoice_id": invoice["id"]}
    call("controller", "request_approval", payment_approval)
    call("admin", "issue_approval_code", {"approval_id": payment_approval["id"], "code": "654321"})
    call("human:operator", "decide_approval", {
        "id": payment_approval["id"], "decision": "approved", "code": "654321",
    })
    call("ingest", "upsert_invoice", {**invoice, "id": "INV-OTHER", "carrier_id": "C-OTHER"})
    payment = {"id": "PAY-HIGH", "case_id": base["case_id"], "invoice_id": invoice["id"],
               "amount": 9400, "approval_id": payment_approval["id"]}
    denied("carrier_ap", "propose_payment", {**payment, "invoice_id": "INV-OTHER"})
    call("carrier_ap", "propose_payment", payment)
    denied("carrier_ap", "propose_payment", {**payment, "id": "PAY-SPLIT", "amount": 100, "approval_id": ""})
    call("ingest", "upsert_invoice", {**invoice, "id": "INV-SMALL", "carrier_id": "C-SMALL",
         "amount": 1000, "contract_amount": 1000})
    small_payment = {"case_id": base["case_id"], "invoice_id": "INV-SMALL", "amount": 100}
    call("carrier_ap", "propose_payment", {**small_payment, "id": "PAY-SMALL"})
    denied("carrier_ap", "propose_payment", {**small_payment, "id": "PAY-DUPLICATE"})
    passed("approved payment binds the exact invoice and duplicate payments are refused")

    call("admin", "set_period", {"period": "2026-10", "status": "open"})
    entry = {"id": "JR-W2-TEST", "case_id": base["case_id"], "period": "2026-10", "memo": "W2 test",
             "lines": [{"account": "freight_expense", "debit": 1850, "credit": 0},
                       {"account": "accounts_payable", "debit": 0, "credit": 1850}]}
    denied("carrier_ap", "request_journal_entry", {
        **entry, "id": "JR-UNBALANCED", "lines": [{"account": "bad", "debit": 100, "credit": 0}],
    })
    call("carrier_ap", "request_journal_entry", entry)
    denied("controller", "post_journal_entry", {
        "id": "JE-FORGE", "request_id": entry["id"], "requested_by": "some-other-agent",
    })
    call("controller", "post_journal_entry", {"id": "JE-W2-TEST", "request_id": entry["id"]})
    call("controller", "request_journal_entry", {**entry, "id": "JR-SELF"})
    denied("controller", "post_journal_entry", {"id": "JE-SELF", "request_id": "JR-SELF"})
    call("carrier_ap", "request_journal_entry", {**entry, "id": "JR-LOCKED"})
    call("admin", "set_period", {"period": "2026-10", "status": "locked"})
    denied("controller", "post_journal_entry", {"id": "JE-LOCKED", "request_id": "JR-LOCKED"})
    assert len(admin.rows("ledger_entry")) == 1
    passed("balanced journals, locked periods and authenticated requester/approver separation")

    # Every delegation must be justified by an existing task owned by the caller.
    call("controller", "upsert_task", {"task_id": "TASK-ROOT", "case_id": base["case_id"],
         "skill": "reconcile", "a2a_state": "working", "business_status": "investigating"})
    chain = [("controller", "cfo", "TASK-ROOT", "TASK-CFO", "cash_forecast"),
             ("cfo", "carrier_ap", "TASK-CFO", "TASK-AP", "process_carrier_invoice")]
    for source, target, parent, task, skill in chain:
        call(source, "record_delegation", {"id": f"EDGE-{task}", "case_id": base["case_id"],
             "parent_task_id": parent, "task_id": task, "to_agent": target, "skill": skill})
    denied("carrier_ap", "record_delegation", {"id": "EDGE-CYCLE", "case_id": base["case_id"],
           "parent_task_id": "TASK-AP", "task_id": "TASK-CYCLE", "to_agent": "controller", "skill": "reconcile"})
    denied("risk", "record_delegation", {"id": "EDGE-SPOOF", "case_id": base["case_id"],
           "parent_task_id": "TASK-AP", "task_id": "TASK-SPOOF", "to_agent": "verification", "skill": "verify_claim"})
    call("carrier_ap", "upsert_task", {"task_id": "TASK-AP", "case_id": base["case_id"],
         "parent_task_id": "TASK-CFO", "skill": "process_carrier_invoice", "a2a_state": "completed",
         "business_status": "resolved", "outcome": "clear", "artifact_json": {"test": True}})
    denied("carrier_ap", "upsert_task", {"task_id": "TASK-AP", "case_id": base["case_id"],
           "parent_task_id": "TASK-CFO", "skill": "process_carrier_invoice", "a2a_state": "working",
           "business_status": "investigating"})
    denied("carrier_ap", "upsert_task", {"task_id": "TASK-AP", "a2a_state": "completed",
           "artifact_json": {"tampered": True}})
    denied("cfo", "upsert_forecast", {"id": "F-SPOOF", "case_id": base["case_id"],
           "task_id": "TASK-AP", "period": "2026-10", "category": "test", "amount": 100})
    assert not admin.rows("cash_forecast"), "failed attribution check must also roll back the financial row"
    passed("owned task graph, cycle prevention and terminal-state protection")

    flag = {"id": "FLAG-W2-TEST", "target_ref": f"case:{base['case_id']}", "kind": "unclear",
            "note": "Review acceptance-test evidence", "case_id": base["case_id"]}
    call("human:operator", "raise_flag", flag)
    assert admin.rows("routing_outbox"), "human flag must create durable Validator work"
    work = admin.rows("routing_outbox")[0]
    denied("router", "ack_outbox", {"id": work["id"], "success": True})
    call("router", "claim_outbox", {"id": work["id"], "lease_seconds": 30})
    call("router", "ack_outbox", {"id": work["id"], "success": True})
    passed("human flag creates a durable work item for the W3 router")

    call("validator", "add_review", {"id": "REVIEW-TEST", "target_task_id": "TASK-AP",
         "case_id": base["case_id"], "verdict": "pass", "checks_json": [], "friction_json": [], "actions": ["none"]})
    call("validator", "upsert_memory", {"id": "MEM-TEST", "agent": "carrier_ap",
         "scope": {"claim_type": "detention"}, "text": "Read the source receipt", "source_ref": "REVIEW-TEST"})
    call("validator", "upsert_memory", {"id": "MEM-TEST", "agent": "carrier_ap",
         "scope": {"claim_type": "detention"}, "text": "Read the receipt and GPS", "source_ref": "REVIEW-TEST"})
    assert len(admin.rows("memory_revision")) == 2
    denied("validator", "upsert_memory", {"id": "MEM-POLICY", "agent": "carrier_ap",
           "scope": {"policy": "override"}, "text": "Invalid scope", "source_ref": "REVIEW-TEST"})
    from agents.validator.tools import instruction_proposal
    from common.learning import reconstruct_instructions
    original, _ = reconstruct_instructions("carrier_ap", [])
    call("validator", "publish_instruction_version", {"agent": "carrier_ap", "diff": instruction_proposal(original),
         "expected_version": 0, "reason": "Acceptance test", "source_ref": "REVIEW-TEST"})
    call("human:operator", "revert_instruction_version", {"agent": "carrier_ap", "version": 0,
         "expected_version": 1, "reason": "Acceptance test baseline rollback"})
    call("human:operator", "revert_instruction_version", {"agent": "carrier_ap", "version": 1,
         "expected_version": 2, "reason": "Acceptance test compensating version"})
    versions = admin.rows("instruction_version")
    assert len(versions) == 3 and len([row for row in versions if row["active"]]) == 1
    assert next(row for row in versions if row["active"])["version"] == 3
    passed("immutable memory history and compensating instruction revert")

    call("human:operator", "update_case", {"case_id": base["case_id"], "status": "human_owned",
         "reason": "Acceptance test takeover"})
    denied("controller", "place_hold", {**hold, "id": "TAKEOVER-HOLD", "amount": 1})
    denied("controller", "update_case", {"case_id": base["case_id"], "status": "open", "reason": "Unauthorized release"})
    call("human:operator", "update_case", {"case_id": base["case_id"], "status": "open", "reason": "Resume case"})
    passed("human takeover blocks autonomous financial changes until human release")

    # Concurrent ingress while another case exists, with no global transaction ordering assumptions.
    def open_concurrent(i: int) -> float:
        start = time.monotonic()
        call("ingest", "open_case", {**base, "case_id": f"CASE-PAR-{i}", "load_id": f"LOAD-PAR-{i}"})
        return (time.monotonic() - start) * 1000

    with ThreadPoolExecutor(max_workers=8) as pool:
        latencies = list(pool.map(open_concurrent, range(24)))
    assert len(admin.rows("case")) == 25
    passed("24 concurrent cases commit independently")

    changes = admin.rows("change_set")
    audits = admin.rows("audit_event")
    cells = admin.rows("cell_change")
    for row in [*changes, *audits]:
        assert hashlib.sha256(row["hash_body"].encode()).hexdigest() == row["hash"], row["id"]
    for row in [*changes, *audits, *cells]:
        serialized = json.dumps(row)
        assert '"code_hash"' not in serialized and '\\"code\\"' not in serialized
    by_id = {row["id"]: row for row in changes}
    for row in changes:
        body = json.loads(row["hash_body"])
        actual_cells = [{key: cell[key] for key in ("sheet", "row_key", "column", "before", "after")}
                        for cell in cells if cell["change_set_id"] == row["id"]]
        cell_key = lambda cell: (cell["sheet"], cell["row_key"], cell["column"])
        assert sorted(body["cells"], key=cell_key) == sorted(actual_cells, key=cell_key)
        for key, value in body.items():
            if key != "cells":
                assert row[key] == value, f"changed metadata {row['id']}:{key}"
        if row["parent_id"]:
            assert row["parent_id"] in by_id
            assert by_id[row["parent_id"]]["case_id"] == row["case_id"]
            assert body["parent_hash"] == by_id[row["parent_id"]]["hash"]
    for event in audits:
        assert json.loads(event["hash_body"]) == {
            key: value for key, value in event.items() if key not in {"hash", "hash_body"}
        }
    for case_id in {event["case_id"] for event in audits}:
        chain = sorted((event for event in audits if event["case_id"] == case_id), key=lambda event: event["sequence"])
        for index, event in enumerate(chain):
            assert event["sequence"] == index + 1
            assert event["prev_hash"] == (chain[index - 1]["hash"] if index else "")
    assert all(row["change_set_id"] in by_id for row in cells)
    assert any(row["sheet"] == "holds" and row["column"] == "status" for row in cells)
    passed("SHA-256 audit/change chains and attached before/after cells verify independently")
    return {"database": admin.database, "checks": checks, "cases": 25,
            "changes": len(changes), "audit_events": len(audits), "cell_changes": len(cells),
            "concurrent_http_median_ms": round(statistics.median(latencies), 1),
            "concurrent_http_max_ms": round(max(latencies), 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    admin = LocalAdmin(args.server, f"freightops-test-{uuid.uuid4().hex[:10]}")
    try:
        result = run(admin)
        (admin.directory / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        subprocess.run([
            str(ROOT / "spacetime/module/spacetimedb/node_modules/.bin/tsx"),
            str(ROOT / "scripts/spacetime_subscriptions.ts"), admin.database, admin.server,
        ], cwd=ROOT, check=True)
    finally:
        admin.client.close()


if __name__ == "__main__":
    main()
