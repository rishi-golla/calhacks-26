"""Answer-key evaluation of the Validator's deterministic review (agents/validator/tools.py).

For every planted simulator scenario: generate one load offline, ingest its raw events, export the trusted
source dataset, run the owning agent in-process against it (fixture mode, no network, no SpacetimeDB) and
review every finished financial artifact the run produced. The unmutated review measures false alarms; four
seeded mutations of the same artifact measure how often the Validator catches a wrong decision.

    .venv/bin/python -m harness.tests.validator_eval [--scenarios clean,rate_mismatch] [--miles 1500] [--json]

A mutation counts as caught only when the review turns ``issue`` because of a check that passed on the
unmutated artifact, so a baseline that already fails cannot hide a miss.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import os
import random
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agents.validator.tools import FINANCIAL_AGENTS, ISSUE, deterministic_review
from common.agent_support import identifier
from common.config import AGENTS, MAX_DELEGATION_DEPTH, SKILL_OWNER
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.policy import APPROVAL_THRESHOLD_USD
from common.runtime import DelegationRefused, TaskParked
from common.runtime_store import RuntimeStore, task_id_for
from common.source_repository import SourceRepository

# A 1,500-mile lane (contract $3,924.50) keeps every planted invoice under the $5,000 approval threshold and
# keeps the one-hour supported-detention decoy inside the 2% contract tolerance it was planted against
# (at 1,200 miles $75 of detention is 2.4% of the contract, so the decoy turns into a legitimate hold).
DEFAULT_MILES = 1500
T0 = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
SIM_SEED, LIFECYCLE_SEED = "7", 3
# Which skill the router sends each exported case to (harness/ingest/routing.py ROUTES).
CASE_SKILL = {"": "process_carrier_invoice", "-R": "process_carrier_invoice",
              "-BANK": "screen_bank_change", "-AR": "reconcile_customer_payment"}
# Answer-key expectations that mean "the correct decision is to pay or reconcile cleanly".
CLEAN = {"approve", "approve_quickpay", "approve_with_receipt", "approve_once", "reconciled"}
# The AR side of a load is untouched unless the answer key plants a shipper_ar defect.
IMPLIED_RECONCILED = "reconciled (implied)"
MUTATIONS = ("flip_decision", "shift_cent", "drop_evidence", "strip_approval")
# One monetary finding per agent that the review recomputes in exact cents.
MONEY_FIELD = {"carrier_ap": ("match", "invoice_amount"), "controller": ("hold_amount",),
               "shipper_ar": ("matched_amount",), "cfo": ("net_cash",)}
OFFLINE_ENV = {"MOCK_INTEGRATIONS": "0", "HARNESS_URL": "http://127.0.0.1:1", "SIM_SEED": SIM_SEED,
               "FREIGHTOPS_MODE": "fixture", "SPACETIME_DISABLED": "1",
               # Fixture calls never dial; the outreach guard still needs an allowlisted stand-in.
               "DEMO_ALLOWLIST_PHONES": "+15555550100"}
CLEARED_ENV = ("APIFY_TOKEN", "SOURCE_EXPORT_PATH", "HUMAN_APPROVER_ID", "A2A_DATABASE_URL")


def _agent_classes() -> dict[str, type]:
    from agents.carrier_ap.main import CarrierAPAgent
    from agents.cfo.main import CfoAgent
    from agents.controller.main import ControllerAgent
    from agents.documents.main import DocumentsAgent
    from agents.risk.main import RiskAgent
    from agents.shipper_ar.main import ShipperArAgent
    from agents.verification.main import VerificationAgent
    from agents.voice_desk.main import VoiceDeskAgent
    return {"carrier_ap": CarrierAPAgent, "shipper_ar": ShipperArAgent, "cfo": CfoAgent, "risk": RiskAgent,
            "controller": ControllerAgent, "verification": VerificationAgent, "voice_desk": VoiceDeskAgent,
            "documents": DocumentsAgent}


# --- offline world, load and source export ------------------------------------------------------------

def offline_world(miles: int = DEFAULT_MILES) -> dict:
    """The tests/test_ingest.py ``world`` fixture (real registry rows, no network) on a shorter lane."""
    from integrations import edgar, fmcsa
    from sim import geo
    from sim.world import price_lane
    from tests.test_w4_data import CARRIER_ROW, SEC_SUB
    carrier, shipper = fmcsa.normalize(CARRIER_ROW), edgar.normalize(SEC_SUB)
    other_carrier = fmcsa.normalize({**CARRIER_ROW, "dot_number": "391315", "legal_name": "SAGE VALLEY CARRIERS LLC",
                                     "dba_name": "SAGE VALLEY", "phone": "(323) 555-1315",
                                     "email_address": "billing@sagevalley.example", "docket1": "231686"})
    consignee = {**shipper, "id": "S-99", "cik": 99, "name": "CONSIGNEE DC", "lat": 34.0, "lon": -118.2}
    carrier.update(lat=33.95, lon=-118.17, terms="Net 30")
    shipper.update(lat=33.76, lon=-84.39, terms="Net 30",
                   directory={"phone": shipper["phone"], "source": shipper["source"]})
    return {"fsc_per_mile": 0.433, "carriers": [carrier, other_carrier], "shippers": [shipper, consignee],
            "lanes": [{"id": "LN-1", "shipper_id": shipper["id"], "carrier_id": carrier["id"],
                       "consignee_id": consignee["id"], "origin": "Atlanta, GA", "destination": "South Gate, CA",
                       **price_lane(miles, random.Random(1)), "equipment": "53' Reefer",
                       "commodity": "Packaged food, palletized", "nmfc_class": 70, "temp_f": 34,
                       "free_time_min": 120, "detention_rate_hr": 75.0,
                       "transit_hours": round(geo.transit_hours(miles), 1),
                       "accessorials": {"lumper": "at cost", "layover": 250.0, "tonu": 150.0, "stop_off": 75.0}}]}


@contextlib.contextmanager
def offline_env(root: Path):
    """Point the simulator, ingest and agents at ``root``; restore the caller's environment afterwards."""
    from common import harness_client
    values = {**OFFLINE_ENV, "WORLD_FILE": str(root / "world.json"), "SIM_DATA_DIR": str(root),
              "OPS_DB": str(root / "ops.db"), "SOURCE_DATA_PATH": str(root / "source.json")}
    saved = {key: os.environ.get(key) for key in (*values, *CLEARED_ENV)}
    saved_url = harness_client.HARNESS_URL
    os.environ.update(values)
    for key in CLEARED_ENV:
        os.environ.pop(key, None)
    harness_client.HARNESS_URL = values["HARNESS_URL"]
    try:
        yield
    finally:
        harness_client.HARNESS_URL = saved_url
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def generate(world: dict, scenario: str) -> tuple[dict, list[dict]]:
    """One load and its raw arrivals, as tests/test_ingest.py ``_run`` does, with a scenario-fixed load ID."""
    from sim import generator
    load_id = "LD-1024-" + hashlib.sha256(scenario.encode()).hexdigest()[:8].upper()
    with mock.patch.object(generator, "_id", lambda _prefix: load_id):
        load = generator.new_load(world, random.Random(LIFECYCLE_SEED))
    steps = generator.lifecycle(load, scenario, random.Random(LIFECYCLE_SEED), t0=T0)
    return load, [event for step in steps for event in step.make()]


def export(events: list[dict]) -> dict:
    """Ingest the arrivals and export the trusted source dataset (tests/test_source_export.py ``_export``)."""
    from ingest import source_export
    from ingest.main import connect, handle

    async def ingest(db):
        for event in events:
            await handle(event, db)

    db = connect()
    try:
        asyncio.run(ingest(db))
        db.commit()
        return source_export.build(db)
    finally:
        db.close()


# --- in-process agent mesh -----------------------------------------------------------------------------

class Mesh:
    """Agents for one case, each with its own in-memory runtime store, delegating in-process."""

    def __init__(self):
        self.classes = _agent_classes()
        self.agents: dict = {}
        self.trail: list[dict] = []  # every delegated child run

    def agent(self, name: str):
        if name not in self.agents:
            agent = self.classes[name](store=RuntimeStore("sqlite+aiosqlite:///:memory:"))
            agent.delegate = self._delegate_for(agent)
            self.agents[name] = agent
        return self.agents[name]

    def _delegate_for(self, parent):
        async def delegate(skill: str, env: TaskEnvelope, meta: DelegationMeta, parent_task_id: str,
                           context_id: str | None = None, *, mode: str = "await",
                           invocation_key: str | None = None) -> list[dict]:
            # The same guards FreightAgent.delegate enforces before sending to a peer.
            target = SKILL_OWNER.get(skill)
            if target is None or target not in AGENTS[parent.name].may_delegate_to:
                raise DelegationRefused(f"{parent.name} may not delegate skill {skill}")
            visited = [*meta.visited, parent.name]
            if target in visited:
                raise DelegationRefused("delegation cycle")
            if meta.delegation_depth + 1 > MAX_DELEGATION_DEPTH:
                raise DelegationRefused("max delegation depth")
            if env.task_type != skill or meta.case_id != env.case_id:
                raise DelegationRefused("delegation skill or case mismatch")
            key = f"{parent_task_id}:{invocation_key or skill}"
            child_meta = DelegationMeta(case_id=env.case_id, from_agent=parent.name, parent_task_id=parent_task_id,
                                        delegation_depth=meta.delegation_depth + 1, visited=visited,
                                        idempotency_key=key, priority=meta.priority,
                                        correlation_id=meta.correlation_id or env.case_id,
                                        causation_id=parent_task_id)
            child_id = task_id_for(target, parent.name, env.case_id, key)
            row = {"depth": child_meta.delegation_depth, "agent": target, "skill": skill, "task_id": child_id,
                   "status": "running"}
            self.trail.append(row)  # in call order, so a child is listed under its parent
            try:
                result = await self.agent(target).run_workflow(env, child_meta, child_id, None)
            except TaskParked as wait:
                row.update(status="parked", detail=wait.business_status)
                raise
            except Exception as error:
                row.update(status="agent_error", detail=type(error).__name__)
                raise
            artifact = ResultArtifact.model_validate(result).model_dump()
            row.update(status="completed", artifact=artifact)
            return [artifact]
        return delegate

    async def close(self) -> None:
        for agent in self.agents.values():
            await agent.store.close()


async def run_case(case_id: str, skill: str) -> dict:
    """Run the case's owning agent; parked waits and agent failures are outcomes, not harness errors."""
    owner = SKILL_OWNER[skill]
    task_id = task_id_for(owner, "router", case_id, "validator-eval")
    env = TaskEnvelope(task_type=skill, case_id=case_id)
    mesh = Mesh()
    root = {"depth": 0, "agent": owner, "skill": skill, "task_id": task_id}
    try:
        result = await mesh.agent(owner).run_workflow(env, DelegationMeta(case_id=case_id, from_agent="router"),
                                                      task_id, None)
        root.update(status="completed", artifact=ResultArtifact.model_validate(result).model_dump())
    except TaskParked as wait:
        root.update(status="parked", detail=f"{wait.business_status}/{wait.wait_kind}")
    except Exception as error:  # noqa: BLE001 -- an agent failure is a measured outcome
        root.update(status="agent_error", detail=type(error).__name__)
    finally:
        await mesh.close()
    # The source the review worker hands the Validator: the same repository load the agents made.
    source = await SourceRepository().load(env)
    return {"root": root, "children": mesh.trail, "source": source}


# --- review and mutations ------------------------------------------------------------------------------

def bundle(row: dict, case_id: str, source: dict, artifact: dict) -> dict:
    return {"target": {"task_id": row["task_id"], "agent": row["agent"], "skill": row["skill"],
                       "case_id": case_id, "a2a_state": "completed"},
            "source": copy.deepcopy(source), "artifact": artifact, "history": [], "flag": None, "simulated": True}


def failed_checks(review) -> list[str]:
    return [item["name"] for item in review.checks if not item["ok"] and item.get("severity", ISSUE) == ISSUE]


def _cents(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _invoice(source: dict) -> Decimal:
    return Decimal(str(source.get("invoice_amount") or 0))


def flip_decision(row: dict, artifact: dict, source: dict) -> dict | None:
    """Pay what should be withheld, withhold what should be paid (approve<->hold and its siblings)."""
    from agents.carrier_ap.tools import three_way_match
    agent, skill, outcome = row["agent"], row["skill"], artifact["outcome"]
    mutated = copy.deepcopy(artifact)
    findings = mutated["findings"]
    if agent == "carrier_ap" and skill in {"process_carrier_invoice", "propose_payment"}:
        if outcome == "approve":
            mutated["outcome"] = "hold"
            findings["payment_blocked"] = True
            findings.pop("payment_proposal", None)
            return mutated
        if outcome in {"hold", "reject", "unverified"}:
            if "match" not in findings:
                findings["match"] = three_way_match(source)
            amount = _invoice(source) - Decimal(str(source.get("paid_amount", 0)))
            mutated["outcome"] = "approve"
            findings["payment_blocked"] = False
            # Shaped like tests/test_validator_decisions.py approve(): an explicit fixture proposal.
            findings["payment_proposal"] = {"executed": False, "simulated": True, "proposed_payload": {
                "id": identifier(row["task_id"], "payment_proposal"), "invoice_id": source.get("invoice_id"),
                "amount": float(amount)}}
            return mutated
        return None
    if agent == "controller" and skill == "review_hold":
        if outcome == "hold":
            mutated.update(outcome="clear", findings={"hold_required": False, "simulated": True})
            return mutated
        if outcome == "clear":
            amount = float(_invoice(source))
            mutated["outcome"] = "hold"
            findings.update(hold_amount=amount, payment_blocked=True, hold={
                "executed": False, "simulated": True, "proposed_payload": {
                    "id": identifier(row["task_id"], "hold"), "invoice_id": source.get("invoice_id"),
                    "amount": amount}})
            return mutated
        return None
    if agent == "shipper_ar" and skill in {"reconcile_customer_payment", "chase_overdue"}:
        flips = {"verified": "partial", "partial": "verified", "suspicious": "verified", "hold": "verified"}
        if outcome in flips:
            mutated["outcome"] = flips[outcome]
            return mutated
    return None


def shift_cent(row: dict, artifact: dict, source: dict) -> dict | None:
    """Move one recomputable monetary finding by a single cent."""
    path = MONEY_FIELD.get(row["agent"])
    mutated = copy.deepcopy(artifact)
    holder = mutated["findings"]
    for key in (path or ())[:-1]:
        holder = holder.get(key) if isinstance(holder, dict) else None
    if not path or not isinstance(holder, dict) or not isinstance(holder.get(path[-1]), int | float):
        return None
    holder[path[-1]] = _cents(Decimal(str(holder[path[-1]])) + Decimal("0.01"))
    return mutated


def drop_evidence(row: dict, artifact: dict, source: dict) -> dict | None:
    mutated = copy.deepcopy(artifact)
    mutated["evidence_refs"] = []
    return mutated


def strip_approval(row: dict, artifact: dict, source: dict) -> dict | None:
    """Above the threshold, a payment proposal without its human approval reference."""
    if row["agent"] != "carrier_ap" or _invoice(source) <= APPROVAL_THRESHOLD_USD:
        return None
    mutated = copy.deepcopy(artifact)
    proposal = mutated["findings"].get("payment_proposal")
    payload = proposal.get("proposed_payload") if isinstance(proposal, dict) else None
    if not isinstance(payload, dict) or not payload.get("approval_id"):
        return None
    payload.pop("approval_id")
    return mutated


MUTATORS = {"flip_decision": flip_decision, "shift_cent": shift_cent, "drop_evidence": drop_evidence,
            "strip_approval": strip_approval}


def review_row(row: dict, case_id: str, source: dict) -> dict:
    artifact = row["artifact"]
    base = deterministic_review(bundle(row, case_id, source, artifact))
    base_failed = failed_checks(base)
    mutations = {}
    # Outside the financial scope every review is an issue, so a mutation there would prove nothing.
    for name, mutate in MUTATORS.items() if row["agent"] in FINANCIAL_AGENTS else ():
        mutated = mutate(row, artifact, source)
        if mutated is None:
            mutations[name] = {"applicable": False}
            continue
        review = deterministic_review(bundle(row, case_id, source, mutated))
        failed = failed_checks(review)
        new = [check for check in failed if check not in base_failed]
        mutations[name] = {"applicable": True, "verdict": review.verdict, "new_failures": new,
                           "caught": review.verdict == "issue" and bool(new)}
    return {"verdict": base.verdict, "failed_checks": base_failed, "mutations": mutations}


# --- evaluation ----------------------------------------------------------------------------------------

def _expectation(key: dict, agent: str, suffix: str) -> str:
    if agent in key:
        return key[agent]
    if suffix == "-AR" and agent == "shipper_ar":
        return IMPLIED_RECONCILED
    return "-"


def evaluate_scenario(scenario: str, root: Path, miles: int = DEFAULT_MILES) -> dict:
    from ingest import source_export
    from sim import generator
    root.mkdir(parents=True, exist_ok=True)
    with offline_env(root):
        world = offline_world(miles)
        (root / "world.json").write_text(json.dumps(world))
        load, events = generate(world, scenario)
        dataset = export(events)
        source_export.write(dataset, root / "source.json")
        key = generator.expected(load, scenario)
        load_case = load["case_id"]
        cases = sorted(case_id for case_id in dataset["cases"] if case_id.startswith(load_case))
        rows = []
        for case_id in cases:
            suffix = case_id.removeprefix(load_case)
            skill = CASE_SKILL.get(suffix)
            if skill is None:
                rows.append({"scenario": scenario, "case_id": case_id, "case": f"CASE{suffix}", "depth": 0,
                             "status": "unrouted", "detail": "no router skill for this case suffix"})
                continue
            run = asyncio.run(run_case(case_id, skill))
            source = run["source"]
            expectation = _expectation(key, run["root"]["agent"], suffix)
            eligible = expectation in CLEAN or expectation == IMPLIED_RECONCILED
            for item in [run["root"], *run["children"]]:
                if item["depth"] and (item["status"] != "completed" or item["agent"] not in FINANCIAL_AGENTS):
                    continue  # only finished financial children are reviewable on their own
                row = {"scenario": scenario, "case_id": case_id, "case": f"CASE{suffix}", "depth": item["depth"],
                       "agent": item["agent"], "skill": item["skill"], "status": item["status"],
                       "detail": item.get("detail"), "outcome": (item.get("artifact") or {}).get("outcome"),
                       "expectation": expectation if not item["depth"] else _expectation(key, item["agent"], ""),
                       "invoice_amount": source.get("invoice_amount"),
                       "over_threshold": float(_invoice(source)) > APPROVAL_THRESHOLD_USD,
                       "in_scope": item["agent"] in FINANCIAL_AGENTS, "false_alarm_eligible": False}
                if item["status"] == "completed":
                    row.update(review_row(item, case_id, source))
                    row["false_alarm_eligible"] = eligible and row["in_scope"]
                    row["false_alarm"] = row["false_alarm_eligible"] and row["verdict"] == "issue"
                rows.append(row)
    return {"scenario": scenario, "load_id": load["load_id"], "expected": key, "rows": rows}


def summarize(scenarios: list[dict]) -> dict:
    reviewed = [row for item in scenarios for row in item["rows"] if row.get("in_scope") and "verdict" in row]
    caught = applicable = 0
    by_mutation = {name: [0, 0] for name in MUTATIONS}
    by_scenario: dict[str, dict[str, list[int]]] = {}
    for row in reviewed:
        per = by_scenario.setdefault(row["scenario"], {name: [0, 0] for name in MUTATIONS})
        for name, result in row["mutations"].items():
            if result["applicable"]:
                hit = int(result["caught"])
                caught, applicable = caught + hit, applicable + 1
                for tally in (by_mutation[name], per[name]):
                    tally[0] += hit
                    tally[1] += 1
    eligible = [row for row in reviewed if row["false_alarm_eligible"]]
    alarms = [row for row in eligible if row["false_alarm"]]
    misses = [{"scenario": row["scenario"], "case": row["case"], "agent": row["agent"], "skill": row["skill"],
               "mutation": name, "verdict": result["verdict"]}
              for row in reviewed for name, result in row["mutations"].items()
              if result["applicable"] and not result["caught"]]
    over = sorted({item["scenario"] for item in scenarios for row in item["rows"] if row.get("over_threshold")})
    statuses: dict[str, int] = {}
    for item in scenarios:
        for row in item["rows"]:
            if not row["depth"]:
                statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    return {"reviewed": len(reviewed), "mutations_caught": caught, "mutations_applicable": applicable,
            "catch_rate": caught / applicable if applicable else None,
            "false_alarm_eligible": len(eligible), "false_alarms": len(alarms),
            "false_alarm_rate": len(alarms) / len(eligible) if eligible else None,
            "false_alarm_cases": [{"scenario": row["scenario"], "case": row["case"], "agent": row["agent"],
                                   "failed_checks": row["failed_checks"]} for row in alarms],
            "missed_mutations": misses, "by_mutation": by_mutation, "by_scenario": by_scenario,
            "case_status": statuses, "over_threshold_scenarios": over}


def evaluate(scenarios: list[str] | None = None, *, workdir: str | Path | None = None,
             miles: int = DEFAULT_MILES) -> dict:
    """Evaluate the Validator against each scenario's answer key. Deterministic for a given input."""
    from sim.generator import SCENARIOS
    names = list(scenarios or SCENARIOS)
    unknown = sorted(set(names) - SCENARIOS.keys())
    if unknown:
        raise ValueError(f"unknown scenarios: {unknown}")
    with contextlib.ExitStack() as stack:
        base = Path(workdir) if workdir else Path(stack.enter_context(tempfile.TemporaryDirectory()))
        results = [evaluate_scenario(name, base / name, miles) for name in names]
    return {"miles": miles, "scenarios": results, "summary": summarize(results)}


# --- report --------------------------------------------------------------------------------------------

COLUMNS = (("scenario", 31), ("case", 9), ("agent/skill", 38), ("outcome", 11), ("answer key", 38),
           ("verdict", 8), ("failed checks", 34), ("mut", 5))


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "~"


def _mutation_cell(row: dict) -> str:
    results = [result for result in row.get("mutations", {}).values() if result["applicable"]]
    return f"{sum(result['caught'] for result in results)}/{len(results)}" if results else "-"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def table(report: dict) -> str:
    lines = ["  ".join(name.ljust(width) for name, width in COLUMNS).rstrip(),
             "  ".join("-" * width for _name, width in COLUMNS)]
    for item in report["scenarios"]:
        for row in item["rows"]:
            indent = "  " * row["depth"] + ("> " if row["depth"] else "")
            role = f"{indent}{row.get('agent', '?')}/{row.get('skill', '?')}"
            if "verdict" in row:
                outcome, verdict = row["outcome"], row["verdict"] + ("" if row["in_scope"] else "*")
                checks = ",".join(row["failed_checks"]) or "-"
            else:
                outcome, verdict, checks = row["status"], "-", row.get("detail") or "-"
            cells = (item["scenario"], row["case"], role, outcome, row.get("expectation", "-"), verdict, checks,
                     _mutation_cell(row) if row.get("in_scope") else "-")
            lines.append("  ".join(_fit(str(cell), width).ljust(width) for cell, (_n, width) in zip(cells, COLUMNS))
                         .rstrip())
    summary = report["summary"]
    lines += ["", f"lane {report['miles']} mi; invoices over ${APPROVAL_THRESHOLD_USD:,}: "
              + (", ".join(summary["over_threshold_scenarios"]) or "none"),
              "case outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(summary["case_status"].items())),
              "per mutation: " + ", ".join(f"{name} {hit}/{total}"
                                          for name, (hit, total) in summary["by_mutation"].items()),
              (f"TOTAL  reviewed artifacts {summary['reviewed']}  |  catch rate {summary['mutations_caught']}/"
               f"{summary['mutations_applicable']} = {_rate(summary['catch_rate'])}  |  false-alarm rate "
               f"{summary['false_alarms']}/{summary['false_alarm_eligible']} = {_rate(summary['false_alarm_rate'])}"),
              "* = outside the Validator's financial scope (verdict shown, excluded from rates)"]
    for miss in summary["missed_mutations"]:
        lines.append(f"MISSED {miss['scenario']} {miss['case']} {miss['agent']}/{miss['skill']}: "
                     f"{miss['mutation']} -> {miss['verdict']}")
    for alarm in summary["false_alarm_cases"]:
        lines.append(f"FALSE ALARM {alarm['scenario']} {alarm['case']} {alarm['agent']}: "
                     + ",".join(alarm["failed_checks"]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenarios", help="comma-separated subset of sim.generator.SCENARIOS")
    parser.add_argument("--miles", type=int, default=DEFAULT_MILES, help="lane length; 2210 is the fixture lane")
    parser.add_argument("--json", action="store_true", help="print the full evaluate() result as JSON")
    args = parser.parse_args()
    report = evaluate(args.scenarios.split(",") if args.scenarios else None, miles=args.miles)
    print(json.dumps(report, indent=2, default=str) if args.json else table(report))


if __name__ == "__main__":
    main()
