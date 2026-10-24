"""Risk & Compliance: accessorial investigations, bank-change and counterparty screening."""
from agents.risk.card import DESCRIPTIONS
from agents.risk.tools import gps_dwell_minutes
from common.envelope import ResultArtifact
from common.runtime import Case, FreightAgent, Park, child_result
from common.server import serve
from integrations.tools import crs_business_credit, crs_ofac, fmcsa_lookup, score_anomaly


class RiskAgent(FreightAgent):
    name = "risk"

    async def handle(self, case: Case) -> ResultArtifact | Park:
        t, s = case.env.task_type, case.subject
        if t == "screen_counterparty":
            c = s.get("carrier") or s.get("counterparty") or {}
            f = {"fmcsa": fmcsa_lookup(c.get("usdot")), "ofac": crs_ofac(c.get("name", "")),
                 "credit": crs_business_credit(c.get("name", ""), c.get("state", ""))}
            bad = f["ofac"].get("hit") or f["fmcsa"].get("authority_status") not in (None, "ACTIVE")
            return ResultArtifact(outcome="suspicious" if bad else "clear", confidence=0.8, findings=f)
        if t == "screen_bank_change":
            ofac = crs_ofac(s.get("carrier", {}).get("name", ""))
            d = await case.delegate("verify_bank_change", subject={**s, "claim": "bank account change"})
            if d.parked:
                return Park("awaiting_other_agent", "verification", {"kind": "bank", "ofac": ofac})
            return self._bank_verdict(ofac, d.result)
        dwell, free = gps_dwell_minutes(s), s.get("free_time_min", 120)
        claimed = s.get("detention_hours_claimed", 0)
        anomaly = score_anomaly(s)
        suspicious = dwell is not None and claimed > 0 and dwell <= free
        base = {"gps_dwell_min": dwell, "free_time_min": free, "detention_hours_claimed": claimed,
                "receipt_attached": s.get("receipt_attached"), "anomaly": anomaly, "needs_verification": suspicious}
        if not suspicious:
            return ResultArtifact(outcome="clear", confidence=0.6, findings=base)
        await case.status("awaiting_other_agent", waiting_on="verification")
        d = await case.delegate("verify_claim", subject={
            "claim": f"{claimed}h detention on {s.get('load_id')}", "claim_type": "detention",
            "carrier": s.get("carrier", {}), "load_id": s.get("load_id"), "facts": base,
            "channel": s.get("verification_channel", "call"), "demo_target": s.get("demo_target"),
            "questions": [f"Our GPS shows {dwell} minutes on site. How long was the driver detained?",
                          "Do you have a detention receipt or signed in/out times?"]})
        if d.parked:
            return Park("awaiting_other_agent", "verification", {"kind": "detention", "base": base})
        return self._detention_verdict(base, d.result)

    async def on_resume(self, case: Case) -> ResultArtifact:
        if case.state["kind"] == "bank":
            return self._bank_verdict(case.state["ofac"], child_result(case))
        return self._detention_verdict(case.state["base"], child_result(case))

    def _detention_verdict(self, base: dict, ver: dict) -> ResultArtifact:
        vf = ver.get("findings", {})
        confirmed, denied = bool(vf.get("claim_confirmed")), bool(vf.get("claim_denied")) and not vf.get("claim_confirmed")
        outcome = "verified" if confirmed else "unverified" if denied else "inconclusive"
        return ResultArtifact(outcome=outcome, confidence=0.9,
                              findings={**base, "verification": ver, "claim_confirmed": confirmed,
                                        "claim_denied": denied},
                              evidence_refs=ver.get("evidence_refs", []),
                              recommended_next=[] if confirmed else ["carrier_ap.short_pay_detention"] if denied
                              else ["carrier_ap.keep_hold"])

    def _bank_verdict(self, ofac: dict, ver: dict) -> ResultArtifact:
        ok = bool(ver.get("findings", {}).get("claim_confirmed")) and not ofac.get("hit")
        return ResultArtifact(outcome="clear" if ok else "suspicious", confidence=0.85,
                              findings={"ofac": ofac, "verification": ver, "block_change": not ok})


if __name__ == "__main__":
    serve(RiskAgent(), DESCRIPTIONS)
