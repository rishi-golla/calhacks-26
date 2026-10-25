"""Read-only cutover checklist for a FreightLine SpacetimeDB database.

    python -m scripts.cloud_verify                      # Maincloud at350-freightops
    python -m scripts.cloud_verify --server http://127.0.0.1:3000 --database freightops-t1-3d6644

Every check is an anonymous SELECT over the HTTP SQL endpoint: no token is sent and
nothing is written. ``--readyz`` additionally GETs each agent's /readyz on this
machine. Exits 1 when any check fails. See spacetime/VALIDATION.md (Maincloud cutover).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from common.config import AGENTS
from common.spacetime_client import canonical_sql_column
from scripts.spacetime_cloud import DATABASE, SERVER, accounting_periods

STALE_FIRST_HOP_SECONDS = 60
HEARTBEAT_SECONDS = 15
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


@dataclass
class Check:
    status: str  # PASS, FAIL or INFO
    title: str
    details: list[str] = field(default_factory=list)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age(value: object, now: datetime) -> float | None:
    moment = _time(value)
    return None if moment is None else (now - moment).total_seconds()


def _seconds_between(start: object, end: object) -> float | None:
    first, last = _time(start), _time(end)
    return None if first is None or last is None else (last - first).total_seconds()


def _port(card_url: object) -> str:
    try:
        parsed = urlsplit(str(card_url))
        return f"{parsed.hostname}:{parsed.port}" if parsed.hostname else "?"
    except ValueError:
        return "?"


class Reader:
    """Anonymous, SELECT-only SQL reads; records each round trip for the latency line."""

    def __init__(self, server: str, database: str, *, transport: httpx.BaseTransport | None = None):
        self.server = server.rstrip("/")
        self.database = database
        self.client = httpx.Client(base_url=self.server, timeout=30, follow_redirects=False, transport=transport)
        self.timings: list[float] = []

    def rows(self, sql: str) -> list[dict]:
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("verification issues SELECT statements only")
        started = time.perf_counter()
        response = self.client.post(f"/v1/database/{quote(self.database, safe='')}/sql", content=sql)
        self.timings.append((time.perf_counter() - started) * 1000)
        if not response.is_success:
            raise RuntimeError(f"SQL read failed (HTTP {response.status_code}): {sql.split(' WHERE ')[0]}")
        result = response.json()[0]
        names = [canonical_sql_column(e["name"].get("some") if isinstance(e["name"], dict) else e["name"])
                 for e in result["schema"]["elements"]]
        return [dict(zip(names, row, strict=True)) for row in result["rows"]]

    def close(self) -> None:
        self.client.close()


def check_agent_config(reader: Reader) -> Check:
    rows = {row["agent"]: row for row in reader.rows("SELECT agent, model, enabled FROM agent_config")}
    missing = [name for name in AGENTS if name not in rows]
    disabled = [name for name in AGENTS if name in rows and not rows[name]["enabled"]]
    details = [f"{name}: model {rows[name]['model']}, {'enabled' if rows[name]['enabled'] else 'DISABLED'}"
               for name in AGENTS if name in rows]
    details += [f"{name}: MISSING" for name in missing]
    status = "PASS" if not missing and not disabled else "FAIL"
    return Check(status, f"agent_config has {len(AGENTS) - len(missing)}/{len(AGENTS)} agents"
                 f" (documents {'present' if 'documents' in rows else 'MISSING'})", details)


def _agents(reader: Reader) -> dict[str, dict]:
    return {row["name"]: row for row in reader.rows("SELECT name, status, card_url, last_seen FROM agent")}


def check_heartbeats(reader: Reader, wait: float, *, sleep=time.sleep, clock=lambda: datetime.now(UTC)) -> Check:
    first = _agents(reader)
    now = clock()
    second = first
    if wait > 0:
        sleep(wait)
        second = _agents(reader)
        now = clock()
    details, failing = [], []
    for name in AGENTS:
        row = second.get(name)
        if row is None:
            details.append(f"{name}: no agent row")
            failing.append(name)
            continue
        age = _age(row["last_seen"], now)
        earlier = first.get(name, {})
        then, latest = _time(earlier.get("last_seen")), _time(row["last_seen"])
        advanced = bool(then and latest and latest > then)
        moved = bool(earlier.get("card_url")) and earlier.get("card_url") != row["card_url"]
        line = (f"{name}: {row['status']}, card {_port(row['card_url'])}, last_seen "
                f"{'never' if age is None else f'{age:.0f} s ago'}")
        if wait > 0:
            line += ", advanced" if advanced else ", NOT advancing"
        if moved:
            line += f", card moved from {_port(earlier['card_url'])} (two fleets heartbeating?)"
        details.append(line)
        fresh = advanced if wait > 0 else age is not None and age <= 3 * HEARTBEAT_SECONDS
        if not fresh or moved:
            failing.append(name)
    hosts = sorted({_port(row["card_url"]).rsplit(":", 1)[0] for row in second.values()})
    ports = sorted({_port(row["card_url"]) for row in second.values()})
    details.append(f"card hosts {', '.join(hosts) or 'none'}; ports {', '.join(ports) or 'none'}")
    how = f"advancing over {wait:.0f} s" if wait > 0 else f"seen within {3 * HEARTBEAT_SECONDS} s"
    return Check("FAIL" if failing else "PASS",
                 f"agent heartbeats: {len(AGENTS) - len(failing)}/{len(AGENTS)} {how}", details)


def check_periods(reader: Reader, today) -> Check:
    window = accounting_periods(today)
    stored = {row["period"]: row["status"] for row in reader.rows("SELECT period, status FROM accounting_period")}
    missing = [period for period in window if period not in stored]
    locked = [period for period in window if stored.get(period, "open") != "open"]
    details = []
    if missing:
        details.append(f"missing: {', '.join(missing)}")
    if locked:
        details.append(f"locked: {', '.join(locked)}")
    open_count = len(window) - len(missing) - len(locked)
    return Check("PASS" if not missing and not locked else "FAIL",
                 f"accounting periods {window[0]}..{window[-1]}: {open_count}/{len(window)} open", details)


def _party(record: dict, side: str) -> str | None:
    """Counterparty id as SourceRepository derives it: ``<side>_id`` or ``<side>.id``."""
    value = record.get(f"{side}_id")
    nested = record.get(side)
    if not value and isinstance(nested, dict):
        value = nested.get("id")
    return value if isinstance(value, str) and value else None


def _latest_case(reader: Reader) -> str | None:
    rows = reader.rows("SELECT case_id, status, ts FROM dispatch")
    routed = [row for row in rows if row["status"] in {"queued", "accepted"}] or rows
    routed.sort(key=lambda row: _time(row["ts"]) or datetime.min.replace(tzinfo=UTC))
    return routed[-1]["case_id"] if routed else None


def check_case_chain(reader: Reader, case_id: str | None) -> Check:
    if case_id is None:
        case_id = _latest_case(reader)
        if case_id is None:
            return Check("FAIL", "case chain: no dispatch rows yet")
    if not _CASE_ID.fullmatch(case_id):
        raise ValueError("case id contains unsupported characters")
    events: list[tuple[str, str]] = []
    missing: list[str] = []
    source = reader.rows(f"SELECT id, record_json, updated_at FROM source_record WHERE id = {_quote('case:' + case_id)}")
    if source:
        events.append((source[0]["updated_at"], f"source_record case:{case_id}"))
        try:
            record = json.loads(source[0]["record_json"])
        except ValueError:
            record = {}
        parties = [_party(record, side) for side in ("carrier", "shipper")] if isinstance(record, dict) else []
        for party in dict.fromkeys(filter(None, parties)):
            for prefix in ("company", "contacts"):
                for row in reader.rows(f"SELECT id, updated_at FROM source_record WHERE id = {_quote(f'{prefix}:{party}')}"):
                    events.append((row["updated_at"], f"source_record {row['id']}"))
    else:
        missing.append(f"source_record case:{case_id}")
    case = reader.rows(f'SELECT case_id, status, opened_at FROM "case" WHERE case_id = {_quote(case_id)}')
    if case:
        events.append((case[0]["opened_at"], f"case opened ({case[0]['status']})"))
    else:
        missing.append("case row")
    dispatches = reader.rows("SELECT event_id, agent, skill, task_id, status, ts FROM dispatch"
                             f" WHERE case_id = {_quote(case_id)}")
    outbox = {row["id"]: row for row in reader.rows(
        "SELECT id, kind, status, attempts, last_error, created_at, updated_at FROM routing_outbox"
        f" WHERE case_id = {_quote(case_id)}")}
    for row in dispatches:
        hop = outbox.get(f"first_hop:{row['event_id']}")
        task = f" task {row['task_id']}" if row["task_id"] else ""
        if hop:
            events.append((hop["created_at"], f"first_hop:{row['event_id']} enqueued (dispatch queued)"))
            waited = _seconds_between(hop["created_at"], row["ts"]) if row["status"] == "accepted" else None
            task += "" if waited is None else f", {waited:.1f} s after enqueue"
            outcome = (f"first_hop:{row['event_id']} {hop['status']} after {hop['attempts']} attempt(s)"
                       + (f", last_error {hop['last_error']}" if hop["last_error"] else ""))
            events.append((hop["updated_at"], outcome))
        direct = "" if hop else " (no first_hop outbox row: direct A2A)"
        events.append((row["ts"], f"dispatch {row['event_id']} -> {row['agent']} {row['status']}{task}{direct}"))
    accepted = [row for row in dispatches if row["status"] == "accepted" and row["task_id"]]
    if not accepted:
        missing.append("accepted dispatch with a task id")
    tasks = reader.rows(f"SELECT task_id, agent, skill, a2a_state, started_at FROM task WHERE case_id = {_quote(case_id)}")
    for row in tasks:
        events.append((row["started_at"], f"task {row['agent']}.{row['skill']} {row['a2a_state']} {row['task_id']}"))
    if accepted and not {row["task_id"] for row in accepted} & {row["task_id"] for row in tasks}:
        missing.append("task row for the accepted dispatch")
    edges = reader.rows(f"SELECT from_agent, to_agent, skill, ts FROM delegation_edge WHERE case_id = {_quote(case_id)}")
    for row in edges:
        events.append((row["ts"], f"delegation {row['from_agent']} -> {row['to_agent']} ({row['skill']})"))
    events.sort(key=lambda item: _time(item[0]) or datetime.min.replace(tzinfo=UTC))
    details = [f"{stamp or '(no time)'}  {text}" for stamp, text in events]
    details += [f"MISSING: {item}" for item in missing]
    details.append(f"{len(tasks)} task row(s), {len(edges)} delegation edge(s)")
    return Check("FAIL" if missing else "PASS", f"case chain for {case_id}", details)


def check_stale_first_hops(reader: Reader, now: datetime, limit: float = STALE_FIRST_HOP_SECONDS) -> Check:
    rows = reader.rows("SELECT id, status, attempts, created_at, last_error FROM routing_outbox"
                       " WHERE kind = 'first_hop' AND status != 'done'")
    stale = sorted((row for row in rows if (_age(row["created_at"], now) or 0) > limit),
                   key=lambda row: row["created_at"])
    details = [f"{row['id']}: {row['status']}, {row['attempts']} attempt(s), "
               f"{_age(row['created_at'], now):.0f} s old, last_error {row['last_error'] or '-'}" for row in stale[:10]]
    if len(stale) > 10:
        details.append(f"... and {len(stale) - 10} more")
    return Check("FAIL" if stale else "PASS",
                 f"first_hop rows not done and older than {limit:.0f} s: {len(stale)} ({len(rows)} not done)", details)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def check_first_hop_latency(reader: Reader, now: datetime, window_minutes: float) -> Check:
    """Enqueue (outbox created_at) to accepted (dispatch ts), both stamped by the module's clock."""
    enqueued = {row["id"].removeprefix("first_hop:"): row["created_at"] for row in reader.rows(
        "SELECT id, created_at FROM routing_outbox WHERE kind = 'first_hop' AND status = 'done'")}
    samples = []
    for row in reader.rows("SELECT event_id, ts FROM dispatch WHERE status = 'accepted'"):
        age = _age(row["ts"], now)
        waited = _seconds_between(enqueued.get(row["event_id"]), row["ts"])
        if waited is not None and age is not None and age <= window_minutes * 60:
            samples.append(waited)
    title = f"enqueue-to-accept over first hops accepted in the last {window_minutes:.0f} min: {len(samples)} event(s)"
    if not samples:
        return Check("INFO", title, ["no accepted first hop in the window"])
    summary = (f"min {min(samples):.2f} s, median {statistics.median(samples):.2f} s,"
               f" p95 {_percentile(samples, 0.95):.2f} s, max {max(samples):.2f} s")
    return Check("INFO", title, [summary])


def check_outbox_backlog(reader: Reader) -> Check:
    rows = reader.rows("SELECT kind, status, last_error FROM routing_outbox WHERE status != 'done'")
    by_kind = Counter((row["kind"], row["status"]) for row in rows)
    errors = Counter((row["kind"], row["last_error"]) for row in rows if row["last_error"])
    details = [f"{kind} {status}: {count}" for (kind, status), count in sorted(by_kind.items())]
    details += [f"{kind} last_error {error!r}: {count}" for (kind, error), count in sorted(errors.items())]
    return Check("INFO", f"outbox rows not done: {len(rows)}", details or ["none"])


def check_readyz(*, transport: httpx.BaseTransport | None = None) -> Check:
    details, failing = [], 0
    with httpx.Client(timeout=15, transport=transport) as client:
        for name, spec in AGENTS.items():
            try:
                response = client.get(f"{spec.url}/readyz")
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                reason = body.get("reason") or body.get("mode") or ""
                details.append(f"{name} {spec.url}: {response.status_code} {reason}".rstrip())
                failing += response.status_code != 200
            except httpx.HTTPError as error:
                details.append(f"{name} {spec.url}: {type(error).__name__}")
                failing += 1
    return Check("FAIL" if failing else "PASS", f"agent /readyz: {len(AGENTS) - failing}/{len(AGENTS)} ready", details)


def run(reader: Reader, *, case_id: str | None = None, heartbeat_wait: float = 20, readyz: bool = False,
        latency_window: float = 60, sleep=time.sleep, clock=lambda: datetime.now(UTC),
        readyz_transport=None) -> list[Check]:
    checks = [check_agent_config(reader), check_heartbeats(reader, heartbeat_wait, sleep=sleep, clock=clock),
              check_periods(reader, clock().date()), check_case_chain(reader, case_id),
              check_stale_first_hops(reader, clock()), check_first_hop_latency(reader, clock(), latency_window),
              check_outbox_backlog(reader)]
    if readyz:
        checks.append(check_readyz(transport=readyz_transport))
    return checks


def report(checks: list[Check], reader: Reader, started: datetime) -> str:
    lines = [f"FreightLine cutover verification: {reader.server} / {reader.database}",
             f"at {started.isoformat(timespec='seconds')}; anonymous SELECTs only, nothing written", ""]
    for item in checks:
        lines.append(f"[{item.status}] {item.title}")
        lines += [f"       {detail}" for detail in item.details]
    if reader.timings:
        lines += ["", (f"{len(reader.timings)} SQL round trips: median {statistics.median(reader.timings):.0f} ms,"
                       f" max {max(reader.timings):.0f} ms")]
    failed = [item.title for item in checks if item.status == "FAIL"]
    lines.append(f"{len(failed)} failing check(s)" if failed else "All checks pass")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=SERVER)
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--case", dest="case_id", help="case to trace (default: case of the newest routed dispatch)")
    parser.add_argument("--heartbeat-wait", type=float, default=20,
                        help="seconds between two agent reads; 0 checks last_seen age only (default 20)")
    parser.add_argument("--latency-window", type=float, default=60,
                        help="minutes of accepted first hops to measure enqueue-to-accept over (default 60)")
    parser.add_argument("--readyz", action="store_true", help="also GET /readyz on each agent URL from this machine")
    args = parser.parse_args(argv)
    started = datetime.now(UTC)
    reader = Reader(args.server, args.database)
    try:
        checks = run(reader, case_id=args.case_id, heartbeat_wait=args.heartbeat_wait, readyz=args.readyz,
                     latency_window=args.latency_window)
        print(report(checks, reader, started))
    finally:
        reader.close()
    return 1 if any(item.status == "FAIL" for item in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
