"""Agent recall over the Elastic `agent-memory` mirror (common/agent_recall.py)."""
import json

import httpx
import pytest

from common import agent_recall

PRECEDENT = {"id": "P-1", "carrier_id": "C-311", "claim_type": "detention", "lane": "OAK-PHX", "outcome": "hold",
             "source_case": "CASE-OLD", "pattern_json": "{}", "as_of": "2026-10-20"}


def memory(row_id: str, *, active: bool = True, text: str = "hold padded detention", version: int = 1) -> dict:
    return {"id": row_id, "agent": "carrier_ap", "scope": json.dumps({"carrier_id": "C-311"}), "text": text,
            "source_ref": "review:R-1", "active": active, "version": version, "created_at": "2026-10-21"}


class FakeSpacetime:
    """SpacetimeDB read side: rows per table, every SQL string seen."""

    def __init__(self, memories=(), precedents=(), fail=False):
        self.tables = {"agent_memory": list(memories), "precedent": list(precedents)}
        self.fail, self.sql = fail, []

    async def query_rows(self, sql: str) -> list[dict]:
        from common.spacetime_client import SpacetimeTransportError

        self.sql.append(sql)
        if self.fail:
            raise SpacetimeTransportError(timeout=True, may_have_committed=False)
        return list(self.tables[sql.split()[3]])


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(agent_recall.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("ELASTIC_CLOUD_URL", "https://es.example")
    monkeypatch.setenv("ELASTIC_AGENT_MEMORY_READER_KEY", "reader")
    seen, hits = [], [{"_id": "precedent:P-1", "_score": 7.1234}]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"hits": {"hits": hits}})

    _mock_http(monkeypatch, handler)
    return seen, hits


def test_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv("ELASTIC_CLOUD_URL", raising=False)
    monkeypatch.delenv("ELASTIC_AGENT_MEMORY_READER_KEY", raising=False)
    assert agent_recall.enabled() is False


async def test_unconfigured_recall_returns_nothing(monkeypatch):
    monkeypatch.delenv("ELASTIC_AGENT_MEMORY_READER_KEY", raising=False)
    assert await agent_recall.similar_cases(carrier_id="C-1") == []


async def test_query_uses_reader_key_and_only_active_memory(cloud):
    seen, _ = cloud
    db = FakeSpacetime(precedents=[PRECEDENT])
    hits = await agent_recall.similar_cases(carrier_id="C-311", claim_type="detention", text="detention padded", db=db)
    assert [(h["id"], h["case_id"], h["outcome"], h["score"]) for h in hits] == [("precedent:P-1", "CASE-OLD", "hold",
                                                                                  7.12)]
    assert set(hits[0]) == {*agent_recall.FIELDS, "score"}
    request = seen[0]
    assert request.headers["authorization"] == "ApiKey reader" and request.url.path == "/agent-memory/_search"
    body = json.loads(request.content)
    assert body["_source"] is False and body["size"] == 10   # ids only, twice the default limit of 5
    assert body["query"]["bool"]["filter"] == [{"term": {"active": True}}]
    assert {"term": {"carrier_id": {"value": "C-311", "boost": 4}}} in body["query"]["bool"]["should"]
    assert db.sql == ["SELECT * FROM precedent WHERE id = 'P-1'"]


async def test_no_criteria_means_no_query(cloud):
    seen, _ = cloud
    assert await agent_recall.similar_cases() == [] and seen == []


async def test_facts_come_from_spacetime_in_elastic_rank_order(cloud):
    """A lesson reverted since the last sync is dropped; edited text is current; a missing row is skipped."""
    _, hits = cloud
    hits[:] = [{"_id": "memory:M-2", "_score": 9.0}, {"_id": "memory:M-1", "_score": 8.0},
               {"_id": "memory:GONE", "_score": 7.5}, {"_id": "precedent:P-1", "_score": 7.0}]
    db = FakeSpacetime(memories=[memory("M-1", text="new wording", version=3), memory("M-2", active=False)],
                       precedents=[PRECEDENT])
    cases = await agent_recall.similar_cases(carrier_id="C-311", limit=2, db=db)
    assert [(c["id"], c["score"]) for c in cases] == [("memory:M-1", 8.0), ("precedent:P-1", 7.0)]
    assert cases[0]["text"] == "new wording" and cases[0]["version"] == 3
    assert db.sql[0] == "SELECT * FROM agent_memory WHERE id = 'M-2' OR id = 'M-1' OR id = 'GONE'"


async def test_ids_are_quoted_in_sql(cloud):
    _, hits = cloud
    hits[:] = [{"_id": "memory:x' OR '1'='1", "_score": 1.0}, {"_id": "unknown:Z", "_score": 1.0}]
    db = FakeSpacetime()
    assert await agent_recall.similar_cases(carrier_id="C-311", db=db) == []
    assert db.sql == ["SELECT * FROM agent_memory WHERE id = 'x'' OR ''1''=''1'"]


async def test_spacetime_unavailable_returns_nothing_not_the_mirror(cloud):
    assert await agent_recall.similar_cases(carrier_id="C-311", db=FakeSpacetime(fail=True)) == []


def test_reverted_memory_is_marked_inactive():
    doc = agent_recall.memory_doc({"id": "M-1", "agent": "carrier_ap", "text": "hold padded detention",
                                   "scope": json.dumps({"carrier_id": "C-311", "skill": "settle_carrier_invoice"}),
                                   "active": False, "version": 2})
    assert doc["id"] == "memory:M-1" and doc["active"] is False and doc["carrier_id"] == "C-311"


def test_precedent_text_omits_evidence_refs():
    doc = agent_recall.precedent_doc({"id": "P-1", "carrier_id": "C-311", "claim_type": "detention", "lane": "OAK-PHX",
                                      "outcome": "hold", "source_case": "CASE-1",
                                      "pattern_json": json.dumps({"free_time_min": 120, "source_evidence": ["x"]})})
    assert "free_time_min" in doc["text"] and "source_evidence" not in doc["text"] and doc["case_id"] == "CASE-1"


@pytest.fixture
def bulk(monkeypatch):
    """Elastic `_bulk`: records each request's actions; `fail` lists ids to answer with a 500."""
    monkeypatch.setenv("ELASTIC_CLOUD_URL", "https://es.example")
    monkeypatch.setenv("ELASTIC_AGENT_MEMORY_WRITER_KEY", "writer")
    state = {"requests": [], "fail": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [json.loads(line) for line in request.content.decode().splitlines()]
        actions, items = [], []
        for line in lines:
            if "index" in line or "delete" in line:
                op = next(iter(line))
                doc_id = line[op]["_id"]
                actions.append((op, doc_id))
                items.append({op: {"_id": doc_id, "status": 500 if doc_id in state["fail"] else 200}})
        state["requests"].append(actions)
        return httpx.Response(200, json={"items": items})

    _mock_http(monkeypatch, handler)
    return state


async def test_sync_sends_only_changes(bulk):
    db = FakeSpacetime(memories=[memory("M-1"), memory("M-2", active=False)], precedents=[PRECEDENT])
    sent: dict[str, str] = {}
    first = await agent_recall.sync(sent, db=db)
    assert sorted(bulk["requests"][0]) == [("delete", "memory:M-2"), ("index", "memory:M-1"),
                                           ("index", "precedent:P-1")]
    assert (first["indexed"], first["removed"], first["errors"]) == (2, 1, 0)

    second = await agent_recall.sync(sent, db=db)
    assert len(bulk["requests"]) == 1 and second["unchanged"] == 3   # nothing changed, no request at all

    db.tables["agent_memory"] = [memory("M-1", text="edited", version=2), memory("M-2", active=False),
                                 memory("M-3")]
    await agent_recall.sync(sent, db=db)
    assert sorted(bulk["requests"][-1]) == [("index", "memory:M-1"), ("index", "memory:M-3")]


async def test_sync_reverts_and_vanished_rows_leave_the_index(bulk):
    db = FakeSpacetime(memories=[memory("M-1"), memory("M-2")], precedents=[PRECEDENT])
    sent: dict[str, str] = {}
    await agent_recall.sync(sent, db=db)
    db.tables["agent_memory"] = [memory("M-1", active=False)]   # M-1 reverted, M-2 gone
    result = await agent_recall.sync(sent, db=db)
    assert sorted(bulk["requests"][-1]) == [("delete", "memory:M-1"), ("delete", "memory:M-2")]
    assert result["removed"] == 2 and "memory:M-2" not in sent
    await agent_recall.sync(sent, db=db)
    assert len(bulk["requests"]) == 2   # a reverted row is deleted once, not every pass


async def test_sync_retries_failed_items_next_pass(bulk):
    db = FakeSpacetime(memories=[memory("M-1"), memory("M-2")])
    sent: dict[str, str] = {}
    bulk["fail"] = {"memory:M-2"}
    first = await agent_recall.sync(sent, db=db)
    assert (first["indexed"], first["errors"]) == (1, 1)
    bulk["fail"] = set()
    await agent_recall.sync(sent, db=db)
    assert bulk["requests"][-1] == [("index", "memory:M-2")]


async def test_sync_without_state_sends_everything(bulk):
    db = FakeSpacetime(memories=[memory("M-1")])
    await agent_recall.sync(db=db)
    await agent_recall.sync(db=db)
    assert bulk["requests"] == [[("index", "memory:M-1")]] * 2
