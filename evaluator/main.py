"""W4 evaluator: score agent decisions against the simulator's answer key and publish SpacetimeDB `metric` rows.

PRD G6 / T10. The simulator appends one answer-key line per load to `$SIM_DATA_DIR/answer_key.jsonl` (scenario,
planted fields, expected outcome). This service joins those lines to what the agents actually did, as recorded in
SpacetimeDB, and writes a scoreboard with the `set_metric` reducer (role `evaluator`).

Deployment: the answer key exists only on the machine running the simulator (the ASUS), and the system of record is
Maincloud. Run the evaluator on the simulator's machine with the same `SIM_DATA_DIR`. It only makes outbound HTTPS
calls (SQL reads and `set_metric`) and needs `SPACETIME_DISABLED=0`, `SPACETIME_URL`, `SPACETIME_DB` and
`SPACETIME_TOKEN_EVALUATOR` (an identity granted the `evaluator` role). It opens no ports.

    python -m evaluator.main --once                 # one scoring pass, then exit
    python -m evaluator.main --loop 30              # rescore every 30 s
    python -m evaluator.main --once --dry-run       # read live rows, print, write nothing
    python -m evaluator.main --rows snapshot.json   # offline: score a table snapshot, print, write nothing
    python -m evaluator.main --once --dump-rows snapshot.json   # save the rows read for later offline runs

How a load is scored
--------------------
Cases join to a load by `case.load_id`, or by the resolved case-ID scheme ingest uses: `CASE-<load>` and `-R` are the
carrier settlement (lane `carrier_ap`), `-AR` the shipper receivable (`shipper_ar`), `-BANK` a bank-change screen
(`risk`). The lanes a load needs are the agent keys of its answer-key `expected` entry (`risk` alone only when no
`carrier_ap` decision is expected; otherwise Accessorial Audit runs as a child of Carrier Settlement).

A lane's decision is the latest terminal root task with that lane's first-hop skill, on any of the load's cases. A load is scored only when every
required lane has a decision and no task on any of its cases is still in flight (working, parked for a call or a
human, ...). Loads with no case in SpacetimeDB yet are "unseen"; the rest are "pending".

A load is flagged when any of its cases has a hold row, or a lane decision is `hold`, `reject`, `suspicious` or
`partial`, or `unverified` without an explicit clearance (`payment_blocked: false`) or a pending payment approval.
Planted-error scenarios are positives; clean loads and decoys are negatives. A duplicate invoice also counts as
caught when exactly one payment was proposed for the load (expected outcome `approve_once`).

Metrics (headline names match web/components/operations.tsx `Scoreboard`)
  precision            flagged positives / flagged loads
  recall               caught positives / scored positives
  dollars_caught       USD at risk on caught positives (planted delta, else case exposure, else invoice amount)
  false_holds          flagged negatives (count)
  median_resolution_s  median seconds from the load's first case opening to its last lane decision
  calls_placed         `call` rows past `queued` that were not cancelled
  f1, false_hold_rate, dollars_missed, true_positives, false_positives, false_negatives, true_negatives,
  loads_total, loads_scored, loads_pending, loads_unseen, coverage (scored / (scored + pending)),
  recall.<scenario>, positives.<scenario>, false_hold_rate.<scenario>, negatives.<scenario>,
  precision.<lane>, flags.<lane>
A ratio with a zero denominator is not written.

Write IDs are `evaluator:metric:<name>:<sha256(value|previous updated_at)[:16]>`. A metric whose stored value already
equals the new value is skipped, and a retry of an unconfirmed write reuses its write ID, so reruns are idempotent and
every real change still gets a fresh ID (a value that changes back is written again).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import pathlib
import statistics
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("evaluator")

TABLES = ("case", "task", "hold", "payment_proposal", "invoice", "call", "metric")
ERROR_SCENARIOS = frozenset({
    "detention_unsupported", "shipper_short_pay", "bank_change", "duplicate_invoice", "duplicate_renumbered",
    "rate_mismatch", "fsc_error", "math_error", "lumper_no_receipt", "factoring_unverified", "pod_unsigned",
    "double_brokering", "misapplied_payment",
})
# Look suspicious but are payable as billed (supported detention, a QuickPay discount, a clean twin).
DECOY_SCENARIOS = frozenset({"clean_decoy", "decoy_detention_supported_small", "decoy_quickpay",
                             "detention_supported"})
CLEAN_SCENARIOS = frozenset({"clean"})
SCENARIOS = ERROR_SCENARIOS | DECOY_SCENARIOS | CLEAN_SCENARIOS
# First-hop skills whose result is the lane's business decision.
LANE_SKILLS = {
    "carrier_ap": frozenset({"process_carrier_invoice", "propose_payment"}),
    "shipper_ar": frozenset({"reconcile_customer_payment", "chase_overdue"}),
    "risk": frozenset({"screen_bank_change", "screen_counterparty"}),
}
SKILL_LANES = {skill: lane for lane, skills in LANE_SKILLS.items() for skill in skills}
CASE_SUFFIXES = {"-AR": "shipper_ar", "-BANK": "risk", "-R": "carrier_ap"}
FLAG_OUTCOMES = frozenset({"hold", "reject", "suspicious", "partial"})
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "cancelled", "rejected"})
UNPLACED_CALLS = frozenset({"", "queued", "cancelled", "canceled"})


@dataclass
class Decision:
    lane: str
    task_id: str
    outcome: str
    flagged: bool
    at: str


@dataclass
class LoadScore:
    load_id: str
    scenario: str
    status: str  # "scored", "pending" or "unseen"
    positive: bool = False
    flagged: bool = False
    caught: bool = False
    flagged_lanes: set[str] = field(default_factory=set)
    at_risk_usd: float = 0.0
    resolution_s: float | None = None
    decisions: dict[str, Decision] = field(default_factory=dict)


# ----------------------------------------------------------------------------- inputs

def answer_key_path() -> pathlib.Path:
    return pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / "answer_key.jsonl"


def read_answer_key(path: pathlib.Path) -> list[dict]:
    """One entry per load (the last line wins); unreadable lines are skipped with a warning."""
    if not path.exists():
        raise FileNotFoundError(f"answer key not found: {path} (set SIM_DATA_DIR to the simulator's data dir)")
    answers: dict[str, dict] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.warning("answer key line %d is not JSON; skipped", number)
            continue
        if isinstance(entry, dict) and entry.get("load_id") and entry.get("scenario"):
            answers[str(entry["load_id"])] = entry
    return list(answers.values())


async def fetch_rows(client) -> dict[str, list[dict]]:
    """Snapshot every table the scorer reads. Table names are constants, never caller input."""
    return {table: await client.query_rows(f'SELECT * FROM "{table}"') for table in TABLES}


# ----------------------------------------------------------------------------- scoring

def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _when(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed and parsed.tzinfo is None else parsed


def _findings(task: Mapping) -> dict:
    raw = task.get("artifact_json")
    try:
        artifact = json.loads(raw) if isinstance(raw, str) and raw else raw
    except json.JSONDecodeError:
        return {}
    findings = artifact.get("findings") if isinstance(artifact, dict) else None
    return findings if isinstance(findings, dict) else {}


def lane_of(case_id: str) -> str:
    return next((lane for suffix, lane in CASE_SUFFIXES.items() if case_id.endswith(suffix)), "carrier_ap")


def base_case_id(case_id: str) -> str:
    return next((case_id.removesuffix(suffix) for suffix in CASE_SUFFIXES if case_id.endswith(suffix)), case_id)


def required_lanes(answer: Mapping) -> set[str]:
    expected = answer.get("expected") if isinstance(answer.get("expected"), dict) else {}
    lanes = {key for key in expected if key in LANE_SKILLS}
    if "carrier_ap" in lanes:
        lanes.discard("risk")  # Accessorial Audit is a child of Carrier Settlement here
    return lanes or {"carrier_ap"}


def classify_decision(task: Mapping) -> bool | None:
    """True when the outcome flags a problem, False when it lets the item through, None when it is no decision."""
    if _text(task.get("a2a_state")) != "completed":
        return False  # failed or cancelled: terminal, but it caught nothing
    outcome = _text(task.get("outcome"))
    if outcome in FLAG_OUTCOMES:
        return True
    if outcome == "unverified":
        findings = _findings(task)
        if findings.get("awaiting_invoice"):
            return None  # delivery evidence ahead of the bill; settlement has not happened yet
        return not (findings.get("payment_blocked") is False or "approval" in findings)
    return False


def _cases_by_load(answers: Iterable[Mapping], cases: Iterable[Mapping]) -> dict[str, list[dict]]:
    by_case_id = {str(a.get("case_id") or f"CASE-{a['load_id']}"): str(a["load_id"]) for a in answers}
    known = set(by_case_id.values())
    found: dict[str, list[dict]] = {}
    for case in cases:
        load_id = _text(case.get("load_id"))
        if load_id not in known:
            load_id = by_case_id.get(base_case_id(_text(case.get("case_id"))), "")
        if load_id:
            found.setdefault(load_id, []).append(dict(case))
    return found


def _group(rows: Iterable[Mapping], key: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(_text(row.get(key)), []).append(dict(row))
    return grouped


def at_risk_usd(answer: Mapping, cases: list[dict], invoices: list[dict], lanes: set[str]) -> float:
    """What the planted error would have cost: its planted delta, else the case exposure ingest computed on the
    error's lane, else the invoice amount (a duplicate, an unsigned POD or a double-brokered load risks all of it)."""
    planted = answer.get("planted") if isinstance(answer.get("planted"), dict) else {}
    scenario = answer.get("scenario")
    if scenario in ("rate_mismatch", "math_error") and planted.get("delta") is not None:
        return _number(planted["delta"])
    if scenario == "fsc_error" and planted.get("billed") is not None:
        return max(_number(planted["billed"]) - _number(planted.get("contract")), 0.0)
    if scenario == "lumper_no_receipt" and planted.get("billed") is not None:
        return _number(planted["billed"])
    exposure = max((_number(c.get("exposure_usd")) for c in cases if lane_of(_text(c.get("case_id"))) in lanes),
                   default=0.0)
    if exposure > 0:
        return exposure
    return max((_number(i.get("amount")) for i in invoices), default=0.0)


def score_load(answer: Mapping, cases: list[dict], tasks_by_case: Mapping[str, list[dict]],
               holds_by_case: Mapping[str, list[dict]], proposals_by_case: Mapping[str, list[dict]],
               invoices_by_case: Mapping[str, list[dict]]) -> LoadScore:
    scenario = str(answer["scenario"])
    result = LoadScore(load_id=str(answer["load_id"]), scenario=scenario, status="unseen",
                       positive=scenario in ERROR_SCENARIOS)
    if not cases:
        return result
    result.status = "pending"
    case_ids = [_text(c.get("case_id")) for c in cases]
    tasks = [t for case_id in case_ids for t in tasks_by_case.get(case_id, [])]
    if any(_text(t.get("a2a_state")) not in TERMINAL_STATES for t in tasks):
        return result  # something on this load is still in flight
    lanes = required_lanes(answer)
    for task in tasks:
        # The skill names the lane: a bank change whose load ingest could not resolve lands on CASE-<load>.
        lane = SKILL_LANES.get(_text(task.get("skill")))
        if lane is None or _text(task.get("parent_task_id")):
            continue
        flagged = classify_decision(task)
        if flagged is None:
            continue
        at = _text(task.get("updated_at")) or _text(task.get("started_at"))
        current = result.decisions.get(lane)
        if current is None or (at, _text(task.get("task_id"))) > (current.at, current.task_id):
            result.decisions[lane] = Decision(lane, _text(task.get("task_id")), _text(task.get("outcome")),
                                              flagged, at)
    result.flagged_lanes.update(lane_of(case_id) for case_id in case_ids if holds_by_case.get(case_id))
    if not lanes <= result.decisions.keys():
        return result
    result.status = "scored"
    result.flagged_lanes.update(lane for lane, decision in result.decisions.items() if decision.flagged)
    result.flagged = bool(result.flagged_lanes)
    proposals = sum(len(proposals_by_case.get(case_id, [])) for case_id in case_ids)
    result.caught = result.positive and (result.flagged or (scenario == "duplicate_invoice" and proposals == 1))
    invoices = [i for case_id in case_ids for i in invoices_by_case.get(case_id, [])]
    result.at_risk_usd = round(at_risk_usd(answer, cases, invoices, lanes), 2) if result.positive else 0.0
    opened = [when for when in (_when(c.get("opened_at")) for c in cases) if when]
    decided = [when for when in (_when(d.at) for d in result.decisions.values()) if when]
    if opened and decided:
        result.resolution_s = max((max(decided) - min(opened)).total_seconds(), 0.0)
    return result


def score(answers: list[dict], rows: Mapping[str, list[dict]]) -> list[LoadScore]:
    known = [a for a in answers if a["scenario"] in SCENARIOS]
    for unknown in sorted({a["scenario"] for a in answers} - SCENARIOS):
        log.warning("answer-key scenario %r is not classified; its loads are not scored", unknown)
    cases = _cases_by_load(known, rows.get("case", []))
    tasks, holds = _group(rows.get("task", []), "case_id"), _group(rows.get("hold", []), "case_id")
    proposals = _group(rows.get("payment_proposal", []), "case_id")
    invoices = _group(rows.get("invoice", []), "case_id")
    return [score_load(a, cases.get(str(a["load_id"]), []), tasks, holds, proposals, invoices) for a in known]


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def scoreboard(loads: list[LoadScore], rows: Mapping[str, list[dict]]) -> dict[str, float]:
    """Metric name -> value. Ratios with nothing to divide by are omitted rather than written as 0."""
    scored = [x for x in loads if x.status == "scored"]
    positives = [x for x in scored if x.positive]
    negatives = [x for x in scored if not x.positive]
    flagged = [x for x in scored if x.flagged]
    tp = sum(x.caught for x in positives)
    fp = sum(x.flagged for x in negatives)
    precision = _ratio(sum(x.flagged for x in positives), len(flagged))
    recall = _ratio(tp, len(positives))
    pending = sum(x.status == "pending" for x in loads)
    durations = [x.resolution_s for x in scored if x.resolution_s is not None]
    calls = sum(_text(c.get("status")) not in UNPLACED_CALLS for c in rows.get("call", []))
    metrics: dict[str, float | None] = {
        "precision": precision, "recall": recall,
        "dollars_caught": sum(x.at_risk_usd for x in positives if x.caught),
        "false_holds": fp,
        "median_resolution_s": statistics.median(durations) if durations else None,
        "calls_placed": calls,
        "f1": (2 * precision * recall / (precision + recall)) if precision and recall else None,
        "false_hold_rate": _ratio(fp, len(negatives)),
        "dollars_missed": sum(x.at_risk_usd for x in positives if not x.caught),
        "true_positives": tp, "false_positives": fp, "false_negatives": len(positives) - tp,
        "true_negatives": len(negatives) - fp,
        "loads_total": len(loads), "loads_scored": len(scored), "loads_pending": pending,
        "loads_unseen": sum(x.status == "unseen" for x in loads),
        "coverage": _ratio(len(scored), len(scored) + pending),
    }
    for scenario in sorted({x.scenario for x in positives}):
        group = [x for x in positives if x.scenario == scenario]
        metrics[f"positives.{scenario}"] = len(group)
        metrics[f"recall.{scenario}"] = _ratio(sum(x.caught for x in group), len(group))
    for scenario in sorted({x.scenario for x in negatives}):
        group = [x for x in negatives if x.scenario == scenario]
        metrics[f"negatives.{scenario}"] = len(group)
        metrics[f"false_hold_rate.{scenario}"] = _ratio(sum(x.flagged for x in group), len(group))
    for lane in LANE_SKILLS:
        group = [x for x in scored if lane in x.flagged_lanes]
        metrics[f"flags.{lane}"] = len(group)
        metrics[f"precision.{lane}"] = _ratio(sum(x.positive for x in group), len(group))
    return {name: round(float(value), 6) for name, value in metrics.items() if value is not None}


# ----------------------------------------------------------------------------- output

def write_id(name: str, value: float, previous_updated_at: str) -> str:
    digest = hashlib.sha256(f"{value!r}|{previous_updated_at}".encode()).hexdigest()
    return f"evaluator:metric:{name}:{digest[:16]}"


def changed_metrics(metrics: Mapping[str, float], stored: Iterable[Mapping]) -> list[dict]:
    """`set_metric` payloads for metrics whose stored value differs; unchanged metrics produce nothing."""
    current = {_text(row.get("name")): row for row in stored}
    payloads = []
    for name, value in metrics.items():
        row = current.get(name)
        if row is not None and math.isclose(_number(row.get("value")), value, rel_tol=0.0, abs_tol=1e-9):
            continue
        previous = _text(row.get("updated_at")) if row is not None else ""
        payloads.append({"name": name, "value": value, "write_id": write_id(name, value, previous)})
    return payloads


async def publish(client, metrics: Mapping[str, float], stored: Iterable[Mapping]) -> list[str]:
    written = []
    for payload in changed_metrics(metrics, stored):
        await client.mutate("set_metric", payload)
        written.append(payload["name"])
    return written


def render(metrics: Mapping[str, float]) -> str:
    width = max((len(name) for name in metrics), default=0)
    return "\n".join(f"{name:<{width}}  {value:g}" for name, value in metrics.items())


# ----------------------------------------------------------------------------- service

def _client():
    from common.spacetime_client import SpacetimeClient
    return SpacetimeClient(actor="evaluator")


async def run_pass(answer_path: pathlib.Path, *, client=None, rows: Mapping | None = None,
                   dry_run: bool = False, dump: pathlib.Path | None = None) -> tuple[dict[str, float], list[str]]:
    answers = read_answer_key(answer_path)
    if rows is None:
        rows = await fetch_rows(client)
    if dump is not None:
        dump.write_text(json.dumps(rows, indent=1, sort_keys=True, default=str))
    metrics = scoreboard(score(answers, rows), rows)
    if dry_run:
        return metrics, []
    return metrics, await publish(client, metrics, rows.get("metric", []))


async def main_async(args: argparse.Namespace) -> int:
    answer_path = args.answer_key or answer_key_path()
    if args.rows:
        rows = json.loads(args.rows.read_text())
        metrics, _ = await run_pass(answer_path, rows=rows, dry_run=True)
        print(json.dumps(metrics, indent=2) if args.json else render(metrics))
        return 0
    from common.spacetime_client import SpacetimeError
    client = _client()
    if client.disabled:
        print("evaluator: SpacetimeDB is disabled; set SPACETIME_DISABLED=0 (use --rows FILE to score offline)",
              file=sys.stderr)
        return 2
    if not args.dry_run and not (os.getenv("SPACETIME_TOKEN_EVALUATOR") or os.getenv("SPACETIME_TOKEN")):
        print("evaluator: SPACETIME_TOKEN_EVALUATOR is required to write metrics", file=sys.stderr)
        return 2
    while True:
        try:
            metrics, written = await run_pass(answer_path, client=client, dry_run=args.dry_run,
                                              dump=args.dump_rows)
        except (SpacetimeError, FileNotFoundError) as error:
            if args.loop is None:
                raise
            log.warning("scoring pass failed: %s", error)
        else:
            if args.loop is None:
                print(json.dumps(metrics, indent=2) if args.json else render(metrics))
                summary = "dry run, nothing written" if args.dry_run else f"{len(written)} metric(s) written"
                print(f"evaluator: {summary}", file=sys.stderr)
                return 0
            log.info("scored %d/%d loads (precision=%s recall=%s); wrote %d metric(s)",
                     metrics.get("loads_scored", 0), metrics.get("loads_total", 0), metrics.get("precision", "-"),
                     metrics.get("recall", "-"), len(written))
        await asyncio.sleep(args.loop)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score agent outcomes against the simulator answer key.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one scoring pass against SpacetimeDB (default)")
    mode.add_argument("--loop", type=float, metavar="SECONDS", help="rescore every SECONDS")
    mode.add_argument("--rows", type=pathlib.Path, metavar="FILE",
                      help="score a JSON table snapshot offline; implies --dry-run")
    parser.add_argument("--dry-run", action="store_true", help="print the scoreboard without writing metrics")
    parser.add_argument("--answer-key", type=pathlib.Path, help="default: $SIM_DATA_DIR/answer_key.jsonl")
    parser.add_argument("--dump-rows", type=pathlib.Path, metavar="FILE", help="save the rows read as JSON")
    parser.add_argument("--json", action="store_true", help="print metrics as JSON")
    args = parser.parse_args(argv)
    if args.loop is not None and not (math.isfinite(args.loop) and args.loop > 0):
        parser.error("--loop needs a positive number of seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
