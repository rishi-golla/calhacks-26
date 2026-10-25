"""The cutover checklist is tested against an in-memory SpacetimeDB SQL endpoint; nothing leaves the process."""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from common.config import AGENTS
from scripts import cloud_verify as verify
from scripts.spacetime_cloud import DATABASE, SERVER, accounting_periods

REAL_READER = verify.Reader
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
QUERY = re.compile(r'^SELECT (?P<cols>.+?) FROM "?(?P<table>\w+)"?(?: WHERE (?P<where>.+))?$')
CONDITION = re.compile(r"^(\w+) (=|!=) '((?:[^']|'')*)'$")


def stamp(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


class FakeSql:
    """Serves the module's fixed SELECTs: projection, ``col = 'v'`` / ``col != 'v'`` joined by AND."""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.snapshots: dict[str, list[list[dict]]] = {}
        self.reads: dict[str, int] = {}
        self.requests: list[httpx.Request] = []
        self.queries: list[str] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/v1/database/{DATABASE}/sql"
        assert "authorization" not in request.headers
        sql = request.content.decode()
        assert sql.startswith("SELECT")
        self.queries.append(sql)
        match = QUERY.fullmatch(sql)
        assert match, sql
        table = match["table"]
        if table in self.snapshots:
            index = self.reads.get(table, 0)
            self.reads[table] = index + 1
            rows = self.snapshots[table][min(index, len(self.snapshots[table]) - 1)]
        else:
            rows = self.tables[table]
        for condition in (match["where"] or "").split(" AND "):
            if condition:
                column, operator, value = CONDITION.fullmatch(condition).groups()
                value = value.replace("''", "'")
                rows = [row for row in rows if (row[column] == value) == (operator == "=")]
        columns = match["cols"].split(", ")
        return httpx.Response(200, json=[{
            "schema": {"elements": [{"name": {"some": column}} for column in columns]},
            "rows": [[row[column] for column in columns] for row in rows]}])

    def reader(self):
        return REAL_READER(SERVER, DATABASE, transport=self.transport)


def agent_rows(seconds_ago=10, port_base=9100):
    return [{"name": name, "status": "online", "card_url": f"http://127.0.0.1:{port_base + index}",
             "last_seen": stamp(seconds_ago)} for index, name in enumerate(AGENTS)]


def outbox(event, *, status="done", created=300.0, waited=1.2, case="CASE-1", error=""):
    return {"id": f"first_hop:{event}", "case_id": case, "kind": "first_hop", "status": status, "attempts": 1,
            "last_error": error, "created_at": stamp(created), "updated_at": stamp(created - waited)}


def dispatch(event, *, case="CASE-1", status="accepted", task="T1", created=300.0, waited=1.2):
    return {"event_id": event, "case_id": case, "agent": "carrier_ap", "skill": "review", "task_id": task,
            "status": status, "ts": stamp(created - waited)}


def healthy():
    return {
        "agent_config": [{"agent": name, "model": "auto", "enabled": True} for name in AGENTS],
        "agent": agent_rows(),
        "accounting_period": [{"period": p, "status": "open"} for p in accounting_periods(NOW.date())],
        "source_record": [
            {"id": "case:CASE-1", "updated_at": stamp(310),
             "record_json": json.dumps({"carrier": {"id": "C1"}, "shipper_id": "S1"})},
            *({"id": f"{prefix}:{party}", "updated_at": stamp(305), "record_json": "{}"}
              for prefix in ("company", "contacts") for party in ("C1", "S1"))],
        "case": [{"case_id": "CASE-1", "status": "open", "opened_at": stamp(309)}],
        "dispatch": [dispatch("E1")],
        "routing_outbox": [outbox("E1")],
        "task": [{"task_id": "T1", "case_id": "CASE-1", "agent": "carrier_ap", "skill": "review",
                  "a2a_state": "working", "started_at": stamp(297)}],
        "delegation_edge": [],
    }


def run_check(check, tables, *args, **kwargs):
    fake = FakeSql(tables)
    reader = fake.reader()
    try:
        return check(reader, *args, **kwargs), fake
    finally:
        reader.close()


# --- agent_config ---------------------------------------------------------------------------------

def test_agent_config_passes_with_all_nine_including_documents():
    check, fake = run_check(verify.check_agent_config, healthy())
    assert check.status == "PASS"
    assert "9/9" in check.title
    assert "documents present" in check.title
    assert fake.requests


@pytest.mark.parametrize("change", ["missing", "disabled"])
def test_agent_config_fails_when_documents_is_missing_or_disabled(change):
    tables = healthy()
    if change == "missing":
        tables["agent_config"] = [row for row in tables["agent_config"] if row["agent"] != "documents"]
    else:
        next(row for row in tables["agent_config"] if row["agent"] == "documents")["enabled"] = False
    check, _ = run_check(verify.check_agent_config, tables)
    assert check.status == "FAIL"
    if change == "missing":
        assert "documents MISSING" in check.title
        assert "documents: MISSING" in check.details
    else:
        assert "documents: model auto, DISABLED" in check.details


# --- heartbeats -----------------------------------------------------------------------------------

def test_heartbeat_without_wait_uses_last_seen_age():
    sleeps = []
    check, _ = run_check(verify.check_heartbeats, healthy(), 0, sleep=sleeps.append, clock=lambda: NOW)
    assert check.status == "PASS"
    assert not sleeps
    tables = healthy()
    tables["agent"] = agent_rows(seconds_ago=45)
    assert run_check(verify.check_heartbeats, tables, 0, clock=lambda: NOW)[0].status == "PASS"
    tables["agent"] = agent_rows(seconds_ago=46)
    stale, _ = run_check(verify.check_heartbeats, tables, 0, clock=lambda: NOW)
    assert stale.status == "FAIL"
    assert "0/9" in stale.title


def run_wait(first, second):
    fake = FakeSql(healthy())
    fake.snapshots["agent"] = [first, second]
    sleeps = []
    reader = fake.reader()
    try:
        return verify.check_heartbeats(reader, 20, sleep=sleeps.append, clock=lambda: NOW), sleeps
    finally:
        reader.close()


def test_heartbeat_wait_passes_when_last_seen_advances():
    check, sleeps = run_wait(agent_rows(seconds_ago=30), agent_rows(seconds_ago=10))
    assert sleeps == [20]
    assert check.status == "PASS"
    assert all(detail.endswith("advanced") for detail in check.details[:-1])


def test_heartbeat_wait_fails_when_last_seen_does_not_advance():
    rows = agent_rows(seconds_ago=30)
    check, _ = run_wait(rows, [dict(row) for row in rows])
    assert check.status == "FAIL"
    assert "NOT advancing" in check.details[0]


def test_heartbeat_fails_when_card_url_moves_between_reads():
    second = agent_rows(seconds_ago=10)
    second[0]["card_url"] = "http://127.0.0.1:9201"
    check, _ = run_wait(agent_rows(seconds_ago=30), second)
    assert check.status == "FAIL"
    assert "8/9" in check.title
    assert any("two fleets" in detail and "9100" in detail and "9201" in detail for detail in check.details)


def test_heartbeat_fails_for_a_missing_agent_row():
    tables = healthy()
    tables["agent"] = [row for row in tables["agent"] if row["name"] != "risk"]
    check, _ = run_check(verify.check_heartbeats, tables, 0, clock=lambda: NOW)
    assert check.status == "FAIL"
    assert "risk: no agent row" in check.details


# --- accounting periods ---------------------------------------------------------------------------

def test_periods_pass_when_the_whole_window_is_open():
    check, _ = run_check(verify.check_periods, healthy(), date(2026, 10, 24))
    assert check.status == "PASS"
    assert check.title == "accounting periods 2026-10..2028-04: 19/19 open"


def test_periods_fail_for_missing_and_locked():
    tables = healthy()
    tables["accounting_period"] = [row for row in tables["accounting_period"] if row["period"] != "2027-02"]
    next(row for row in tables["accounting_period"] if row["period"] == "2026-12")["status"] = "locked"
    check, _ = run_check(verify.check_periods, tables, date(2026, 10, 24))
    assert check.status == "FAIL"
    assert "17/19 open" in check.title
    assert check.details == ["missing: 2027-02", "locked: 2026-12"]


def test_periods_window_follows_the_clock_date():
    tables = healthy()
    tables["accounting_period"] = [{"period": p, "status": "open"} for p in accounting_periods(date(2026, 12, 1))]
    check, _ = run_check(verify.check_periods, tables, date(2026, 12, 20))
    assert check.title.startswith("accounting periods 2026-12..2028-06")
    assert check.status == "PASS"


# --- case chain -----------------------------------------------------------------------------------

def test_case_chain_defaults_to_the_newest_queued_or_accepted_dispatch():
    tables = healthy()
    tables["dispatch"] += [dispatch("OLD", case="CASE-OLD", created=3000),
                           dispatch("BAD", case="CASE-BAD", status="failed", task="", created=10)]
    check, fake = run_check(verify.check_case_chain, tables, None)
    assert check.title == "case chain for CASE-1"
    assert check.status == "PASS"
    queried = " ".join(fake.queries)
    assert "CASE-BAD" not in queried


def test_case_chain_falls_back_to_any_dispatch_when_none_routed():
    tables = healthy()
    tables["dispatch"] = [dispatch("F1", status="failed", task="")]
    check, _ = run_check(verify.check_case_chain, tables, None)
    assert check.title == "case chain for CASE-1"


def test_case_chain_passes_with_every_link_in_time_order():
    check, fake = run_check(verify.check_case_chain, healthy(), "CASE-1")
    assert check.status == "PASS"
    queried = " ".join(fake.queries)
    for row_id in ("company:C1", "contacts:C1", "company:S1", "contacts:S1"):
        assert f"'{row_id}'" in queried
    assert not any(detail.startswith("MISSING") for detail in check.details)
    stamps = [detail.split()[0] for detail in check.details[:-1]]
    assert stamps == sorted(stamps)
    assert any("1.2 s after enqueue" in detail for detail in check.details)
    assert any("source_record case:CASE-1" in detail for detail in check.details)
    assert any("first_hop:E1 done after 1 attempt(s)" in detail for detail in check.details)
    assert check.details[-1] == "1 task row(s), 0 delegation edge(s)"


def test_case_chain_lists_every_missing_link():
    tables = healthy()
    for name in ("source_record", "case", "dispatch", "routing_outbox", "task"):
        tables[name] = []
    tables["dispatch"] = [dispatch("E9", case="CASE-2", status="queued", task="")]
    check, _ = run_check(verify.check_case_chain, tables, "CASE-1")
    assert check.status == "FAIL"
    missing = [detail for detail in check.details if detail.startswith("MISSING")]
    assert missing == ["MISSING: source_record case:CASE-1", "MISSING: case row",
                       "MISSING: accepted dispatch with a task id"]


def test_case_chain_fails_when_the_accepted_task_row_is_absent():
    tables = healthy()
    tables["task"][0]["task_id"] = "OTHER"
    check, _ = run_check(verify.check_case_chain, tables, "CASE-1")
    assert check.status == "FAIL"
    assert "MISSING: task row for the accepted dispatch" in check.details


def test_case_chain_notes_direct_dispatch_without_outbox_row():
    tables = healthy()
    tables["routing_outbox"] = []
    check, _ = run_check(verify.check_case_chain, tables, "CASE-1")
    assert any("no first_hop outbox row: direct A2A" in detail for detail in check.details)


@pytest.mark.parametrize("case_id", ["CASE 1", "CASE'1", "x;DROP TABLE y", "", "-leading", "a" * 70])
def test_invalid_case_id_is_rejected_before_any_request(case_id):
    fake = FakeSql(healthy())
    reader = fake.reader()
    try:
        with pytest.raises(ValueError, match="unsupported characters"):
            verify.check_case_chain(reader, case_id)
        assert not fake.requests
    finally:
        reader.close()


def test_case_chain_fails_when_there_are_no_dispatch_rows():
    tables = healthy()
    tables["dispatch"] = []
    check, _ = run_check(verify.check_case_chain, tables, None)
    assert check.status == "FAIL"
    assert "no dispatch rows" in check.title


# --- first hops -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("age", "status"), [(61, "FAIL"), (59, "PASS")])
def test_stale_first_hops_threshold(age, status):
    tables = healthy()
    tables["routing_outbox"] = [outbox("S1", status="pending", created=age, error="boom")]
    check, _ = run_check(verify.check_stale_first_hops, tables, NOW)
    assert check.status == status
    if status == "FAIL":
        assert check.details == [f"first_hop:S1: pending, 1 attempt(s), {age} s old, last_error boom"]


def test_done_first_hops_are_ignored_however_old():
    tables = healthy()
    tables["routing_outbox"] = [outbox("OLD", created=100000)]
    check, _ = run_check(verify.check_stale_first_hops, tables, NOW)
    assert check.status == "PASS"
    assert "0 not done" in check.title


def test_latency_counts_only_accepted_in_window_with_done_outbox():
    tables = healthy()
    tables["dispatch"] = [dispatch(f"L{n}", created=600, waited=n) for n in range(1, 6)] + [
        dispatch("OLD", created=7300, waited=9),          # accepted two hours ago: outside the window
        dispatch("OPEN", created=600, waited=8),          # its outbox row is not done
        dispatch("QUEUED", status="queued", task="", created=600, waited=7),
        dispatch("DIRECT", created=600, waited=6)]        # no outbox row at all
    tables["routing_outbox"] = [outbox(f"L{n}", created=600, waited=n) for n in range(1, 6)] + [
        outbox("OLD", created=7300, waited=9), outbox("OPEN", status="pending", created=600, waited=8),
        outbox("QUEUED", created=600, waited=7)]
    check, _ = run_check(verify.check_first_hop_latency, tables, NOW, 60)
    assert check.status == "INFO"
    assert "5 event(s)" in check.title
    assert check.details == ["min 1.00 s, median 3.00 s, p95 5.00 s, max 5.00 s"]


def test_latency_with_no_samples_is_informational():
    tables = healthy()
    tables["dispatch"] = []
    check, _ = run_check(verify.check_first_hop_latency, tables, NOW, 60)
    assert (check.status, check.details) == ("INFO", ["no accepted first hop in the window"])


def test_backlog_groups_by_kind_status_and_error():
    tables = healthy()
    tables["routing_outbox"] = [
        outbox("A", status="pending", error="boom"), outbox("B", status="pending", error="boom"),
        {**outbox("C", status="failed"), "id": "notify:C", "kind": "notify"}, outbox("D")]
    check, _ = run_check(verify.check_outbox_backlog, tables)
    assert check.status == "INFO"
    assert check.title == "outbox rows not done: 3"
    assert check.details == ["first_hop pending: 2", "notify failed: 1", "first_hop last_error 'boom': 2"]


def test_empty_backlog_reports_none():
    tables = healthy()
    tables["routing_outbox"] = []
    check, _ = run_check(verify.check_outbox_backlog, tables)
    assert check.details == ["none"]


# --- /readyz --------------------------------------------------------------------------------------

def test_readyz_passes_when_every_agent_answers_200():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"mode": "live"})

    check = verify.check_readyz(transport=httpx.MockTransport(handler))
    assert check.status == "PASS"
    assert seen == [f"{spec.url}/readyz" for spec in AGENTS.values()]


@pytest.mark.parametrize("failure", ["503", "connect"])
def test_readyz_fails_on_503_or_connection_error(failure):
    def handler(request):
        if request.url.port == AGENTS["risk"].url.rsplit(":", 1)[1] and failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        if str(request.url.port) == AGENTS["risk"].url.rsplit(":", 1)[1]:
            return httpx.Response(503, json={"reason": "no spacetime"})
        return httpx.Response(200)

    check = verify.check_readyz(transport=httpx.MockTransport(handler))
    assert check.status == "FAIL"
    assert "8/9" in check.title
    assert any("ConnectError" in detail or "503 no spacetime" in detail for detail in check.details)


# --- main, report and the reader ------------------------------------------------------------------

class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def patch_main(monkeypatch, fake):
    monkeypatch.setattr(verify, "datetime", FixedDatetime)
    monkeypatch.setattr(verify, "Reader", lambda server, database: fake.reader())


def test_main_returns_zero_and_prints_round_trip_summary_when_all_pass(monkeypatch, capsys):
    fake = FakeSql(healthy())
    patch_main(monkeypatch, fake)
    assert verify.main(["--heartbeat-wait", "0"]) == 0
    output = capsys.readouterr().out
    assert "All checks pass" in output
    assert re.search(r"\d+ SQL round trips: median \d+ ms, max \d+ ms", output)
    assert "[PASS] agent_config has 9/9 agents" in output
    assert all(request.method == "POST" for request in fake.requests)


def test_main_returns_one_when_any_check_fails(monkeypatch, capsys):
    tables = healthy()
    tables["agent_config"] = [row for row in tables["agent_config"] if row["agent"] != "documents"]
    patch_main(monkeypatch, FakeSql(tables))
    assert verify.main(["--heartbeat-wait", "0", "--case", "CASE-1"]) == 1
    output = capsys.readouterr().out
    assert "[FAIL] agent_config" in output
    assert "1 failing check(s)" in output


def test_report_lists_failures_and_round_trips():
    fake = FakeSql(healthy())
    reader = fake.reader()
    try:
        checks = verify.run(reader, heartbeat_wait=0, clock=lambda: NOW)
        assert [check.status for check in checks] == ["PASS"] * 4 + ["PASS", "INFO", "INFO"]
        text = verify.report(checks, reader, NOW)
        assert text.splitlines()[0] == f"FreightLine cutover verification: {SERVER} / {DATABASE}"
        assert f"{len(reader.timings)} SQL round trips" in text
    finally:
        reader.close()


def test_run_sleeps_through_the_injected_hook_and_adds_readyz():
    fake = FakeSql(healthy())
    fake.snapshots["agent"] = [agent_rows(seconds_ago=30), agent_rows(seconds_ago=10)]
    sleeps = []
    reader = fake.reader()
    try:
        checks = verify.run(reader, heartbeat_wait=7, readyz=True, sleep=sleeps.append, clock=lambda: NOW,
                            readyz_transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        assert sleeps == [7]
        assert checks[-1].title.startswith("agent /readyz")
        assert checks[-1].status == "PASS"
    finally:
        reader.close()


@pytest.mark.parametrize("sql", ["DELETE FROM agent", "UPDATE agent SET status = 'x'", "INSERT INTO agent VALUES (1)",
                                 "SELECTx FROM agent"])
def test_reader_refuses_anything_but_select_without_sending(sql):
    fake = FakeSql(healthy())
    reader = fake.reader()
    try:
        with pytest.raises(ValueError, match="SELECT"):
            reader.rows(sql)
        assert not fake.requests
        assert not reader.timings
    finally:
        reader.close()


def test_reader_surfaces_http_errors_without_the_where_clause():
    reader = verify.Reader(SERVER, DATABASE, transport=httpx.MockTransport(lambda request: httpx.Response(530)))
    try:
        with pytest.raises(RuntimeError, match=r"HTTP 530\): SELECT id FROM t$"):
            reader.rows("SELECT id FROM t WHERE id = 'secret'")
    finally:
        reader.close()
