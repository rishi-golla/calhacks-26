"""Deterministic checks + friction mining for the Validator. No LLM needed for the verdict."""
from common import store

FRICTION_MAP = {"failed": "retry", "delegation_refused": "reject", "policy_denied": "policy_deny",
                "callback_failed": "timeout", "resume_failed": "timeout", "human_override": "human_override"}
POLICY_CHANGE_WORDS = ("limit", "threshold", "policy", "permission", "allowlist")


def checks(agent: str, task_id: str, inp: dict, result: dict) -> list[dict]:
    f, out = result.get("findings", {}), []
    if result.get("outcome") in ("hold", "short_pay", "reject"):
        out.append({"check": "policy_cited", "ok": bool(result.get("policy_ids"))})
    if {"approved_amount", "disputed_amount", "invoice_amount"} <= f.keys():
        ok = round(f["approved_amount"] + f["disputed_amount"] - f["invoice_amount"], 2) == 0
        out.append({"check": "amount_tieout", "ok": ok,
                    "detail": f"{f['approved_amount']} + {f['disputed_amount']} vs {f['invoice_amount']}"})
    lines = f.get("journal") or f.get("lines")
    if lines:
        dr, cr = sum(x.get("debit", 0) for x in lines), sum(x.get("credit", 0) for x in lines)
        out.append({"check": "balanced_entry", "ok": round(dr - cr, 2) == 0, "detail": f"Dr {dr} / Cr {cr}"})
    if result.get("outcome") not in ("info", "not_implemented"):
        has_audit = any(cs["task_id"] == task_id for cs in store.change_sets())
        out.append({"check": "audit_trail", "ok": has_audit or agent == "cfo"})
    return out


def friction(case_id: str, inp: dict) -> list[dict]:
    out = []
    for e in store.events(case_id):
        kind = FRICTION_MAP.get(e["kind"])
        if kind:
            out.append({"kind": kind, "detail": f"{e['agent']}: {e['detail']}", "agent": e["agent"]})
    if inp.get("receipt_attached") is False and any("detention" in li.get("desc", "").lower()
                                                     for li in inp.get("line_items", [])):
        out.append({"kind": "missing_doc", "detail": "detention billed without receipt", "agent": "carrier_ap"})
    for fl in store.flags():
        if fl["case_id"] == case_id:
            out.append({"kind": "flag", "detail": fl["note"], "agent": fl["agent"] or "unknown"})
    return out


def repeated(agent: str, kind: str) -> int:
    """How many prior reviews saw this friction kind for this agent (drives instruction patches)."""
    n = 0
    for r in store.reviews():
        if any(fr.get("kind") == kind and fr.get("agent") == agent for fr in r["body"].get("friction", [])):
            n += 1
    return n
