"""CFO: cash position and impact from the live subledgers (light by design, PRD)."""
from collections import defaultdict

from agents.cfo.card import DESCRIPTIONS
from common import store
from common.envelope import ResultArtifact
from common.runtime import Case, FreightAgent
from common.server import serve


def _rows(sheet: str) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(dict)
    for c in store.sheet(sheet):
        out[c["row"]][c["col"]] = c["value"]
    return out


class CFOAgent(FreightAgent):
    name = "cfo"

    async def handle(self, case: Case) -> ResultArtifact:
        ap, ar = _rows("ap_subledger"), _rows("ar_subledger")
        num = lambda v: float(v or 0)
        ap_due = sum(num(r.get("approved")) for r in ap.values() if str(r.get("status", "")).startswith("approved"))
        disputed = sum(num(r.get("disputed")) for r in ap.values())
        on_hold = sum(num(r.get("invoiced")) for r in ap.values() if r.get("status") == "on_hold")
        ar_open = sum(num(r.get("open")) for r in ar.values())
        f = {"ap_due": ap_due, "ap_disputed_saved": disputed, "ap_on_hold": on_hold, "ar_open": ar_open,
             "net_position": round(ar_open - ap_due, 2)}
        if case.env.task_type == "cash_forecast":
            case.commit("update_forecast", [{"sheet": "cfo_forecast", "row": "week_1", "col": k, "after": v}
                                            for k, v in f.items()])
        return ResultArtifact(outcome="info", confidence=0.9, findings=f)


if __name__ == "__main__":
    serve(CFOAgent(), DESCRIPTIONS)
