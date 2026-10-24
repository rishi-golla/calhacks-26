import asyncio
import json
import os
import random
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from integrations import directory
from sim import generator, retention, verify
from sim.verify import ALLOWED
from sim.world import offline_world


@pytest.fixture(scope="module")
def generated_data(tmp_path_factory):
    root = tmp_path_factory.mktemp("verify-data")
    old_data, old_world = os.environ.get("SIM_DATA_DIR"), os.environ.get("WORLD_FILE")
    os.environ["SIM_DATA_DIR"] = str(root)
    os.environ["WORLD_FILE"] = str(root / "world.json")
    world = offline_world()
    directory.save(world)

    async def generate():
        await generator.seed_hero(world)
        for index, scenario in enumerate(sorted(generator.SCENARIOS)):
            rnd = random.Random(100 + index)
            load = generator.new_load(world, rnd)
            steps = generator.lifecycle(load, scenario, rnd,
                                        t0=datetime(2026, 1, 1, 8, 0, tzinfo=UTC))
            for step in steps:
                for event in step.make():
                    await generator.publish(None, event)
            generator.record_answer(load, scenario, load.get("planted"))

    try:
        asyncio.run(generate())
    finally:
        if old_data is None:
            os.environ.pop("SIM_DATA_DIR", None)
        else:
            os.environ["SIM_DATA_DIR"] = old_data
        if old_world is None:
            os.environ.pop("WORLD_FILE", None)
        else:
            os.environ["WORLD_FILE"] = old_world
    return root


def _copy_data(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    return target


def test_verifier_accepts_all_scenarios_and_hero_seeds(generated_data):
    report = verify.run(generated_data)
    assert report["ok"], report["unexpected_violations"]
    assert set(generator.SCENARIOS) <= set(report["by_scenario"])
    answers = [json.loads(line) for line in (generated_data / "answer_key.jsonl").read_text().splitlines()]
    rules_by_load = {}
    for item in report["violations"]:
        rules_by_load.setdefault(item["load_id"], set()).add(item["rule"])
    for answer in answers:
        if ALLOWED.get(answer["scenario"]):
            assert rules_by_load.get(answer["load_id"], set()) & ALLOWED[answer["scenario"]]
    assert {"LOAD-4821", "LOAD-4877"} <= {
        doc.get("load_id") for path in (generated_data / "docs").glob("*.json")
        if (doc := json.loads(path.read_text()))
    }


def test_answer_key_scenarios_and_outcomes_are_isolated(generated_data):
    from pypdf import PdfReader

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    answers = [json.loads(line) for line in (generated_data / "answer_key.jsonl").read_text().splitlines()]
    forbidden = set(generator.SCENARIOS)
    for answer in answers:
        forbidden.update(strings(answer.get("expected", {})))
    artifacts = [generated_data / "events.jsonl", *generated_data.glob("docs/*.json"),
                 *generated_data.glob("mail/*.eml")]
    for path in generated_data.glob("docs/*.pdf"):
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        assert not any(term.casefold() in text.casefold() for term in forbidden), path.name
    for path in artifacts:
        data = path.read_bytes().decode("utf-8", "replace").casefold()
        assert not any(term.casefold() in data for term in forbidden), path.name


def test_pdf_fsc_rates_keep_precision_and_satisfy_v2(generated_data):
    from ingest.documents import extract_invoice_rules
    from pypdf import PdfReader

    clean_load = next(item["load_id"] for item in
                      (json.loads(line) for line in (generated_data / "answer_key.jsonl").read_text().splitlines())
                      if item["scenario"] == "clean")
    docs = [json.loads(path.read_text()) for path in (generated_data / "docs").glob("*.json")]
    invoice = next(doc for doc in docs if doc.get("load_id") == clean_load
                   and doc.get("type") == "carrier_invoice")
    invoice_text = "\n".join(page.extract_text() or "" for page in
                             PdfReader(str(generated_data / "docs" / invoice["pdf"])).pages)
    parsed = extract_invoice_rules(invoice_text)
    fsc = next(line for line in parsed["line_items"] if "fuel surcharge" in line["desc"].casefold())
    assert fsc["rate"] == 0.433
    assert round(fsc["qty"] * fsc["rate"], 2) == fsc["amount"]
    for kind in ("rate_confirmation", "shipper_invoice"):
        doc = next(item for item in docs if item.get("load_id") == clean_load and item.get("type") == kind)
        text = "\n".join(page.extract_text() or "" for page in
                         PdfReader(str(generated_data / "docs" / doc["pdf"])).pages)
        assert "$0.433" in text
    report = verify.run(generated_data)
    assert not any(item["rule"] == "V2" and item["load_id"] == clean_load for item in report["violations"])


def test_verifier_detects_invoice_total_mutation(generated_data, tmp_path):
    root = _copy_data(generated_data, tmp_path / "total-mutation")
    clean_load = next(item["load_id"] for item in
                      (json.loads(line) for line in (root / "answer_key.jsonl").read_text().splitlines())
                      if item["scenario"] == "clean")
    invoice_path = next(path for path in (root / "docs").glob("*.json")
                        if (doc := json.loads(path.read_text())).get("load_id") == clean_load
                        and doc.get("type") == "carrier_invoice")
    invoice = json.loads(invoice_path.read_text())
    invoice["total"] += 1
    invoice_path.write_text(json.dumps(invoice))
    report = verify.run(root)
    assert not report["ok"]
    assert any(item["rule"] == "V2" and item["load_id"] == clean_load
               for item in report["unexpected_violations"])


def test_verifier_detects_missing_pod(generated_data, tmp_path):
    root = _copy_data(generated_data, tmp_path / "pod-mutation")
    clean_load = next(item["load_id"] for item in
                      (json.loads(line) for line in (root / "answer_key.jsonl").read_text().splitlines())
                      if item["scenario"] == "clean")
    pod_path = next(path for path in (root / "docs").glob("*.json")
                    if (doc := json.loads(path.read_text())).get("load_id") == clean_load
                    and doc.get("type") == "proof_of_delivery")
    pod = json.loads(pod_path.read_text())
    (root / "docs" / pod.get("pdf", f"{pod['ref']}.pdf")).unlink()
    pod_path.unlink()
    report = verify.run(root)
    assert not report["ok"]
    assert any(item["rule"] == "V4" and item["load_id"] == clean_load
               for item in report["unexpected_violations"])


def test_verifier_detects_counterparty_phone_mutation(generated_data, tmp_path):
    root = _copy_data(generated_data, tmp_path / "phone-mutation")
    clean_load = next(item["load_id"] for item in
                      (json.loads(line) for line in (root / "answer_key.jsonl").read_text().splitlines())
                      if item["scenario"] == "clean")
    rate_con_path = next(path for path in (root / "docs").glob("*.json")
                         if (doc := json.loads(path.read_text())).get("load_id") == clean_load
                         and doc.get("type") == "rate_confirmation")
    rate_con = json.loads(rate_con_path.read_text())
    rate_con["carrier"]["phone"] = "+15550000000"
    rate_con_path.write_text(json.dumps(rate_con))
    report = verify.run(root)
    assert not report["ok"]
    assert any(item["rule"] == "V10" and item["load_id"] == clean_load
               for item in report["unexpected_violations"])


def test_retention_keeps_ten_loads_and_hero_fixtures(tmp_path, monkeypatch):
    root = tmp_path / "retention-data"
    monkeypatch.setenv("SIM_DATA_DIR", str(root))
    monkeypatch.setenv("WORLD_FILE", str(root / "world.json"))
    asyncio.run(generator.offline(30, root, seed=19))

    removed = retention.prune(root, keep_loads=10)
    answers = [json.loads(line) for line in (root / "answer_key.jsonl").read_text().splitlines()]
    pruned = {json.loads(line)["load_id"] for line in (root / "pruned_loads.jsonl").read_text().splitlines()}
    remaining = {item["load_id"] for item in answers} - pruned
    file_loads = {json.loads(path.read_text()).get("load_id") for path in (root / "docs").glob("*.json")}
    assert len(remaining) == 10
    assert file_loads == remaining
    assert {"LOAD-4821", "LOAD-4877"} <= file_loads
    assert len(removed) == 22
    report = verify.run(root)
    assert report["ok"], report["unexpected_violations"]
    assert set(report["pruned"]) == pruned
