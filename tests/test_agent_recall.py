"""Agent recall over the Elastic `agent-memory` mirror (common/agent_recall.py)."""
import json

import httpx
import pytest

from common import agent_recall


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("ELASTIC_CLOUD_URL", "https://es.example")
    monkeypatch.setenv("ELASTIC_AGENT_MEMORY_READER_KEY", "reader")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"hits": {"hits": [
            {"_score": 7.1234, "_source": {"id": "precedent:P-1", "kind": "precedent", "case_id": "CASE-OLD",
                                           "carrier_id": "C-311", "outcome": "hold"}}]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(agent_recall.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv("ELASTIC_CLOUD_URL", raising=False)
    monkeypatch.delenv("ELASTIC_AGENT_MEMORY_READER_KEY", raising=False)
    assert agent_recall.enabled() is False


async def test_unconfigured_recall_returns_nothing(monkeypatch):
    monkeypatch.delenv("ELASTIC_AGENT_MEMORY_READER_KEY", raising=False)
    assert await agent_recall.similar_cases(carrier_id="C-1") == []


async def test_query_uses_reader_key_and_only_active_memory(cloud):
    hits = await agent_recall.similar_cases(carrier_id="C-311", claim_type="detention", text="detention padded")
    assert hits == [{"id": "precedent:P-1", "kind": "precedent", "case_id": "CASE-OLD", "carrier_id": "C-311",
                     "outcome": "hold", "score": 7.12}]
    request = cloud[0]
    assert request.headers["authorization"] == "ApiKey reader" and request.url.path == "/agent-memory/_search"
    body = json.loads(request.content)
    assert body["query"]["bool"]["filter"] == [{"term": {"active": True}}]
    assert {"term": {"carrier_id": {"value": "C-311", "boost": 4}}} in body["query"]["bool"]["should"]


async def test_no_criteria_means_no_query(cloud):
    assert await agent_recall.similar_cases() == [] and cloud == []


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
