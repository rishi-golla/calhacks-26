"""Local ops store (SQLite, shared by all agent processes on one host).

Holds what the A2A task store doesn't: parked-task addressing (calls, emails, approvals), the trace of
agent events (the Validator's friction source), Git-style audit change sets, agent memory, versioned
instructions, reviews and human flags. Every write is mirrored to SpacetimeDB by `common.mirror`
when SPACETIME_DISABLED=0 (W2 owns those live tables; this file is the fallback and the test fixture).
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_state (task_id TEXT PRIMARY KEY, agent TEXT, env TEXT, meta TEXT,
    state TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS parked (key TEXT PRIMARY KEY, kind TEXT, agent TEXT, task_id TEXT,
    context_id TEXT, case_id TEXT, payload TEXT, created REAL, resolved REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, case_id TEXT,
    agent TEXT, task_id TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS change_sets (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, ts REAL,
    case_id TEXT, agent TEXT, task_id TEXT, action TEXT, policy_ids TEXT, instruction_version INTEGER,
    memory_ids TEXT, sources TEXT, cells TEXT, parent_id TEXT, reverts TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS sheet_cells (sheet TEXT, row TEXT, col TEXT, value TEXT, change_set_id TEXT,
    PRIMARY KEY (sheet, row, col));
CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, ts REAL, agent TEXT, scope TEXT, text TEXT,
    source TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS instructions (agent TEXT, version INTEGER, ts REAL, patch TEXT, author TEXT,
    reason TEXT, reverted INTEGER DEFAULT 0, PRIMARY KEY (agent, version));
CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, ts REAL, case_id TEXT, agent TEXT, task_id TEXT,
    verdict TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS flags (id TEXT PRIMARY KEY, ts REAL, target_type TEXT, target_id TEXT,
    case_id TEXT, agent TEXT, note TEXT, by TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL);
"""


def _path() -> str:
    return os.getenv("OPS_DB", "data/ops.db")


def connect() -> sqlite3.Connection:
    Path(_path()).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_path(), timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def _j(v: Any) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def _rows(cur) -> list[dict]:
    out = []
    for r in cur.fetchall():
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    d[k] = json.loads(v)
                except ValueError:
                    pass
        out.append(d)
    return out


# ---- task continuation state (survives agent restarts) ----
def save_task_state(task_id: str, agent: str, env: dict, meta: dict, state: dict) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO task_state VALUES (?,?,?,?,?,?)",
                  (task_id, agent, _j(env), _j(meta), _j(state), time.time()))


def load_task_state(task_id: str) -> dict | None:
    with connect() as c:
        rows = _rows(c.execute("SELECT * FROM task_state WHERE task_id=?", (task_id,)))
    return rows[0] if rows else None


# ---- parked work addressed by external keys (call id, email id, approval id) ----
def park(key: str, kind: str, agent: str, task_id: str, context_id: str, case_id: str,
         payload: dict | None = None) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO parked VALUES (?,?,?,?,?,?,?,?,NULL)",
                  (key, kind, agent, task_id, context_id, case_id, _j(payload or {}), time.time()))


def take_parked(key: str) -> dict | None:
    """Atomically claim a parked entry so a duplicate webhook can't resume twice."""
    with connect() as c:
        cur = c.execute("UPDATE parked SET resolved=? WHERE key=? AND resolved IS NULL", (time.time(), key))
        if cur.rowcount == 0:
            return None
        return _rows(c.execute("SELECT * FROM parked WHERE key=?", (key,)))[0]


def list_parked(kind: str | None = None, include_resolved: bool = False) -> list[dict]:
    q, a = "SELECT * FROM parked WHERE 1=1", []
    if kind:
        q, a = q + " AND kind=?", [kind]
    if not include_resolved:
        q += " AND resolved IS NULL"
    with connect() as c:
        return _rows(c.execute(q + " ORDER BY created", a))


# ---- trace events ----
def log_event(case_id: str, agent: str, task_id: str | None, kind: str, **detail) -> None:
    with connect() as c:
        c.execute("INSERT INTO events (ts, case_id, agent, task_id, kind, detail) VALUES (?,?,?,?,?,?)",
                  (time.time(), case_id, agent, task_id, kind, _j(detail)))


def events(case_id: str | None = None, agent: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM events WHERE 1=1", []
    if case_id:
        q, a = q + " AND case_id=?", [*a, case_id]
    if agent:
        q, a = q + " AND agent=?", [*a, agent]
    with connect() as c:
        return _rows(c.execute(q + " ORDER BY id", a))


# ---- Git-style audit ----
def commit_change_set(*, case_id: str, agent: str, task_id: str | None, action: str,
                      cells: list[dict], policy_ids: list[str] | None = None, sources: list[str] | None = None,
                      instruction_version: int = 0, memory_ids: list[str] | None = None,
                      reverts: str | None = None) -> dict:
    """cells: [{sheet,row,col,after}] -> before is read from the current sheet state. Hash-chained."""
    cs_id = f"cs_{uuid.uuid4().hex[:10]}"
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            last = c.execute("SELECT id, hash FROM change_sets ORDER BY seq DESC LIMIT 1").fetchone()
            parent_id, parent_hash = (last["id"], last["hash"]) if last else (None, "")
            full = []
            for cell in cells:
                cur = c.execute("SELECT value FROM sheet_cells WHERE sheet=? AND row=? AND col=?",
                                (cell["sheet"], str(cell["row"]), cell["col"])).fetchone()
                before = json.loads(cur["value"]) if cur else None
                full.append({**cell, "row": str(cell["row"]), "before": before})
                c.execute("INSERT OR REPLACE INTO sheet_cells VALUES (?,?,?,?,?)",
                          (cell["sheet"], str(cell["row"]), cell["col"], _j(cell["after"]), cs_id))
            ts = time.time()
            body = {"id": cs_id, "ts": ts, "case_id": case_id, "agent": agent, "task_id": task_id,
                    "action": action, "policy_ids": policy_ids or [], "instruction_version": instruction_version,
                    "memory_ids": memory_ids or [], "sources": sources or [], "cells": full,
                    "parent_id": parent_id, "reverts": reverts}
            h = hashlib.sha256((parent_hash + _j(body)).encode()).hexdigest()
            c.execute("INSERT INTO change_sets (id, ts, case_id, agent, task_id, action, policy_ids, "
                      "instruction_version, memory_ids, sources, cells, parent_id, reverts, hash) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (cs_id, ts, case_id, agent, task_id, action, _j(body["policy_ids"]), instruction_version,
                       _j(body["memory_ids"]), _j(body["sources"]), _j(full), parent_id, reverts, h))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return {**body, "hash": h}


def change_sets(case_id: str | None = None) -> list[dict]:
    with connect() as c:
        if case_id:
            return _rows(c.execute("SELECT * FROM change_sets WHERE case_id=? ORDER BY seq", (case_id,)))
        return _rows(c.execute("SELECT * FROM change_sets ORDER BY seq"))


def get_change_set(cs_id: str) -> dict | None:
    with connect() as c:
        r = _rows(c.execute("SELECT * FROM change_sets WHERE id=?", (cs_id,)))
    return r[0] if r else None


def sheet(name: str) -> list[dict]:
    with connect() as c:
        rows = _rows(c.execute("SELECT * FROM sheet_cells WHERE sheet=? ORDER BY row, col", (name,)))
    for r in rows:  # cell values are always JSON-encoded, including plain strings
        if isinstance(r.get("value"), str):
            try:
                r["value"] = json.loads(r["value"])
            except ValueError:
                pass
    return rows


def cell_history(sheet_name: str, row: str, col: str) -> list[dict]:
    out = []
    for cs in change_sets():
        for cell in cs["cells"]:
            if cell["sheet"] == sheet_name and cell["row"] == str(row) and cell["col"] == col:
                out.append({"change_set": cs["id"], "ts": cs["ts"], "agent": cs["agent"], "action": cs["action"],
                            "before": cell["before"], "after": cell["after"], "sources": cs["sources"],
                            "policy_ids": cs["policy_ids"]})
    return out


def verify_chain() -> bool:
    prev_hash = ""
    for cs in change_sets():
        body = {k: cs[k] for k in ("id", "ts", "case_id", "agent", "task_id", "action", "policy_ids",
                                   "instruction_version", "memory_ids", "sources", "cells", "parent_id", "reverts")}
        if hashlib.sha256((prev_hash + _j(body)).encode()).hexdigest() != cs["hash"]:
            return False
        prev_hash = cs["hash"]
    return True


# ---- memory + versioned instructions (written only by the Validator or a human) ----
def add_memory(agent: str, text: str, scope: dict | None = None, source: str = "") -> str:
    mid = f"mem_{uuid.uuid4().hex[:10]}"
    with connect() as c:
        c.execute("INSERT INTO memory VALUES (?,?,?,?,?,?,1)", (mid, time.time(), agent, _j(scope or {}), text, source))
    return mid


def memories(agent: str, active_only: bool = True) -> list[dict]:
    with connect() as c:
        q = "SELECT * FROM memory WHERE agent=?" + (" AND active=1" if active_only else "") + " ORDER BY ts"
        return _rows(c.execute(q, (agent,)))


def add_instruction_patch(agent: str, patch: str, author: str, reason: str) -> int:
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        v = (c.execute("SELECT MAX(version) m FROM instructions WHERE agent=?", (agent,)).fetchone()["m"] or 1) + 1
        c.execute("INSERT INTO instructions VALUES (?,?,?,?,?,?,0)", (agent, v, time.time(), patch, author, reason))
        c.execute("COMMIT")
    return v


def instruction_patches(agent: str, include_reverted: bool = False) -> list[dict]:
    with connect() as c:
        q = "SELECT * FROM instructions WHERE agent=?" + ("" if include_reverted else " AND reverted=0")
        return _rows(c.execute(q + " ORDER BY version", (agent,)))


def revert_instruction(agent: str, version: int) -> bool:
    with connect() as c:
        return c.execute("UPDATE instructions SET reverted=1 WHERE agent=? AND version=?",
                         (agent, version)).rowcount == 1


# ---- reviews, flags, idempotency ----
def add_review(case_id: str, agent: str, task_id: str | None, verdict: str, body: dict) -> str:
    rid = f"rev_{uuid.uuid4().hex[:10]}"
    with connect() as c:
        c.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?)", (rid, time.time(), case_id, agent, task_id, verdict, _j(body)))
    return rid


def reviews(case_id: str | None = None) -> list[dict]:
    with connect() as c:
        if case_id:
            return _rows(c.execute("SELECT * FROM reviews WHERE case_id=? ORDER BY ts", (case_id,)))
        return _rows(c.execute("SELECT * FROM reviews ORDER BY ts"))


def add_flag(target_type: str, target_id: str, note: str, by: str, case_id: str = "", agent: str = "") -> str:
    fid = f"flag_{uuid.uuid4().hex[:8]}"
    with connect() as c:
        c.execute("INSERT INTO flags VALUES (?,?,?,?,?,?,?,?,?)",
                  (fid, time.time(), target_type, target_id, case_id, agent, note, by, "open"))
    return fid


def flags(status: str | None = None) -> list[dict]:
    with connect() as c:
        if status:
            return _rows(c.execute("SELECT * FROM flags WHERE status=? ORDER BY ts", (status,)))
        return _rows(c.execute("SELECT * FROM flags ORDER BY ts"))


def set_flag_status(fid: str, status: str) -> None:
    with connect() as c:
        c.execute("UPDATE flags SET status=? WHERE id=?", (status, fid))


def unpark(key: str) -> None:
    """Re-open a claimed entry when delivering the resume failed, so the callback can be retried."""
    with connect() as c:
        c.execute("UPDATE parked SET resolved=NULL WHERE key=?", (key,))


def is_seen(key: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None


def seen_once(key: str) -> bool:
    """True the first time a key is seen; False for duplicates (event/idempotency dedupe)."""
    with connect() as c:
        return c.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, time.time())).rowcount == 1
