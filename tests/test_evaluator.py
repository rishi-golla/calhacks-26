"""Evaluator: answer-key join, scoring math, idempotent metric writes, in-flight and expired loads (PRD G6/T10)."""
import json
import pathlib
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from common.spacetime_client import SpacetimeClient
from evaluator import main as evaluator
from sim import generator

U = evaluator.UNDEFINED
WALL = datetime(2026, 10, 24, 12, 30, tzinfo=UTC)  # 29 min after the fixture's last activity: quiet
BUSINESS = datetime(2026, 10, 5, tzinfo=UTC)
CLOCKS = evaluator.Clocks(wall=WALL, business=BUSINESS)
RECENT = "2026-10-01T00:00:00+00:00"  # business start: lifecycle (9 days) not yet emitted at BUSINESS
OLD = "2026-09-01T00:00:00+00:00"  # every step emitted at BUSINESS


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os
    for name in os.environ:
        if name.startswith(("SPACETIME_", "EVALUATOR_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # unreachable: no real business clock


def answer(load_id: str, scenario: str, planted: dict | None = None, ts: str = RECENT) -> dict:
    """The shape sim.generator.record_answer appends to answer_key.jsonl."""
    return {"ts": ts, "case_id": f"CASE-{load_id}", "load_id": load_id,
            "carrier": "Sierra Haul LLC", "shipper": "Bay Produce Co", "scenario": scenario,
            "planted": planted or {}, "expected": generator.expected({}, scenario)}


def case(case_id: str, load_id: str = "", exposure: float = 0.0, opened: str = "2026-10-24T12:00:00.000Z") -> dict:
    return {"case_id": case_id, "load_id": load_id, "opened_at": opened, "status": "open", "priority": "P2",
            "title": "", "exposure_usd": exposure, "resolved_at": ""}


def task(task_id: str, case_id: str, skill: str, outcome: str = "", state: str = "completed", parent: str = "",
         findings: dict | None = None, updated: str = "2026-10-24T12:01:00.000Z", status: str = "") -> dict:
    artifact = json.dumps({"outcome": outcome, "findings": findings or {}}) if outcome else ""
    return {"task_id": task_id, "case_id": case_id, "parent_task_id": parent, "agent": "carrier_ap",
            "skill": skill, "a2a_state": state, "business_status": status or ("resolved" if outcome else "queued"),
            "outcome": outcome, "started_at": "2026-10-24T12:00:30.000Z", "updated_at": updated,
            "artifact_json": artifact}


AP, AR, BANK = "process_carrier_invoice", "reconcile_customer_payment", "screen_bank_change"


def fixture() -> tuple[list[dict], dict[str, list[dict]]]:
    answers = [
        answer("L1", "rate_mismatch", {"field": "linehaul", "delta": 250}),  # held -> caught
        answer("L2", "detention_unsupported", {"field": "detention", "claimed_h": 2}),  # paid -> missed
        answer("L3", "clean"),  # paid and reconciled -> true negative
        answer("L4", "decoy_quickpay", {"field": "quickpay_discount"}),  # held -> false hold
        answer("L5", "shipper_short_pay"),  # receivable partial -> caught
        answer("L6", "bank_change", {"field": "remit_to"}),  # screen parked on a call -> caught, escalated
        answer("L7", "clean"),  # never reached SpacetimeDB -> unseen
        answer("L8", "duplicate_invoice", ts=OLD),  # duplicate deduped, paid exactly once -> caught
        answer("L9", "clean"),  # receivable lane not decided yet -> pending
        answer("L10", "decoy_detention_supported_small"),  # cleared, not held -> true negative
        answer("L11", "fsc_error"),  # still working -> pending
    ]
    rows = {
        "case": [
            case("CASE-L1", "L1", 250), case("CASE-L2", "L2", 300), case("CASE-L3", "L3"),
            case("CASE-L3-AR", "L3"), case("CASE-L4", "L4"), case("CASE-L5-AR", "L5", 225),
            case("CASE-L6-BANK", "", 9400),  # bank cases may lack load_id: joined by the case-ID scheme
            case("CASE-L8", "L8"), case("CASE-L9", "L9"), case("CASE-L9-AR", "L9"), case("CASE-L10", "L10"),
            case("CASE-L11", "L11"), case("CASE-OTHER", "LX"),  # not in the answer key
        ],
        "task": [
            task("t1", "CASE-L1", AP, "hold"), task("t1c", "CASE-L1", "review_hold", "hold", parent="t1"),
            task("t2", "CASE-L2", AP, "approve"),
            task("t3", "CASE-L3", AP, "approve"), task("t3r", "CASE-L3-AR", AR, "verified"),
            task("t4", "CASE-L4", AP, "hold"),
            task("t5", "CASE-L5-AR", AR, "partial", updated="2026-10-24T12:03:00.000Z"),
            task("t6", "CASE-L6-BANK", BANK, state="input-required", status="awaiting_call"),
            task("t8", "CASE-L8", AP, "approve"),
            task("t9", "CASE-L9", AP, "approve"),
            task("t10", "CASE-L10", AP, "unverified", findings={"payment_blocked": False}),
            task("t11", "CASE-L11", AP, state="working", status="investigating"),
        ],
        "hold": [{"id": "h1", "case_id": "CASE-L1", "amount": 250.0, "status": "active"},
                 {"id": "h4", "case_id": "CASE-L4", "amount": 40.0, "status": "active"}],
        "payment_proposal": [{"id": "p2", "case_id": "CASE-L2"}, {"id": "p3", "case_id": "CASE-L3"},
                             {"id": "p8", "case_id": "CASE-L8"}],
        "invoice": [{"id": "i8", "case_id": "CASE-L8", "amount": 1800.0}],
        "call": [{"id": "c1", "status": "completed"}, {"id": "c2", "status": "queued"},
                 {"id": "c3", "status": "no_answer"}],
        "metric": [],
    }
    return answers, rows


def one(scenario: str, rows: dict, clocks: evaluator.Clocks = CLOCKS, **kwargs) -> evaluator.LoadScore:
    [load] = evaluator.score([answer("L1", scenario, **kwargs)], rows, clocks)
    return load


def test_scenario_classes_cover_the_simulator_exactly():
    classes = (evaluator.ERROR_SCENARIOS, evaluator.DECOY_SCENARIOS, evaluator.CLEAN_SCENARIOS)
    assert set().union(*classes) == set(generator.SCENARIOS)
    assert sum(map(len, classes)) == len(generator.SCENARIOS)  # disjoint


def test_scoreboard_math():
    answers, rows = fixture()
    loads = {x.load_id: x for x in evaluator.score(answers, rows, CLOCKS)}
    assert {k: v.status for k, v in loads.items()} == {
        "L1": "scored", "L2": "scored", "L3": "scored", "L4": "scored", "L5": "scored", "L6": "scored",
        "L7": "unseen", "L8": "scored", "L9": "pending", "L10": "scored", "L11": "pending"}
    metrics = evaluator.scoreboard(list(loads.values()), rows)
    assert metrics["precision"] == 0.75  # L1, L5, L6 of flagged L1, L4, L5, L6
    assert metrics["recall"] == 0.8  # L1, L5, L6, L8 of L1, L2, L5, L6, L8
    assert metrics["f1"] == round(2 * 0.75 * 0.8 / 1.55, 6)
    assert metrics["false_holds"] == 1 and metrics["false_hold_rate"] == round(1 / 3, 6)
    assert metrics["dollars_caught"] == 250 + 225 + 9400 + 1800  # planted delta, exposures, duplicate invoice
    assert metrics["dollars_missed"] == 300  # L2's detention exposure
    assert (metrics["true_positives"], metrics["false_positives"], metrics["false_negatives"],
            metrics["true_negatives"], metrics["escalated"]) == (4, 1, 1, 2, 1)
    assert (metrics["loads_total"], metrics["loads_scored"], metrics["loads_pending"], metrics["loads_unseen"],
            metrics["loads_failed"], metrics["loads_unresolved"]) == (11, 8, 2, 1, 0, 0)
    assert metrics["coverage"] == round(8 / 10, 6)
    assert metrics["calls_placed"] == 2
    # Agent time of completed top-level lane tasks: 30 s each, one 150 s; parked and working tasks don't count.
    assert metrics["median_resolution_s"] == 30
    assert metrics["recall.rate_mismatch"] == 1 and metrics["recall.detention_unsupported"] == 0
    assert metrics["positives.duplicate_invoice"] == 1 and metrics["recall.bank_change"] == 1
    assert metrics["false_hold_rate.decoy_quickpay"] == 1 and metrics["false_hold_rate.clean"] == 0
    assert metrics["negatives.decoy_detention_supported_small"] == 1
    assert metrics["flags.carrier_ap"] == 2 and metrics["precision.carrier_ap"] == 0.5
    assert metrics["flags.shipper_ar"] == 1 and metrics["precision.shipper_ar"] == 1
    assert metrics["flags.risk"] == 1 and metrics["precision.risk"] == 1


def test_every_name_is_written_with_sentinels_and_zero_counts():
    """A figure that becomes undefined or drops out is overwritten, never left stale."""
    metrics = evaluator.scoreboard(evaluator.score([answer("L1", "clean")], {}, CLOCKS), {})
    _, rows = fixture()
    full = evaluator.scoreboard(evaluator.score(fixture()[0], rows, CLOCKS), rows)
    assert metrics.keys() == full.keys()
    assert metrics["loads_unseen"] == 1 and metrics["false_holds"] == 0
    for name in ("precision", "recall", "f1", "coverage", "median_resolution_s", "false_hold_rate",
                 "recall.fsc_error", "false_hold_rate.clean", "precision.risk"):
        assert metrics[name] == U
    assert metrics["positives.fsc_error"] == 0 and metrics["negatives.clean"] == 0
    assert all(value == U or value >= 0 for value in full.values())  # -1 is unambiguous: real figures are >= 0


def test_f1_is_zero_not_undefined_when_nothing_is_caught():
    rows = {"case": [case("CASE-L1", "L1"), case("CASE-L2", "L2")],
            "task": [task("a", "CASE-L1", AP, "approve"), task("b", "CASE-L2", AP, "hold")]}
    loads = evaluator.score([answer("L1", "rate_mismatch"), answer("L2", "decoy_quickpay")], rows, CLOCKS)
    metrics = evaluator.scoreboard(loads, rows)
    assert (metrics["precision"], metrics["recall"], metrics["f1"]) == (0, 0, 0)


def test_scoreboard_renders_negative_sentinels_as_a_dash():
    source = (pathlib.Path(__file__).parents[1] / "web/components/operations.tsx").read_text()
    answers, rows = fixture()
    metrics = evaluator.scoreboard(evaluator.score(answers, rows, CLOCKS), rows)
    for name in ("precision", "recall", "dollars_caught", "false_holds", "median_resolution_s", "calls_placed"):
        assert f'"{name}"' in source and name in metrics
    assert "num(row.value) >= 0" in source  # the Scoreboard drops the -1 sentinel, showing "—"


@pytest.mark.parametrize("tasks", [
    [task("r", "CASE-L1", AP, state="working", status="investigating")],
    [task("r", "CASE-L1", AP, state="submitted")],
    # A parked root still has a child working.
    [task("r", "CASE-L1", AP, state="input-required", status="awaiting_other_agent"),
     task("c", "CASE-L1", "investigate_charge", state="working", parent="r", status="investigating")],
    # Delivery evidence ahead of the bill is not a settlement decision.
    [task("r", "CASE-L1", AP, "unverified", findings={"awaiting_invoice": True})],
    [task("c", "CASE-L1", "review_hold", "hold", parent="x")],  # only a child: no lane decision
    [],
])
def test_in_flight_loads_are_pending_not_scored(tasks):
    rows = {"case": [case("CASE-L1", "L1")], "task": tasks}
    load = one("rate_mismatch", rows, planted={"delta": 100})
    assert load.status == "pending"
    metrics = evaluator.scoreboard([load], rows)
    assert metrics["loads_pending"] == 1 and metrics["loads_scored"] == 0 and metrics["recall"] == U


def test_parked_root_is_an_escalated_flag_and_parked_children_are_ignored():
    rows = {"case": [case("CASE-L1", "L1")],
            "task": [task("r", "CASE-L1", AP, state="input-required", status="awaiting_human_approval")]}
    load = one("pod_unsigned", rows)
    assert load.status == "scored" and load.flagged and load.caught and load.escalated
    rows["task"] = [task("r", "CASE-L1", AP, "hold"),
                    task("c", "CASE-L1", "verify_claim", state="input-required", parent="r", status="awaiting_call")]
    load = one("detention_unsupported", rows)
    assert load.status == "scored" and load.caught and not load.escalated


def expired_clocks(**overrides) -> evaluator.Clocks:
    return evaluator.Clocks(**{"wall": WALL, "business": BUSINESS, **overrides})


def test_expired_load_with_a_missing_lane_is_a_miss():
    # misapplied_payment: the payable side was paid, the receivable case never opened.
    rows = {"case": [case("CASE-L1", "L1")], "task": [task("ap", "CASE-L1", AP, "approve")]}
    assert one("misapplied_payment", rows).status == "pending"  # lifecycle not fully emitted yet
    load = one("misapplied_payment", rows, ts=OLD)
    assert load.expired and load.status == "scored" and not load.caught
    # Not quiet long enough, or no business clock: keep waiting.
    assert one("misapplied_payment", rows, clocks=expired_clocks(grace_s=3600), ts=OLD).status == "pending"
    assert one("misapplied_payment", rows, clocks=expired_clocks(business=None), ts=OLD).status == "pending"
    # A stuck working task stops blocking once the load expires.
    rows["task"].append(task("stuck", "CASE-L1", "verify_claim", state="working", parent="ap"))
    assert one("misapplied_payment", rows, ts=OLD).status == "scored"


def test_expired_negative_that_nothing_decided_is_unresolved_not_a_true_negative():
    rows = {"case": [case("CASE-L1", "L1")], "task": [task("ap", "CASE-L1", AP, state="working")]}
    load = one("clean", rows, ts=OLD)
    assert load.status == "unresolved"
    metrics = evaluator.scoreboard([load], rows)
    assert metrics["true_negatives"] == 0 and metrics["loads_unresolved"] == 1 and metrics["coverage"] == 0


def test_failed_tasks_never_decide_a_lane():
    rows = {"case": [case("CASE-L1", "L1")], "task": [task("f", "CASE-L1", AP, state="failed")]}
    assert one("decoy_quickpay", rows).status == "pending"
    load = one("decoy_quickpay", rows, ts=OLD)
    assert load.status == "failed" and load.failed_lanes == {"carrier_ap"}
    metrics = evaluator.scoreboard([load], rows)
    assert metrics["loads_failed"] == 1 and metrics["true_negatives"] == 0
    assert one("rate_mismatch", rows, ts=OLD).status == "scored"  # a positive it failed on is a miss
    # A completed task beats failed ones, older or newer.
    rows["task"] += [task("ok", "CASE-L1", AP, "approve", updated="2026-10-24T12:00:50Z"),
                     task("f2", "CASE-L1", AP, state="failed", updated="2026-10-24T12:05:00Z")]
    load = one("decoy_quickpay", rows)
    assert load.status == "scored" and load.decisions["carrier_ap"].task_id == "ok" and not load.failed_lanes


@pytest.mark.parametrize("scenario", ["duplicate_invoice", "duplicate_renumbered"])
def test_duplicates_wait_for_the_second_invoice(scenario):
    rows = {"case": [case("CASE-L1", "L1")], "task": [task("a", "CASE-L1", AP, "approve")],
            "payment_proposal": [{"id": "p", "case_id": "CASE-L1"}]}
    assert one(scenario, rows).status == "pending"
    assert one(scenario, rows, ts=OLD).status == "scored"  # its scheduled time has passed
    if scenario == "duplicate_renumbered":
        rows["case"].append(case("CASE-L1-R", "L1"))
        rows["task"].append(task("b", "CASE-L1-R", AP, "reject", updated="2026-10-24T12:02:00Z"))
    else:
        rows["task"].append(task("b", "CASE-L1", AP, "reject", updated="2026-10-24T12:02:00Z"))
    load = one(scenario, rows)
    assert load.status == "scored" and load.caught and load.flagged


def test_latest_root_decision_wins_and_holds_flag_even_when_paid():
    rows = {"case": [case("CASE-L1", "L1")], "task": [
        task("a", "CASE-L1", AP, "approve", updated="2026-10-24T12:01:00Z"),
        task("b", "CASE-L1", AP, "reject", updated="2026-10-24T12:05:00Z")]}
    load = one("duplicate_invoice", rows)
    assert load.decisions["carrier_ap"].task_id == "b" and load.flagged and load.caught
    rows["task"][1]["updated_at"] = "2026-10-24T12:00:00Z"  # now the approval is the newer decision
    load = one("duplicate_invoice", rows)
    assert not load.flagged and not load.caught  # no proposal row: nothing shows it was paid once
    rows["hold"] = [{"id": "h", "case_id": "CASE-L1", "amount": 10}]
    load = one("duplicate_invoice", rows)
    assert load.flagged and load.flagged_lanes == {"carrier_ap"}


@pytest.mark.parametrize(("outcome", "findings", "expected"), [
    ("hold", {}, True), ("reject", {}, True), ("suspicious", {}, True), ("partial", {}, True),
    ("approve", {}, False), ("clear", {}, False), ("verified", {}, False),
    ("unverified", {"documents": []}, True), ("unverified", {"payment_blocked": False}, False),
    ("unverified", {"approval": []}, False), ("unverified", {"awaiting_invoice": True}, None),
])
def test_decision_classification(outcome, findings, expected):
    assert evaluator.classify_decision(task("t", "C", AP, outcome, findings=findings)) is expected


def test_bank_change_lane_and_case_id_join():
    rows = {"case": [case("CASE-L6", "L6"), case("CASE-L6-BANK", "", 9400)],
            "task": [task("ap", "CASE-L6", AP, "approve"), task("bk", "CASE-L6-BANK", BANK, "suspicious")]}
    [load] = evaluator.score([answer("L6", "bank_change")], rows, CLOCKS)
    assert load.status == "scored" and load.flagged_lanes == {"risk"} and load.at_risk_usd == 9400
    # Without the bank screen the load waits: `risk` is the lane the answer key expects.
    [load] = evaluator.score([answer("L6", "bank_change")], {**rows, "task": rows["task"][:1]}, CLOCKS)
    assert load.status == "pending"


def test_bank_screen_on_the_payable_case_still_decides_the_risk_lane():
    # Ingest falls back to the sim's case hint (CASE-<load>) when it cannot resolve the bank change's load.
    rows = {"case": [case("CASE-L6", "L6")], "task": [task("bk", "CASE-L6", BANK, "suspicious")]}
    [load] = evaluator.score([answer("L6", "bank_change")], rows, CLOCKS)
    assert load.status == "scored" and load.caught and load.flagged_lanes == {"risk"}


def test_answer_key_reader_dedupes_and_skips_bad_lines(tmp_path):
    path = tmp_path / "answer_key.jsonl"
    path.write_text("\n".join([json.dumps(answer("L1", "clean")), "not json", "",
                               json.dumps(answer("L1", "rate_mismatch")), json.dumps({"load_id": "L2"})]) + "\n")
    assert [a["scenario"] for a in evaluator.read_answer_key(path)] == ["rate_mismatch"]
    with pytest.raises(FileNotFoundError):
        evaluator.read_answer_key(tmp_path / "missing.jsonl")


def test_reads_what_the_simulator_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))
    load = {"case_id": "CASE-LD-1", "load_id": "LD-1", "carrier": {"name": "C"}, "shipper": {"name": "S"}}
    generator.record_answer(load, "fsc_error", {"billed": 412.5, "contract": 380.0}, BUSINESS - timedelta(days=1))
    [entry] = evaluator.read_answer_key(evaluator.answer_key_path())
    rows = {"case": [case("CASE-LD-1", "LD-1")], "task": [task("t", "CASE-LD-1", AP, "hold")]}
    [scored] = evaluator.score([entry], rows, CLOCKS)
    assert scored.caught and scored.at_risk_usd == 32.5


async def test_business_clock_falls_back_to_the_event_log_tail(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps({"type": "x" * 200, "sim_ts": f"2026-10-0{day}T00:00:00Z"}) + "\n"
                           for day in (1, 3, 2)))
    assert evaluator.events_tail_now(log) == datetime(2026, 10, 3, tzinfo=UTC)
    assert evaluator.events_tail_now(log, tail_bytes=300) == datetime(2026, 10, 2, tzinfo=UTC)  # cut line dropped
    assert evaluator.events_tail_now(tmp_path / "missing.jsonl") is None
    assert await evaluator.business_now(tmp_path) == datetime(2026, 10, 3, tzinfo=UTC)  # Redis is unreachable


def test_write_ids_are_deterministic_and_skip_unchanged_values():
    metrics = {"precision": 0.5, "recall": 0.75}
    first = evaluator.changed_metrics(metrics, [])
    assert first == evaluator.changed_metrics(metrics, [])  # a retry of an unconfirmed write reuses its ID
    assert all(re.fullmatch(r"evaluator:metric:[a-z_.]+:[0-9a-f]{16}", p["write_id"]) for p in first)
    stored = [{"name": "precision", "value": 0.5, "updated_at": "T1"},
              {"name": "recall", "value": 0.7, "updated_at": "T1"}]
    [changed] = evaluator.changed_metrics(metrics, stored)
    assert changed["name"] == "recall" and changed["write_id"] not in {p["write_id"] for p in first}
    # A value that changes back after another write gets a new ID, so it is not swallowed as a replay.
    back = evaluator.changed_metrics({"recall": 0.75}, [{"name": "recall", "value": 0.7, "updated_at": "T2"}])
    assert back[0]["write_id"] != changed["write_id"]
    assert evaluator.changed_metrics(metrics, [{**s, "value": metrics[s["name"]]} for s in stored]) == []


@pytest.mark.parametrize(("name", "stored", "value", "written"), [
    ("median_resolution_s", 30.0, 30.8, False),  # under 1 s
    ("median_resolution_s", 400.0, 403.0, False),  # under 1%
    ("median_resolution_s", 30.0, 32.0, True),
    ("median_resolution_s", U, 30.0, True), ("median_resolution_s", 30.0, U, True),
    ("coverage", 0.9, 0.905, False), ("coverage", 0.9, 0.92, True),
    ("precision", 0.9, 0.905, True),  # scores are never rounded away
])
def test_tiny_changes_to_volatile_metrics_are_not_rewritten(name, stored, value, written):
    payloads = evaluator.changed_metrics({name: value}, [{"name": name, "value": stored, "updated_at": "T"}])
    assert bool(payloads) is written


class FakeSpacetime:
    """SQL reads in the SpacetimeDB wire format; set_metric applies like the module does."""

    def __init__(self, rows: dict[str, list[dict]]):
        self.rows = {name: [dict(r) for r in table] for name, table in rows.items()}
        self.calls: list[dict] = []
        self.receipts: set[str] = set()
        self.tick = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer evaluator-secret"
        if request.url.path.endswith("/sql"):
            table = re.fullmatch(r'SELECT \* FROM "(\w+)"', request.content.decode()).group(1)
            items = self.rows.get(table, [])
            columns = sorted({key for row in items for key in row}) or ["id"]
            return httpx.Response(200, json=[{"schema": {"elements": [{"name": {"some": c}} for c in columns]},
                                              "rows": [[row.get(c) for c in columns] for row in items]}])
        assert request.url.path.endswith("/call/set_metric")
        payload = json.loads(json.loads(request.content)[0])
        self.calls.append(payload)
        if payload["write_id"] not in self.receipts:
            self.receipts.add(payload["write_id"])
            self.tick += 1
            table = [r for r in self.rows.setdefault("metric", []) if r["name"] != payload["name"]]
            table.append({"name": payload["name"], "value": payload["value"], "updated_at": f"T{self.tick}"})
            self.rows["metric"] = table
        return httpx.Response(200)


async def test_live_pass_writes_once_then_is_a_no_op(tmp_path):
    answers, rows = fixture()
    key = tmp_path / "answer_key.jsonl"
    key.write_text("".join(json.dumps(a) + "\n" for a in answers))
    server = FakeSpacetime(rows)
    client = SpacetimeClient(actor="evaluator", token="evaluator-secret", disabled=False,
                             transport=httpx.MockTransport(server.handler))
    metrics, written = await evaluator.run_pass(key, client=client, clocks=CLOCKS)
    assert set(written) == set(metrics) and len(server.calls) == len(metrics)
    assert {r["name"]: r["value"] for r in server.rows["metric"]} == metrics
    again, written = await evaluator.run_pass(key, client=client, clocks=CLOCKS)
    assert again == metrics and written == [] and len(server.calls) == len(metrics)
    # One more decided load changes only the figures it moves.
    server.rows["task"].append(task("t9r", "CASE-L9-AR", AR, "verified"))
    _, written = await evaluator.run_pass(key, client=client, clocks=CLOCKS)
    assert "loads_scored" in written and "precision" not in written and "calls_placed" not in written
    dry, written = await evaluator.run_pass(key, client=client, clocks=CLOCKS, dry_run=True)
    assert written == [] and dry["loads_scored"] == 9


async def test_cli_offline_rows_prints_and_never_connects(tmp_path, capsys, monkeypatch):
    answers, rows = fixture()
    (tmp_path / "answer_key.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.setattr(evaluator, "_client", lambda: pytest.fail("offline scoring must not build a client"))
    args = evaluator.parse_args(["--rows", str(tmp_path / "rows.json"), "--answer-key",
                                 str(tmp_path / "answer_key.jsonl"), "--json",
                                 "--business-now", BUSINESS.isoformat(), "--wall-now", WALL.isoformat()])
    assert await evaluator.main_async(args) == 0
    assert json.loads(capsys.readouterr().out)["recall"] == 0.8


async def test_cli_refuses_disabled_spacetime_and_missing_token(tmp_path, monkeypatch, capsys):
    (tmp_path / "answer_key.jsonl").write_text("")
    args = evaluator.parse_args(["--once", "--answer-key", str(tmp_path / "answer_key.jsonl")])
    assert await evaluator.main_async(args) == 2
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    assert await evaluator.main_async(args) == 2
    assert "SPACETIME_TOKEN_EVALUATOR" in capsys.readouterr().err
    for bad in (["--loop", "0"], ["--business-now", "yesterday"]):
        with pytest.raises(SystemExit):
            evaluator.parse_args(bad)


def epoch(since: str) -> list[dict]:
    return [{"id": "current", "since": since, "set_by": "human:operator", "set_at": since}]


def test_view_epoch_scores_only_loads_opened_since_the_boundary():
    answers, rows = fixture()
    full = evaluator.scoreboard(evaluator.score(answers, rows, CLOCKS), rows)
    # Every fixture case opens at 12:00; L3's two cases open later, and L1 has one case before and one after.
    late = {**rows, "case": [{**c, "opened_at": "2026-10-24T12:10:00.000Z"} if c["case_id"].startswith("CASE-L3")
                             else c for c in rows["case"]] + [case("CASE-L1-AR", "L1", opened="2026-10-24T12:10:00.000Z")]}
    loads = evaluator.score(answers, {**late, "view_epoch": epoch("2026-10-24T12:05:00.000Z")}, CLOCKS)
    assert [x.load_id for x in loads] == ["L3"]
    # A load is kept only when its earliest case is in view, and the boundary itself is inclusive.
    assert len(evaluator.score(answers, {**rows, "view_epoch": epoch("2026-10-24T12:00:00.000Z")}, CLOCKS)) \
        == sum(bool(evaluator._cases_by_load(answers, rows["case"]).get(a["load_id"])) for a in answers)
    metrics = evaluator.scoreboard(loads, {**late, "view_epoch": epoch("2026-10-24T12:05:00.000Z")})
    assert set(metrics) == set(full) and metrics["loads_total"] == 1 and metrics["loads_unseen"] == 0
    # No row means no boundary: the same figures as before.
    assert evaluator.scoreboard(evaluator.score(answers, {**rows, "view_epoch": []}, CLOCKS), rows) == full


def test_view_epoch_counts_only_calls_started_since_the_boundary():
    rows = {"call": [{"status": "completed", "started_at": "2026-10-24T11:00:00.000Z"},
                     {"status": "completed", "started_at": "2026-10-24T13:00:00.000Z"}]}
    assert evaluator.scoreboard([], rows)["calls_placed"] == 2
    assert evaluator.scoreboard([], {**rows, "view_epoch": epoch("2026-10-24T12:00:00.000Z")})["calls_placed"] == 1


async def test_fetch_rows_treats_a_missing_view_epoch_table_as_no_boundary():
    class Client:
        async def query_rows(self, sql):
            if '"view_epoch"' in sql:
                raise RuntimeError("no such table")
            return []
    rows = await evaluator.fetch_rows(Client())
    assert rows["view_epoch"] == [] and evaluator.view_since(rows) is None
