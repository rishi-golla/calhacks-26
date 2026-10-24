"""Validator: reviews every terminal finance output, mines friction, writes memory + instruction versions.
It can never change policy code, limits, tool permissions or delegation allowlists (P-012)."""
from agents.validator.card import DESCRIPTIONS
from agents.validator.tools import POLICY_CHANGE_WORDS, checks, friction, repeated
from common import mirror, store
from common.envelope import Friction, Lesson, ResultArtifact, ReviewOutput
from common.policy import check
from common.runtime import Case, FreightAgent
from common.server import serve

LESSONS = {
    "missing_doc": ("carrier_ap", ("Detention billed without a receipt: request the receipt from the carrier "
                                   "before routing to Risk.")),
    "policy_deny": (None, "A policy denied an action in this case; check policy preconditions before acting."),
    "timeout": (None, "A callback timed out; re-check parked tasks older than the deadline."),
    "human_override": (None, "A human overrode this agent; cite the override reason in similar cases."),
}


class ValidatorAgent(FreightAgent):
    name = "validator"

    async def handle(self, case: Case) -> ResultArtifact:
        if case.env.task_type == "review_flag":
            return self._flag(case)
        s = case.subject
        agent, inp, result = s["agent"], s.get("input", {}), s.get("result", {})
        cks = checks(agent, s.get("task_id", ""), inp, result)
        fr = friction(case.env.case_id, inp)
        lessons: list[Lesson] = []
        for kind in sorted({f["kind"] for f in fr}):
            target, text = LESSONS.get(kind, (None, None))
            if not text:
                continue
            who = target or next((f["agent"] for f in fr if f["kind"] == kind), agent)
            scope = {"carrier_id": inp.get("carrier", {}).get("id")} if inp.get("carrier") else {}
            lessons.append(self._memory(case, who, text, scope))
            if repeated(who, kind) >= 1:
                lessons.append(self._instruction(case, who, text, kind))
        rv = result.get("findings", {}).get("risk", {})
        if rv.get("outcome") == "unverified" and inp.get("carrier"):
            c = inp["carrier"]
            lessons.append(self._memory(case, "risk", f"Carrier {c.get('name')} ({c.get('id')}) billed detention "
                                        f"not supported by GPS on {case.env.case_id}; verify future claims.",
                                        {"carrier_id": c.get("id")}))
        verdict = "pass" if all(c["ok"] for c in cks) else "issue"
        out = ReviewOutput(verdict=verdict, checks=cks, friction=[Friction(kind=f["kind"], detail=f["detail"])
                                                                 for f in fr],
                           lessons=lessons, actions=["none"] if verdict == "pass" else ["reopen_case"])
        body = {**out.model_dump(), "friction": fr, "reviewed_task": s.get("task_id"), "reviewed_agent": agent}
        rid = store.add_review(case.env.case_id, agent, s.get("task_id"), verdict, body)
        mirror.emit("record_review", rid, body)
        return ResultArtifact(outcome="pass" if verdict == "pass" else "issue", confidence=1.0,
                              findings={"review_id": rid, **out.model_dump()})

    def _memory(self, case: Case, agent: str, text: str, scope: dict) -> Lesson:
        mid = store.add_memory(agent, text, scope, source=case.task_id)
        case.commit("write_memory", [{"sheet": "agent_memory", "row": mid, "col": "text", "after": text},
                                     {"sheet": "agent_memory", "row": mid, "col": "agent", "after": agent}])
        return Lesson(agent=agent, type="memory", scope=scope, text_or_diff=text, confidence=0.8)

    def _instruction(self, case: Case, agent: str, text: str, reason: str) -> Lesson:
        v = store.add_instruction_patch(agent, text, author="validator", reason=reason)
        case.commit("patch_instructions", [{"sheet": "instructions", "row": f"{agent}:v{v}", "col": "patch",
                                            "after": text}])
        return Lesson(agent=agent, type="instruction", text_or_diff=f"+ {text}", confidence=0.7)

    def _flag(self, case: Case) -> ResultArtifact:
        s = case.subject
        note, agent = s.get("note", ""), s.get("agent") or "carrier_ap"
        if any(w in note.lower() for w in POLICY_CHANGE_WORDS):
            d = check("change_policy", self.name, s)
            store.set_flag_status(s["flag_id"], "needs_policy_owner")
            return ResultArtifact(outcome="issue", policy_ids=d.policy_ids,
                                  findings={"reason": d.reason, "flag_id": s["flag_id"]})
        lessons = [self._memory(case, agent, f"Human flag on {s.get('target_type')} {s.get('target_id')}: {note}",
                                {"case_id": case.env.case_id}),
                   self._instruction(case, agent, f"Human feedback: {note}", "flag")]
        store.set_flag_status(s["flag_id"], "addressed")
        return ResultArtifact(outcome="pass", confidence=1.0,
                              findings={"flag_id": s["flag_id"], "lessons": [x.model_dump() for x in lessons]})


if __name__ == "__main__":
    serve(ValidatorAgent(), DESCRIPTIONS)
