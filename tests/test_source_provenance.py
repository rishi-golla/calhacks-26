"""Real model execution must not erase synthetic source provenance."""
import pytest

from common.envelope import TaskEnvelope
from common.source_repository import SourceRepository


def dataset():
    return {
        "cases": {"SYNTHETIC-CASE": {"invoice_amount": 123.45}},
        "companies": {"C": {"id": "C", "name": "Synthetic carrier"}},
        "contacts": {"C": [{"verified": True, "source": "synthetic:directory", "name": "Test contact"}]},
    }


async def source_records(data):
    repo = SourceRepository(data)
    return [
        await repo.load(TaskEnvelope(task_type="check_document_packet", case_id="SYNTHETIC-CASE")),
        await repo.get_company("C"),
        await repo.get_contact("C"),
    ]


@pytest.mark.parametrize("marker", ["fixture", "synthetic", "simulated", "source_simulated"])
async def test_live_inference_keeps_dataset_synthetic_labels(monkeypatch, marker):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    data = dataset()
    data[marker] = True
    for record in await source_records(data):
        assert record["source_simulated"] is True
        assert record["simulated"] is True
        assert record["source_kind"] == "synthetic"
    # Accessors must not stamp, mutate, or promote the configured source dataset.
    assert "source_kind" not in data["cases"]["SYNTHETIC-CASE"]


async def test_record_markers_survive_a_mixed_live_dataset(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    data = dataset()
    data["cases"]["SYNTHETIC-CASE"]["source_simulated"] = True
    data["contacts"]["C"][0]["simulated"] = True
    case, company, contact = await source_records(data)
    assert case["source_kind"] == contact["source_kind"] == "synthetic"
    assert company["source_kind"] == "configured_source"
    assert company["source_simulated"] is False


async def test_fixture_execution_keeps_unmarked_sources_synthetic(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    assert all(record["source_simulated"] for record in await source_records(dataset()))


async def test_live_configured_sources_are_not_labeled_as_fixtures(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    for record in await source_records(dataset()):
        assert record["simulated"] is False
        assert record["source_simulated"] is False
        assert record["source_kind"] == "configured_source"


async def test_fallback_company_contact_keeps_dataset_provenance(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    data = dataset()
    data.update(synthetic=True, contacts={})
    data["companies"]["C"].update(verified=True, source="synthetic:directory", phone="+15555550100")
    contact = await SourceRepository(data).get_contact("C")
    assert contact["source_simulated"] is True
    assert contact["source_kind"] == "synthetic"
