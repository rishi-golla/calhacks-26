"""Imported company history (sim/history.py, scripts/seed_history.py): balanced books, past dates, plausible KPIs,
determinism, marked rows and an idempotent, removable seed."""
import hashlib
import json
import os
import pathlib
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from scripts import seed_history
from sim import history
from sim.world import offline_world

END = date(2026, 10, 24)


@pytest.fixture(scope="module")
def world() -> dict:
    return offline_world()


@pytest.fixture(scope="module")
def plan(world) -> history.History:
    return history.generate(world, seed=7, end=END)


def cents(value) -> int:
    return round(float(value) * 100)


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def test_every_journal_is_balanced_on_known_accounts(plan):
    assert len(plan.ledger_entries) > 1000
    for entry in plan.ledger_entries:
        lines = entry["lines"]
        assert len(lines) >= 2
        assert all((cents(x["debit"]) > 0) != (cents(x["credit"]) > 0) for x in lines), entry["id"]
        assert sum(cents(x["debit"]) for x in lines) == sum(cents(x["credit"]) for x in lines), entry["id"]
        assert {x["account"] for x in lines} <= set(history.ACCOUNT_CODES)
        assert entry["period"] == entry["posted_at"][:7]


def test_nothing_is_dated_after_the_end_of_the_window(plan):
    end_at = plan.end_at
    assert end_at == datetime(2026, 10, 24, 23, 30, tzinfo=UTC)
    stamps = [e["posted_at"] for e in plan.ledger_entries] + [i["issued_at"] for i in plan.invoices]
    stamps += [s for c in plan.cases for s in (c["opened_at"], c["resolved_at"])]
    stamps += [r["updated_at"] for r in plan.source_records if "updated_at" in r]
    assert all(parse(s) <= end_at for s in stamps)
    assert all(c["opened_at"] <= c["resolved_at"] for c in plan.cases)
    for record in plan.source_records[:-1]:
        for deposit in json.loads(record["record_json"])["deposits"]:
            assert parse(deposit["posted_at"]) <= end_at
    first = min(c["opened_at"] for c in plan.cases)
    assert parse(first) >= end_at - timedelta(days=90)
    assert plan.opening["posted_at"] < first


def test_finance_kpis_come_out_positive_and_plausible(plan):
    totals = history.kpis(plan.ledger_entries, plan.end_at)
    assert totals["cash"] > 0 and totals["lowest_cash"] >= 150_000
    assert totals["ar"] > 0 and totals["ap"] > 0
    assert totals["revenue_mtd"] > 50_000
    assert 8 < totals["gross_margin_pct"] < 20
    # Days sales outstanding of a Net 30 book.
    assert 25 < totals["ar"] / (totals["revenue_30d"] / 30) < 50
    assert json.loads(plan.manifest["record_json"])["totals_at_end"] == totals


def test_subledgers_reconcile_with_the_journals(plan):
    by_id = {e["id"]: e for e in plan.ledger_entries}
    ar = ap = 0
    for invoice in plan.invoices:
        load = invoice["load_id"]
        pay = by_id.get(f"HIST-JE-{load}-PAY")
        assert (invoice["status"] == "paid") == (pay is not None)
        if pay:
            assert cents(invoice["paid_amount"]) == sum(cents(x["debit"]) for x in pay["lines"])
        cost = by_id[f"HIST-JE-{load}-COST"]
        ap += sum(cents(x["credit"]) for x in cost["lines"]) - (cents(invoice["paid_amount"]) if pay else 0)
    for record in (json.loads(r["record_json"]) for r in plan.source_records[:-1]):
        if record["case_id"].endswith("-AR"):
            continue
        billed = cents(record["shipper_invoice_amount"])
        received = sum(cents(d["amount"]) for d in record["deposits"])
        ar += billed - received - cents(record.get("deduction_written_off") or 0)
    totals = history.kpis(plan.ledger_entries, plan.end_at)
    assert cents(totals["ar"]) == ar
    assert cents(totals["ap"]) == ap


def test_generation_is_deterministic_for_a_seed(world, plan):
    again = history.generate(world, seed=7, end=END)
    assert history.batches(again) == history.batches(plan)
    other = history.generate(world, seed=8, end=END)
    assert {c["case_id"] for c in other.cases}.isdisjoint({c["case_id"] for c in plan.cases})
    assert other.manifest["digest"] != plan.manifest["digest"]


def test_imported_rows_are_marked_and_shaped_like_ingest(plan):
    assert all(c["case_id"].startswith("HIST-LD-") for c in plan.cases)
    assert all(e["id"].startswith("HIST-JE-") for e in plan.ledger_entries)
    assert all(e["case_id"] == "" or e["case_id"].startswith("HIST-") for e in plan.ledger_entries)
    assert all(i["case_id"].startswith("HIST-") for i in plan.invoices)
    assert [r["id"] for r in plan.source_records][-1] == history.MANIFEST_ID
    for record in plan.source_records:
        assert hashlib.sha256(record["record_json"].encode()).hexdigest() == record["digest"]
    payable = next(c for c in plan.cases if " invoice on " in c["title"])
    assert payable["title"].endswith(f" invoice on {payable['load_id']}")
    record = json.loads(next(r for r in plan.source_records if r["id"] == f"case:{payable['case_id']}")["record_json"])
    for key in ("documents", "evidence_refs", "deposits", "carrier", "bol", "contract_rate", "invoice_amount",
                "invoice_id", "shipper_invoice_id", "shipper_invoice_amount", "line_items", "required_documents"):
        assert key in record, key
    assert record["imported"] is True and record["import_source"] == history.DEFAULT_SOURCE
    assert {d["kind"] for d in record["documents"]} >= {"rate_confirmation", "bill_of_lading", "pod", "carrier_invoice"}
    refs = {ref for ref, _, _ in plan.documents}
    assert all(d["ref"].removeprefix("doc:") in refs for d in record["documents"])
    assert record["shipper_invoice_id"] in refs


def test_history_carries_disputes_short_pays_aging_and_open_payables(plan):
    disputed = [i for i in plan.invoices if i["status"] == "paid" and cents(i["paid_amount"]) < cents(i["amount"])
                and cents(i["amount"]) > cents(i["contract_amount"])]
    assert len(disputed) == 2
    assert all(cents(i["paid_amount"]) == cents(i["contract_amount"]) for i in disputed)
    write_offs = [e for e in plan.ledger_entries if e["id"].endswith("-WO")]
    ar_cases = [c for c in plan.cases if c["case_id"].endswith("-AR")]
    assert len(write_offs) >= 3 and len(ar_cases) == len(write_offs)
    assert all(" short pay on " in c["title"] and c["exposure_usd"] > 0 for c in ar_cases)
    assert all(e["memo"].startswith("Short-pay write-off: SINV-") for e in write_offs)
    records = [json.loads(r["record_json"]) for r in plan.source_records[:-1] if not r["id"].endswith("-AR")]
    unpaid_age = [(plan.end_at - parse(r["issued_at"])).days for r in records if not r["deposits"]]
    assert any(age > 60 for age in unpaid_age) and any(30 < age <= 60 for age in unpaid_age)
    assert any(i["status"] == "open" for i in plan.invoices)
    assert {e["memo"].split(":")[0] for e in plan.ledger_entries} == {
        "Opening balance", "Customer billing", "Carrier settlement", "Matched source deposit", "Carrier payment",
        "Short-pay write-off"}


def test_volume_stays_within_what_the_dashboard_can_hold(world):
    plan = history.generate(world, seed=7, end=END)
    assert 300 <= len(plan.cases) <= 600
    fixed = history.generate(world, seed=7, end=END, loads=120)
    assert 110 <= len([c for c in fixed.cases if not c["case_id"].endswith("-AR")]) <= 120


def test_batches_fit_split_by_load_and_replay_the_same_write_ids(plan):
    payloads = history.batches(plan, 60_000)
    assert all(len(json.dumps(p, separators=(",", ":"))) <= 60_000 for p in payloads)
    assert payloads[0]["ledger_entries"][0]["id"] == "HIST-JE-OPENING"
    assert payloads[0]["source_records"][0]["id"] == history.MANIFEST_ID
    assert sum(r["id"] == history.MANIFEST_ID for p in payloads for r in p.get("source_records", [])) == 1
    assert len({p["write_id"] for p in payloads}) == len(payloads)
    assert all(p["write_id"].startswith(f"history:{plan.manifest['digest'][:16]}:") for p in payloads)
    assert all(len(p["write_id"]) <= 200 for p in payloads)
    assert not {p["write_id"] for p in payloads} & {p["write_id"] for p in history.batches(plan, 200_000)}
    assert history.batches(plan, 60_000) == payloads
    seen_companies: set[str] = set()
    cases = [c["case_id"] for p in payloads for c in p.get("cases", [])]
    assert sorted(cases) == sorted(c["case_id"] for c in plan.cases)
    for payload in payloads:
        assert set(payload) <= {"write_id", "source", "companies", "cases", "invoices", "ledger_entries", "source_records"}
        seen_companies.update(c["id"] for c in payload.get("companies", []))
        here = {c["case_id"] for c in payload.get("cases", [])}
        assert all(e["case_id"] in here or e["case_id"] == "" for e in payload.get("ledger_entries", []))
        assert all(i["carrier_id"] in seen_companies for i in payload.get("invoices", []))


class FakeTarget:
    """Holds the rows the reducers would, and fails on demand."""

    label = "fake"
    batch_bytes = seed_history.LOCAL_BATCH_BYTES

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.cases: dict[str, dict] = {}
        self.manifest: dict | None = None
        self.fail_next = 0

    def call(self, reducer: str, payload: dict) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise httpx.ConnectTimeout("timed out")
        self.calls.append((reducer, payload))
        if reducer == seed_history.IMPORT_REDUCER:
            for case in payload.get("cases", []):
                self.cases[case["case_id"]] = case
            for record in payload.get("source_records", []):
                if record["id"] == history.MANIFEST_ID:
                    self.manifest = record
        else:
            for case_id in sorted(self.cases)[: payload["limit"]]:
                del self.cases[case_id]
            if not self.cases:
                self.manifest = None

    def query(self, sql: str) -> list[dict]:
        if '"source_record"' in sql:
            return [self.manifest] if self.manifest else []
        if '"case"' in sql:
            return list(self.cases.values())
        return []

    def close(self) -> None:
        pass


def test_seed_is_idempotent_refuses_another_history_and_undoes(world, plan):
    target = FakeTarget()
    first = seed_history.seed(target, plan, 200_000)
    ids = [p["write_id"] for _, p in target.calls]
    assert first == len(ids) and len(target.cases) == len(plan.cases)
    seed_history.seed(target, plan, 200_000)
    assert [p["write_id"] for _, p in target.calls[first:]] == ids
    other = history.generate(world, seed=8, end=END)
    with pytest.raises(SystemExit, match="--undo"):
        seed_history.seed(target, other, 200_000)
    calls = seed_history.undo(target, limit=100)
    assert calls == -(-len(plan.cases) // 100)
    assert not target.cases and target.manifest is None


def test_a_transport_failure_resends_the_identical_batch(plan):
    target = FakeTarget()
    target.fail_next = 2
    payload = history.batches(plan)[0]
    seed_history.call(target, seed_history.IMPORT_REDUCER, payload, sleep=lambda _: None)
    assert target.calls == [(seed_history.IMPORT_REDUCER, payload)]

    class Refusing(FakeTarget):
        def call(self, reducer, payload):
            self.calls.append((reducer, payload))
            raise seed_history.Refused("530: journal is not balanced")

    refusing = Refusing()
    with pytest.raises(seed_history.Refused):
        seed_history.call(refusing, seed_history.IMPORT_REDUCER, payload, sleep=lambda _: None)
    assert len(refusing.calls) == 1


def test_cli_dry_run_writes_nothing_and_refuses_an_end_that_is_not_past(tmp_path, monkeypatch, capsys, world):
    path = tmp_path / "world.json"
    path.write_text(json.dumps(world))
    monkeypatch.setenv("WORLD_FILE", str(path))
    monkeypatch.setattr(seed_history, "connect", lambda *a: pytest.fail("dry run must not connect"))
    seed_history.main(["--dry-run", "--end", END.isoformat()])
    out = capsys.readouterr().out
    assert "resolved cases" in out and "nothing written" in out
    with pytest.raises(SystemExit, match="before today"):
        seed_history.main(["--dry-run", "--end", datetime.now(UTC).date().isoformat()])


def test_a_partial_run_then_another_history_is_refused(world, plan):
    class Interrupted(FakeTarget):
        def call(self, reducer, payload):
            if len(self.calls) == 1:
                raise seed_history.Refused("530: connection reset mid-run")
            super().call(reducer, payload)

    target = Interrupted()
    with pytest.raises(seed_history.Refused):
        seed_history.seed(target, plan, 60_000)
    assert target.cases and target.manifest is not None, "the first batch carries the manifest"
    with pytest.raises(SystemExit, match="different history"):
        seed_history.seed(target, history.generate(world, seed=8, end=END), 60_000)
    with pytest.raises(SystemExit, match="different history"):
        seed_history.seed(target, history.generate(world, seed=7, end=END, loads=100), 60_000)


def test_imported_cases_without_a_manifest_must_belong_to_the_plan(plan):
    target = FakeTarget()
    target.cases["HIST-LD-0101-ABCDEF"] = {"case_id": "HIST-LD-0101-ABCDEF"}
    with pytest.raises(SystemExit, match="not part of this history"):
        seed_history.seed(target, plan, 200_000)
    mine = FakeTarget()
    first = plan.cases[0]
    mine.cases[first["case_id"]] = first
    seed_history.seed(mine, plan, 200_000)
    assert len(mine.cases) == len(plan.cases)


def test_a_later_rerun_and_undo_use_the_stored_history(world, plan, tmp_path, monkeypatch):
    path = tmp_path / "world.json"
    path.write_text(json.dumps(world))
    monkeypatch.setenv("WORLD_FILE", str(path))
    target = FakeTarget()
    seed_history.seed(target, plan, seed_history.LOCAL_BATCH_BYTES)
    first = [p["write_id"] for _, p in target.calls]
    monkeypatch.setattr(seed_history, "connect", lambda *a: target)
    # No parameters given: the stored end date (not today's default) decides, so this is a no-op replay.
    seed_history.main(["--server", "http://127.0.0.1:1", "--database", "x"])
    assert [p["write_id"] for _, p in target.calls[len(first):]] == first
    with pytest.raises(SystemExit, match="different history"):
        seed_history.main(["--server", "http://127.0.0.1:1", "--database", "x", "--seed", "8"])
    docs = tmp_path / "docs"
    docs.mkdir()
    for ref, _, _ in plan.documents[:3]:
        (docs / f"{ref}.pdf").write_text("pdf")
    seed_history.main(["--server", "http://127.0.0.1:1", "--database", "x", "--undo", "--docs", "--data-dir", str(tmp_path)])
    assert not target.cases and target.manifest is None
    assert not list(docs.iterdir()), "undo removed the stored history's documents"


def test_the_cloud_path_caps_batches_and_surfaces_refusals(monkeypatch):
    from scripts import spacetime_cloud

    class Admin:
        def call(self, actor, reducer, payload):
            raise spacetime_cloud.CloudReducerRefused("Cloud admin reducer x refused: journal is not balanced")

    target = seed_history.CloudTarget.__new__(seed_history.CloudTarget)
    target.admin = Admin()
    with pytest.raises(seed_history.Refused, match="not balanced"):
        seed_history.call(target, seed_history.IMPORT_REDUCER, {}, sleep=lambda _: None)
    assert seed_history.CLOUD_BATCH_BYTES <= seed_history.CLOUD_MAX_BATCH_BYTES
    # The doubly escaped CLI argument of the largest allowed batch stays under Linux's 128 KiB argument cap.
    plan = history.generate(offline_world(), seed=7, end=END)
    for payload in history.batches(plan, seed_history.CLOUD_MAX_BATCH_BYTES):
        argument = json.dumps(json.dumps(payload, separators=(",", ":")))
        assert len(argument.encode()) < 128 * 1024


MODULE_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "spacetime/module/spacetimedb/test/fixtures/history-batches.json"


def module_fixture() -> dict:
    """Real generator output for the module's reducer test: a small history at two batch sizes."""
    plan = history.generate(offline_world(), seed=7, end=date(2026, 10, 20), days=30, loads=10)
    return {"generated_by": "tests/test_seed_history.py (UPDATE_HISTORY_FIXTURE=1 rewrites it)",
            "large": history.batches(plan, 200_000), "small": history.batches(plan, 12_000)}


def test_the_module_fixture_is_current_generator_output():
    expected = module_fixture()
    if os.getenv("UPDATE_HISTORY_FIXTURE"):
        MODULE_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        MODULE_FIXTURE.write_text(json.dumps(expected, indent=1) + "\n")
    assert json.loads(MODULE_FIXTURE.read_text()) == expected
    assert len(expected["small"]) > len(expected["large"]) >= 1
