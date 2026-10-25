"""W4 evaluator: score agent decisions against the simulator's answer key and publish SpacetimeDB `metric` rows.

PRD G6 / T10. The simulator appends one answer-key line per load to `$SIM_DATA_DIR/answer_key.jsonl` (scenario,
planted fields, expected outcome, and `ts`, the load's business start time). This service joins those lines to what
the agents actually did, as recorded in SpacetimeDB, and writes a scoreboard with the `set_metric` reducer (role
`evaluator`).

Deployment: the answer key exists only on the machine running the simulator (the ASUS), and the system of record is
Maincloud. Run the evaluator on the simulator's machine with the same `SIM_DATA_DIR` (and `REDIS_URL`, for the
simulator's business clock). It only makes outbound calls (SQL reads and `set_metric` to SpacetimeDB, one GET of the
clock key from Redis) and needs `SPACETIME_DISABLED=0`, `SPACETIME_URL`, `SPACETIME_DB` and
`SPACETIME_TOKEN_EVALUATOR` (an identity granted the `evaluator` role). It opens no ports.

    python -m evaluator.main --once                 # one scoring pass, then exit
    python -m evaluator.main --loop 30              # rescore every 30 s
    python -m evaluator.main --once --dry-run       # read live rows, print, write nothing
    python -m evaluator.main --rows snapshot.json   # offline: score a table snapshot, print, write nothing
    python -m evaluator.main --once --dump-rows snapshot.json   # save the rows read for later offline runs

How a load is scored
--------------------
Cases join to a load by `case.load_id`, or by the resolved case-ID scheme ingest uses (`CASE-<load>`, `-R`, `-AR`,
`-BANK`). The lanes a load needs are the agent keys of its answer-key `expected` entry: `carrier_ap` (Carrier
Settlement), `shipper_ar` (receivables) and `risk` (bank-change screening; only when no `carrier_ap` decision is
expected, otherwise Accessorial Audit runs as a child of Carrier Settlement). A lane's decision is the latest
top-level task with the lane's first-hop skill, on any of the load's cases, that either completed or is parked
(`input-required`/`auth-required` or an `awaiting_*` status). A parked task blocks payment: it counts as flagged and
as escalated. Failed or cancelled tasks never decide a lane; a lane with only those is "failed".

A load is pending while work on it is in flight (a task submitted or working; parked descendants are ignored) or a
required lane is undecided. Duplicate scenarios also stay pending until their second invoice is seen or the whole
lifecycle has been emitted. Once a load has expired (the business clock is past `ts` + EVALUATOR_LIFECYCLE_DAYS, so
every step has been emitted, and nothing on it changed for EVALUATOR_GRACE_S wall seconds) it is scored as it is:
a positive with an undecided lane counts as not caught; a negative that nothing flagged is not counted as a true
negative but as "failed" (a lane failed) or "unresolved". Loads with no case in SpacetimeDB are "unseen".

"Start fresh": when the public `view_epoch` row exists, only loads whose earliest case `opened_at` is at or after
its `since` are scored (loads with no case yet are left out until one opens), and `calls_placed` counts only calls
started since then. Every metric is still written on every pass; nothing is deleted.

A load is flagged when any of its cases has a hold row, or a lane decision is parked, `hold`, `reject`, `suspicious`
or `partial`, or `unverified` without an explicit clearance (`payment_blocked: false`) or a pending payment approval.
Planted-error scenarios are positives; clean loads and decoys are negatives. A duplicate invoice also counts as caught
when exactly one payment was proposed for the load (expected outcome `approve_once`).

Metrics (headline names match web/components/operations.tsx `Scoreboard`)
  precision            flagged positives / flagged loads
  recall               caught positives / scored positives
  dollars_caught       USD at risk on caught positives (planted delta, else case exposure, else invoice amount)
  false_holds          flagged negatives (count)
  median_resolution_s  median agent time of completed top-level lane tasks (started_at -> final updated_at)
  calls_placed         `call` rows past `queued` that were not cancelled
  f1, false_hold_rate, dollars_missed, true_positives, false_positives, false_negatives, true_negatives, escalated,
  loads_total, loads_scored, loads_pending, loads_unseen, loads_failed, loads_unresolved,
  coverage (scored / (scored + pending + failed + unresolved)),
  recall.<scenario>, positives.<scenario>, false_hold_rate.<scenario>, negatives.<scenario>,
  precision.<lane>, flags.<lane>
Every name is written on every pass, so no figure goes stale: a ratio with nothing to divide by is written as -1,
which the Scoreboard shows as "—", and per-scenario counts that drop out are written as 0.

Write IDs are `evaluator:metric:<name>:<sha256(value|previous updated_at)[:16]>`. A metric whose stored value already
equals the new value is skipped (as is a change under 1% of `coverage` or `median_resolution_s`, or under 1 s of the
latter), and a retry of an unconfirmed write reuses its write ID, so reruns are idempotent and every real change
still gets a fresh ID.
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
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger("evaluator")

TABLES = ("case", "task", "hold", "payment_proposal", "invoice", "call", "metric")
# Read separately so a module published before the table existed still scores (as "no epoch").
EPOCH_TABLE = "view_epoch"
ERROR_SCENARIOS = frozenset({
    "detention_unsupported", "shipper_short_pay", "bank_change", "duplicate_invoice", "duplicate_renumbered",
    "rate_mismatch", "fsc_error", "math_error", "lumper_no_receipt", "factoring_unverified", "pod_unsigned",
    "double_brokering", "misapplied_payment",
})
# Look suspicious but are payable as billed (supported detention, a QuickPay discount, a clean twin).
DECOY_SCENARIOS = frozenset({"clean_decoy", "decoy_detention_supported_small", "decoy_quickpay",
                             "detention_supported"})
CLEAN_SCENARIOS = frozenset({"clean"})
NEGATIVE_SCENARIOS = DECOY_SCENARIOS | CLEAN_SCENARIOS
SCENARIOS = ERROR_SCENARIOS | NEGATIVE_SCENARIOS
DUPLICATE_SCENARIOS = frozenset({"duplicate_invoice", "duplicate_renumbered"})
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
PARKED_STATES = frozenset({"input-required", "auth-required"})
UNPLACED_CALLS = frozenset({"", "queued", "cancelled", "canceled"})
UNDEFINED = -1.0  # written for a ratio with nothing to divide by; the Scoreboard renders negatives as "—"
# Volatile figures: a change smaller than these is not worth a change set (relative, absolute).
TOLERANCE = {"median_resolution_s": (0.01, 1.0), "coverage": (0.01, 0.0)}


@dataclass(frozen=True)
class Clocks:
    """`business` is the simulator's clock (None when unknown: then nothing expires and duplicates wait for their
    second invoice); `wall` is real time, which SpacetimeDB timestamps use."""
    wall: datetime
    business: datetime | None = None
    grace_s: float = 900.0
    lifecycle: timedelta = timedelta(days=9)

    @classmethod
    def from_env(cls, business: datetime | None, wall: datetime | None = None) -> Clocks:
        return cls(wall=wall or datetime.now(UTC), business=business,
                   grace_s=float(os.getenv("EVALUATOR_GRACE_S", "900")),
                   lifecycle=timedelta(days=float(os.getenv("EVALUATOR_LIFECYCLE_DAYS", "9"))))


@dataclass
class Decision:
    lane: str
    task_id: str
    outcome: str
    flagged: bool
    at: str
    escalated: bool = False


@dataclass
class LoadScore:
    load_id: str
    scenario: str
    status: str  # "scored", "pending", "unseen", "failed" or "unresolved"
    positive: bool = False
    flagged: bool = False
    caught: bool = False
    escalated: bool = False
    expired: bool = False
    failed_lanes: set[str] = field(default_factory=set)
    flagged_lanes: set[str] = field(default_factory=set)
    at_risk_usd: float = 0.0
    task_seconds: list[float] = field(default_factory=list)
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
    from common.spacetime_client import SpacetimeHTTPError
    rows = {table: await client.query_rows(f'SELECT * FROM "{table}"') for table in TABLES}
    try:
        rows[EPOCH_TABLE] = await client.query_rows(f'SELECT * FROM "{EPOCH_TABLE}"')
    except SpacetimeHTTPError as error:
        # Only a module published before the table existed (SQL "no such table", HTTP 400) means no
        # boundary. Any other failure propagates, so a pass never publishes unbounded historical scores.
        if error.status_code != 400:
            raise
        log.warning("view_epoch table is missing on this module; scoring every load")
        rows[EPOCH_TABLE] = []
    return rows


def events_tail_now(path: pathlib.Path, tail_bytes: int = 65536) -> datetime | None:
    """Latest business timestamp in the simulator's event log, reading only its tail."""
    try:
        with path.open("rb") as handle:
            offset = max(handle.seek(0, os.SEEK_END) - tail_bytes, 0)
            handle.seek(offset)
            lines = handle.read().decode(errors="ignore").splitlines()
    except OSError:
        return None
    if offset:
        lines = lines[1:]  # the first line is cut mid-record
    stamps = []
    for line in lines:
        try:
            stamps.append(_when(json.loads(line).get("sim_ts")))
        except (json.JSONDecodeError, AttributeError):
            continue
    return max((s for s in stamps if s), default=None)


async def business_now(data_dir: pathlib.Path) -> datetime | None:
    """The simulator's business clock: its Redis anchor if reachable, else the event log's latest timestamp."""
    try:
        import redis.asyncio as redis

        from sim import clock
        client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), socket_timeout=2)
        try:
            raw = await client.get(clock.KEY)  # read only: never re-anchors the simulator's clock
        finally:
            await client.aclose()
        if raw:
            return clock.Clock(**json.loads(raw)).now()
    except Exception as error:  # noqa: BLE001 -- the clock is optional; fall back to the event log
        log.debug("business clock unavailable from Redis: %s", type(error).__name__)
    return events_tail_now(data_dir / "events.jsonl")


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


def view_since(rows: Mapping[str, list[dict]]) -> datetime | None:
    """The "Start fresh" boundary from the `view_epoch` singleton, or None when no epoch is set."""
    for row in rows.get(EPOCH_TABLE, []):
        if _text(row.get("id")) == "current":
            return _when(row.get("since"))
    return None


def _in_view(cases: list[dict], since: datetime | None) -> bool:
    """A load is in view when no epoch is set, or its earliest case opened at or after the epoch."""
    if since is None:
        return True
    opened = [when for when in (_when(c.get("opened_at")) for c in cases) if when]
    return bool(opened) and min(opened) >= since


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


def is_parked(task: Mapping) -> bool:
    state = _text(task.get("a2a_state"))
    return state in PARKED_STATES or (state not in TERMINAL_STATES
                                      and _text(task.get("business_status")).startswith("awaiting_"))


def classify_decision(task: Mapping) -> bool | None:
    """For a completed task: True when the outcome flags a problem, False when it lets the item through, None when
    it is no settlement decision."""
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


def _second_invoice_seen(scenario: str, case_ids: list[str], roots: list[dict], invoices: list[dict]) -> bool:
    if scenario == "duplicate_renumbered":
        return any(case_id.endswith("-R") for case_id in case_ids) or len(invoices) >= 2
    return sum(SKILL_LANES.get(_text(t.get("skill"))) == "carrier_ap" for t in roots) >= 2


def score_load(answer: Mapping, cases: list[dict], tasks_by_case: Mapping[str, list[dict]],
               holds_by_case: Mapping[str, list[dict]], proposals_by_case: Mapping[str, list[dict]],
               invoices_by_case: Mapping[str, list[dict]], clocks: Clocks) -> LoadScore:
    scenario = str(answer["scenario"])
    result = LoadScore(load_id=str(answer["load_id"]), scenario=scenario, status="unseen",
                       positive=scenario in ERROR_SCENARIOS)
    if not cases:
        return result
    result.status = "pending"
    case_ids = [_text(c.get("case_id")) for c in cases]
    tasks = [t for case_id in case_ids for t in tasks_by_case.get(case_id, [])]
    holds = [h for case_id in case_ids for h in holds_by_case.get(case_id, [])]
    invoices = [i for case_id in case_ids for i in invoices_by_case.get(case_id, [])]
    roots = [t for t in tasks if not _text(t.get("parent_task_id")) and _text(t.get("skill")) in SKILL_LANES]
    for task in roots:
        if _text(task.get("a2a_state")) == "completed":
            start, end = _when(task.get("started_at")), _when(task.get("updated_at"))
            if start and end:
                result.task_seconds.append(max((end - start).total_seconds(), 0.0))

    started = _when(answer.get("ts"))
    emitted = bool(clocks.business and started and clocks.business >= started + clocks.lifecycle)
    activity = [when for when in (*(_when(c.get("opened_at")) for c in cases),
                                  *(_when(t.get("updated_at")) for t in tasks),
                                  *(_when(h.get("placed_at")) for h in holds)) if when]
    quiet = bool(activity) and (clocks.wall - max(activity)).total_seconds() >= clocks.grace_s
    result.expired = emitted and quiet
    in_flight = any(_text(t.get("a2a_state")) not in TERMINAL_STATES and not is_parked(t) for t in tasks)
    if in_flight and not result.expired:
        return result
    if (scenario in DUPLICATE_SCENARIOS and not emitted
            and not _second_invoice_seen(scenario, case_ids, roots, invoices)):
        return result  # the duplicate has not arrived yet; scoring now would credit "paid once" too early

    for task in roots:
        lane, state = SKILL_LANES[_text(task.get("skill"))], _text(task.get("a2a_state"))
        if state == "completed":
            flagged, escalated = classify_decision(task), False
            if flagged is None:
                continue
        elif is_parked(task):
            flagged, escalated = True, True  # parked on a call or a human: payment is blocked meanwhile
        else:
            if state in TERMINAL_STATES:
                result.failed_lanes.add(lane)
            continue
        at = _text(task.get("updated_at")) or _text(task.get("started_at"))
        current = result.decisions.get(lane)
        if current is None or (at, _text(task.get("task_id"))) > (current.at, current.task_id):
            result.decisions[lane] = Decision(lane, _text(task.get("task_id")), _text(task.get("outcome")),
                                              flagged, at, escalated)
    result.failed_lanes -= result.decisions.keys()
    lanes = required_lanes(answer)
    missing = lanes - result.decisions.keys()
    if missing and not result.expired:
        return result
    result.flagged_lanes = {lane_of(_text(h.get("case_id"))) for h in holds}
    result.flagged_lanes.update(lane for lane, decision in result.decisions.items() if decision.flagged)
    result.flagged = bool(result.flagged_lanes)
    result.escalated = any(decision.escalated for decision in result.decisions.values())
    if missing and not result.positive and not result.flagged:
        result.status = "failed" if missing & result.failed_lanes else "unresolved"
        return result  # nothing decided it either way: not evidence of a correct pass
    result.status = "scored"
    proposals = sum(len(proposals_by_case.get(case_id, [])) for case_id in case_ids)
    result.caught = result.positive and (result.flagged or (scenario == "duplicate_invoice" and proposals == 1))
    result.at_risk_usd = round(at_risk_usd(answer, cases, invoices, lanes), 2) if result.positive else 0.0
    return result


def score(answers: list[dict], rows: Mapping[str, list[dict]], clocks: Clocks | None = None) -> list[LoadScore]:
    clocks = clocks or Clocks(wall=datetime.now(UTC))
    known = [a for a in answers if a["scenario"] in SCENARIOS]
    for unknown in sorted({a["scenario"] for a in answers} - SCENARIOS):
        log.warning("answer-key scenario %r is not classified; its loads are not scored", unknown)
    cases = _cases_by_load(known, rows.get("case", []))
    since = view_since(rows)
    known = [a for a in known if _in_view(cases.get(str(a["load_id"]), []), since)]
    tasks, holds = _group(rows.get("task", []), "case_id"), _group(rows.get("hold", []), "case_id")
    proposals = _group(rows.get("payment_proposal", []), "case_id")
    invoices = _group(rows.get("invoice", []), "case_id")
    return [score_load(a, cases.get(str(a["load_id"]), []), tasks, holds, proposals, invoices, clocks)
            for a in known]


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else UNDEFINED


def scoreboard(loads: list[LoadScore], rows: Mapping[str, list[dict]]) -> dict[str, float]:
    """Metric name -> value for the full, fixed set of names (see the module docstring)."""
    scored = [x for x in loads if x.status == "scored"]
    positives = [x for x in scored if x.positive]
    negatives = [x for x in scored if not x.positive]
    flagged = [x for x in scored if x.flagged]
    count = {status: sum(x.status == status for x in loads)
             for status in ("pending", "unseen", "failed", "unresolved")}
    tp = sum(x.caught for x in positives)
    fp = sum(x.flagged for x in negatives)
    precision = _ratio(sum(x.flagged for x in positives), len(flagged))
    recall = _ratio(tp, len(positives))
    if precision == UNDEFINED or recall == UNDEFINED:
        f1 = UNDEFINED
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
    durations = [seconds for x in loads for seconds in x.task_seconds]
    since = view_since(rows)
    calls = sum(_text(c.get("status")) not in UNPLACED_CALLS
                and (since is None or ((started := _when(c.get("started_at"))) is not None and started >= since))
                for c in rows.get("call", []))
    metrics: dict[str, float] = {
        "precision": precision, "recall": recall,
        "dollars_caught": sum(x.at_risk_usd for x in positives if x.caught),
        "false_holds": fp,
        "median_resolution_s": statistics.median(durations) if durations else UNDEFINED,
        "calls_placed": calls,
        "f1": f1,
        "false_hold_rate": _ratio(fp, len(negatives)),
        "dollars_missed": sum(x.at_risk_usd for x in positives if not x.caught),
        "true_positives": tp, "false_positives": fp, "false_negatives": len(positives) - tp,
        "true_negatives": len(negatives) - fp, "escalated": sum(x.escalated for x in scored),
        "loads_total": len(loads), "loads_scored": len(scored), "loads_pending": count["pending"],
        "loads_unseen": count["unseen"], "loads_failed": sum(bool(x.failed_lanes) for x in loads),
        "loads_unresolved": count["unresolved"],
        "coverage": _ratio(len(scored), len(scored) + count["pending"] + count["failed"] + count["unresolved"]),
    }
    for scenario in sorted(ERROR_SCENARIOS):
        group = [x for x in positives if x.scenario == scenario]
        metrics[f"positives.{scenario}"] = len(group)
        metrics[f"recall.{scenario}"] = _ratio(sum(x.caught for x in group), len(group))
    for scenario in sorted(NEGATIVE_SCENARIOS):
        group = [x for x in negatives if x.scenario == scenario]
        metrics[f"negatives.{scenario}"] = len(group)
        metrics[f"false_hold_rate.{scenario}"] = _ratio(sum(x.flagged for x in group), len(group))
    for lane in LANE_SKILLS:
        group = [x for x in scored if lane in x.flagged_lanes]
        metrics[f"flags.{lane}"] = len(group)
        metrics[f"precision.{lane}"] = _ratio(sum(x.positive for x in group), len(group))
    return {name: round(float(value), 6) for name, value in metrics.items()}


# ----------------------------------------------------------------------------- output

def write_id(name: str, value: float, previous_updated_at: str) -> str:
    digest = hashlib.sha256(f"{value!r}|{previous_updated_at}".encode()).hexdigest()
    return f"evaluator:metric:{name}:{digest[:16]}"


def _unchanged(name: str, stored: float, value: float) -> bool:
    if math.isclose(stored, value, rel_tol=0.0, abs_tol=1e-9):
        return True
    if name not in TOLERANCE or UNDEFINED in (stored, value):
        return False
    relative, absolute = TOLERANCE[name]
    delta = abs(stored - value)
    return delta < absolute or delta < relative * abs(stored)


def changed_metrics(metrics: Mapping[str, float], stored: Iterable[Mapping]) -> list[dict]:
    """`set_metric` payloads for metrics whose stored value differs; unchanged metrics produce nothing."""
    current = {_text(row.get("name")): row for row in stored}
    payloads = []
    for name, value in metrics.items():
        row = current.get(name)
        if row is not None and _unchanged(name, _number(row.get("value")), value):
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
    return "\n".join(f"{name:<{width}}  {'-' if value == UNDEFINED else format(value, 'g')}"
                     for name, value in metrics.items())


# ----------------------------------------------------------------------------- service

def _client():
    from common.spacetime_client import SpacetimeClient
    return SpacetimeClient(actor="evaluator")


async def run_pass(answer_path: pathlib.Path, *, client=None, rows: Mapping | None = None,
                   clocks: Clocks | None = None, dry_run: bool = False,
                   dump: pathlib.Path | None = None) -> tuple[dict[str, float], list[str]]:
    answers = read_answer_key(answer_path)
    if clocks is None:
        clocks = Clocks.from_env(await business_now(answer_path.parent))
    if rows is None:
        rows = await fetch_rows(client)
    if dump is not None:
        dump.write_text(json.dumps(rows, indent=1, sort_keys=True, default=str))
    metrics = scoreboard(score(answers, rows, clocks), rows)
    if dry_run:
        return metrics, []
    return metrics, await publish(client, metrics, rows.get("metric", []))


def _clocks(args: argparse.Namespace) -> Clocks | None:
    if args.business_now is None and args.wall_now is None:
        return None
    business = args.business_now or events_tail_now((args.answer_key or answer_key_path()).parent / "events.jsonl")
    return Clocks.from_env(business, args.wall_now)


async def main_async(args: argparse.Namespace) -> int:
    answer_path = args.answer_key or answer_key_path()
    if args.rows:
        rows = json.loads(args.rows.read_text())
        metrics, _ = await run_pass(answer_path, rows=rows, clocks=_clocks(args), dry_run=True)
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
            metrics, written = await run_pass(answer_path, client=client, clocks=_clocks(args),
                                              dry_run=args.dry_run, dump=args.dump_rows)
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
                     metrics["loads_scored"], metrics["loads_total"], metrics["precision"], metrics["recall"],
                     len(written))
        await asyncio.sleep(args.loop)


def _timestamp(value: str) -> datetime:
    parsed = _when(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}")
    return parsed


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
    parser.add_argument("--business-now", type=_timestamp, metavar="ISO",
                        help="simulator business time (default: Redis clock, else the event log)")
    parser.add_argument("--wall-now", type=_timestamp, metavar="ISO", help="real time to score at (default: now)")
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
