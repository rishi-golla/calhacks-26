"""Evaluator: answer-key join, scoring math, idempotent metric writes, in-flight loads (PRD G6/T10)."""
import json
import pathlib
import re

import httpx
import pytest

from common.spacetime_client import SpacetimeClient
from evaluator import main as evaluator
from sim import generator


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os
    for name in os.environ:
        if name.startswith("SPACETIME_"):
            monkeypatch.delenv(name)


def answer(load_id: str, scenario: str, planted: dict | None = None) -> dict:
    """The shape sim.generator.record_answer appends to answer_key.jsonl."""
    return {"ts": "2026-10-24T12:00:00+00:00", "case_id": f"CASE-{load_id}", "load_id": load_id,
            "carrier": "Sierra Haul LLC", "shipper": "Bay Produce Co", "scenario": scenario,
            "planted": planted or {}, "expected": generator.expected({}, scenario)}


def case(case_id: str, load_id: str = "", exposure: float = 0.0, opened: str = "2026-10-24T12:00:00.000Z") -> dict:
    return {"case_id": case_id, "load_id": load_id, "opened_at": opened, "status": "open", "priority": "P2",
            "title": "", "exposure_usd": exposure, "resolved_at": ""}


def task(task_id: str, case_id: str, skill: str, outcome: str = "", state: str = "completed", parent: str = "",
         findings: dict | None = None, updated: str = "2026-10-24T12:01:00.000Z") -> dict:
    artifact = json.dumps({"outcome": outcome, "findings": findings or {}}) if outcome else ""
    return {"task_id": task_id, "case_id": case_id, "parent_task_id": parent, "agent": "carrier_ap",
            "skill": skill, "a2a_state": state, "business_status": "resolved", "outcome": outcome,
            "started_at": "2026-10-24T12:00:30.000Z", "updated_at": updated, "artifact_json": artifact}


AP, AR, BANK = "process_carrier_invoice", "reconcile_customer_payment", "screen_bank_change"


def fixture() -> tuple[list[dict], dict[str, list[dict]]]:
    answers = [
        answer("L1", "rate_mismatch", {"field": "linehaul", "delta": 250}),  # held -> caught
        answer("L2", "detention_unsupported", {"field": "detention", "claimed_h": 2}),  # paid -> missed
        answer("L3", "clean"),  # paid and reconciled -> true negative
        answer("L4", "decoy_quickpay", {"field": "quickpay_discount"}),  # held -> false hold
        answer("L5", "shipper_short_pay"),  # receivable partial -> caught
        answer("L6", "bank_change", {"field": "remit_to"}),  # screen parked on a call -> pending
        answer("L7", "clean"),  # never reached SpacetimeDB -> unseen
        answer("L8", "duplicate_invoice"),  # paid exactly once -> caught (approve_once)
        answer("L9", "clean"),  # receivable lane not decided yet -> pending
        answer("L10", "decoy_detention_supported_small"),  # cleared, not held -> true negative
    ]
    rows = {
        "case": [
            case("CASE-L1", "L1", 250), case("CASE-L2", "L2", 300, opened="2026-10-24T11:59:00.000Z"),
            case("CASE-L3", "L3"), case("CASE-L3-AR", "L3"), case("CASE-L4", "L4"), case("CASE-L5-AR", "L5", 225),
            case("CASE-L6-BANK", "", 9400),  # bank cases may lack load_id: joined by the case-ID scheme
            case("CASE-L8", "L8"), case("CASE-L9", "L9"), case("CASE-L9-AR", "L9"), case("CASE-L10", "L10"),
            case("CASE-OTHER", "LX"),  # not in the answer key
        ],
        "task": [
            task("t1", "CASE-L1", AP, "hold"), task("t1c", "CASE-L1", "review_hold", "hold", parent="t1"),
            task("t2", "CASE-L2", AP, "approve"),
            task("t3", "CASE-L3", AP, "approve"), task("t3r", "CASE-L3-AR", AR, "verified"),
            task("t4", "CASE-L4", AP, "hold"),
            task("t5", "CASE-L5-AR", AR, "partial", updated="2026-10-24T12:03:00.000Z"),
            task("t6", "CASE-L6-BANK", BANK, state="input-required"),
            task("t8", "CASE-L8", AP, "approve"),
            task("t9", "CASE-L9", AP, "approve"),
            task("t10", "CASE-L10", AP, "unverified", findings={"payment_blocked": False}),
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


def test_scenario_classes_cover_the_simulator_exactly():
    classes = (evaluator.ERROR_SCENARIOS, evaluator.DECOY_SCENARIOS, evaluator.CLEAN_SCENARIOS)
    assert set().union(*classes) == set(generator.SCENARIOS)
    assert sum(map(len, classes)) == len(generator.SCENARIOS)  # disjoint


def test_scoreboard_math():
    answers, rows = fixture()
    loads = {x.load_id: x for x in evaluator.score(answers, rows)}
    assert {k: v.status for k, v in loads.items()} == {
        "L1": "scored", "L2": "scored", "L3": "scored", "L4": "scored", "L5": "scored", "L6": "pending",
        "L7": "unseen", "L8": "scored", "L9": "pending", "L10": "scored"}
    metrics = evaluator.scoreboard(list(loads.values()), rows)
    assert metrics["precision"] == round(2 / 3, 6)  # L1, L5 of flagged L1, L4, L5
    assert metrics["recall"] == 0.75  # L1, L5, L8 of L1, L2, L5, L8
    assert metrics["f1"] == round(2 * (2 / 3) * 0.75 / (2 / 3 + 0.75), 6)
    assert metrics["false_holds"] == 1 and metrics["false_hold_rate"] == round(1 / 3, 6)
    assert metrics["dollars_caught"] == 250 + 225 + 1800  # planted delta, AR exposure, duplicate invoice
    assert metrics["dollars_missed"] == 300  # L2's detention exposure
    assert (metrics["true_positives"], metrics["false_positives"], metrics["false_negatives"],
            metrics["true_negatives"]) == (3, 1, 1, 2)
    assert (metrics["loads_total"], metrics["loads_scored"], metrics["loads_pending"],
            metrics["loads_unseen"]) == (10, 7, 2, 1)
    assert metrics["coverage"] == round(7 / 9, 6)
    assert metrics["calls_placed"] == 2
    # Per load: first case opened -> last lane decision. L2 opened a minute early, L5 decided two minutes late.
    assert metrics["median_resolution_s"] == 60
    assert metrics["recall.rate_mismatch"] == 1 and metrics["recall.detention_unsupported"] == 0
    assert metrics["positives.duplicate_invoice"] == 1
    assert metrics["false_hold_rate.decoy_quickpay"] == 1 and metrics["false_hold_rate.clean"] == 0
    assert metrics["negatives.decoy_detention_supported_small"] == 1
    assert metrics["flags.carrier_ap"] == 2 and metrics["precision.carrier_ap"] == 0.5
    assert metrics["flags.shipper_ar"] == 1 and metrics["precision.shipper_ar"] == 1
    assert metrics["flags.risk"] == 0 and "precision.risk" not in metrics  # nothing to divide by: not written


def test_headline_names_match_the_dashboard_scoreboard():
    source = (pathlib.Path(__file__).parents[1] / "web/components/operations.tsx").read_text()
    answers, rows = fixture()
    metrics = evaluator.scoreboard(evaluator.score(answers, rows), rows)
    for name in ("precision", "recall", "dollars_caught", "false_holds", "median_resolution_s", "calls_placed"):
        assert f'"{name}"' in source and name in metrics


def test_empty_inputs_write_counts_but_no_undefined_ratios():
    metrics = evaluator.scoreboard(evaluator.score([answer("L1", "clean")], {}), {})
    assert metrics["loads_unseen"] == 1 and metrics["false_holds"] == 0
    assert not {"precision", "recall", "coverage", "median_resolution_s", "false_hold_rate"} & metrics.keys()


@pytest.mark.parametrize("tasks", [
    # The root decided, but a delegated child is still parked on a call.
    [task("r", "CASE-L1", AP, "hold"), task("c", "CASE-L1", "verify_claim", state="input-required", parent="r")],
    # Delivery evidence ahead of the bill is not a settlement decision.
    [task("r", "CASE-L1", AP, "unverified", findings={"awaiting_invoice": True})],
    [task("r", "CASE-L1", AP, state="working")],
    [task("r", "CASE-L1", AP, state="submitted")],
    [task("c", "CASE-L1", "review_hold", "hold", parent="x")],  # only a child: no lane decision
    [],
])
def test_in_flight_loads_are_pending_not_scored(tasks):
    rows = {"case": [case("CASE-L1", "L1")], "task": tasks}
    [load] = evaluator.score([answer("L1", "rate_mismatch", {"delta": 100})], rows)
    assert load.status == "pending"
    metrics = evaluator.scoreboard([load], rows)
    assert metrics["loads_pending"] == 1 and metrics["loads_scored"] == 0 and "recall" not in metrics


def test_latest_root_decision_wins_and_holds_flag_even_when_paid():
    rows = {"case": [case("CASE-L1", "L1")], "task": [
        task("a", "CASE-L1", AP, "approve", updated="2026-10-24T12:01:00Z"),
        task("b", "CASE-L1", AP, "reject", updated="2026-10-24T12:05:00Z")]}
    [load] = evaluator.score([answer("L1", "duplicate_invoice")], rows)
    assert load.decisions["carrier_ap"].task_id == "b" and load.flagged and load.caught
    rows["task"].reverse()
    rows["task"][0]["updated_at"] = "2026-10-24T12:00:00Z"  # now the approval is the newer decision
    [load] = evaluator.score([answer("L1", "duplicate_invoice")], rows)
    assert not load.flagged and not load.caught  # no proposal row: nothing shows it was paid once
    rows["hold"] = [{"id": "h", "case_id": "CASE-L1", "amount": 10}]
    [load] = evaluator.score([answer("L1", "duplicate_invoice")], rows)
    assert load.flagged and load.flagged_lanes == {"carrier_ap"}


@pytest.mark.parametrize(("outcome", "state", "findings", "expected"), [
    ("hold", "completed", {}, True), ("reject", "completed", {}, True), ("suspicious", "completed", {}, True),
    ("partial", "completed", {}, True), ("approve", "completed", {}, False), ("clear", "completed", {}, False),
    ("verified", "completed", {}, False), ("unverified", "completed", {"documents": []}, True),
    ("unverified", "completed", {"payment_blocked": False}, False),
    ("unverified", "completed", {"approval": []}, False),
    ("unverified", "completed", {"awaiting_invoice": True}, None), ("", "failed", {}, False),
])
def test_decision_classification(outcome, state, findings, expected):
    assert evaluator.classify_decision(task("t", "C", AP, outcome, state=state, findings=findings)) is expected


def test_bank_change_lane_and_case_id_join():
    rows = {"case": [case("CASE-L6", "L6"), case("CASE-L6-BANK", "", 9400)],
            "task": [task("ap", "CASE-L6", AP, "approve"), task("bk", "CASE-L6-BANK", BANK, "suspicious")]}
    [load] = evaluator.score([answer("L6", "bank_change")], rows)
    assert load.status == "scored" and load.flagged_lanes == {"risk"} and load.at_risk_usd == 9400
    # Without the bank screen the load waits: `risk` is the lane the answer key expects.
    [load] = evaluator.score([answer("L6", "bank_change")], {**rows, "task": rows["task"][:1]})
    assert load.status == "pending"


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
    generator.record_answer(load, "fsc_error", {"billed": 412.5, "contract": 380.0})
    [entry] = evaluator.read_answer_key(evaluator.answer_key_path())
    rows = {"case": [case("CASE-LD-1", "LD-1")], "task": [task("t", "CASE-LD-1", AP, "hold")]}
    [scored] = evaluator.score([entry], rows)
    assert scored.caught and scored.at_risk_usd == 32.5


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
    metrics, written = await evaluator.run_pass(key, client=client)
    assert set(written) == set(metrics) and len(server.calls) == len(metrics)
    assert {r["name"]: r["value"] for r in server.rows["metric"]} == metrics
    again, written = await evaluator.run_pass(key, client=client)
    assert again == metrics and written == [] and len(server.calls) == len(metrics)
    # One more decided load changes only the figures it moves.
    server.rows["task"].append(task("t9r", "CASE-L9-AR", AR, "verified"))
    _, written = await evaluator.run_pass(key, client=client)
    assert "loads_scored" in written and "precision" not in written and "calls_placed" not in written
    dry, written = await evaluator.run_pass(key, client=client, dry_run=True)
    assert written == [] and dry["loads_scored"] == 8


async def test_cli_offline_rows_prints_and_never_connects(tmp_path, capsys, monkeypatch):
    answers, rows = fixture()
    (tmp_path / "answer_key.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.setattr(evaluator, "_client", lambda: pytest.fail("offline scoring must not build a client"))
    args = evaluator.parse_args(["--rows", str(tmp_path / "rows.json"), "--answer-key",
                                 str(tmp_path / "answer_key.jsonl"), "--json"])
    assert await evaluator.main_async(args) == 0
    assert json.loads(capsys.readouterr().out)["recall"] == 0.75


async def test_cli_refuses_disabled_spacetime_and_missing_token(tmp_path, monkeypatch, capsys):
    (tmp_path / "answer_key.jsonl").write_text("")
    args = evaluator.parse_args(["--once", "--answer-key", str(tmp_path / "answer_key.jsonl")])
    assert await evaluator.main_async(args) == 2
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    assert await evaluator.main_async(args) == 2
    assert "SPACETIME_TOKEN_EVALUATOR" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        evaluator.parse_args(["--loop", "0"])
