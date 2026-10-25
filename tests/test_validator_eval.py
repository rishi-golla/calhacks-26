"""The Validator catches seeded wrong decisions on real exported cases and passes the correct ones.

Runs harness/tests/validator_eval.py offline: simulator lifecycle -> ingest -> source export -> in-process
agents -> deterministic review, for a handful of planted scenarios.
"""
import pytest

from harness.tests.validator_eval import evaluate

SCENARIOS = ["clean", "rate_mismatch", "pod_unsigned", "duplicate_renumbered", "decoy_quickpay"]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    root = tmp_path_factory.mktemp("validator-eval")
    with pytest.MonkeyPatch.context() as monkeypatch:
        # Same offline world setup as tests/test_ingest.py; evaluate() scopes each scenario to its own folder.
        monkeypatch.setenv("WORLD_FILE", str(root / "world.json"))
        monkeypatch.setenv("SIM_DATA_DIR", str(root))
        monkeypatch.setenv("MOCK_INTEGRATIONS", "0")
        monkeypatch.setenv("HARNESS_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
        monkeypatch.setenv("SPACETIME_DISABLED", "1")
        monkeypatch.delenv("APIFY_TOKEN", raising=False)
        monkeypatch.delenv("HUMAN_APPROVER_ID", raising=False)
        yield evaluate(SCENARIOS, workdir=root)


def rows(report, scenario):
    return next(item for item in report["scenarios"] if item["scenario"] == scenario)["rows"]


def root_row(report, scenario, case="CASE"):
    return next(row for row in rows(report, scenario) if row["case"] == case and row["depth"] == 0)


def verdicts(report):
    return [(row["scenario"], row["case"], row["agent"], row.get("outcome"), row.get("verdict"),
             row.get("failed_checks"), row.get("mutations")) for item in report["scenarios"] for row in item["rows"]]


def test_every_mutation_of_every_terminal_artifact_is_caught(report):
    reviewed = [row for item in report["scenarios"] for row in item["rows"]
                if row.get("in_scope") and "verdict" in row]
    assert {row["scenario"] for row in reviewed} == set(SCENARIOS)
    for row in reviewed:
        applicable = {name: result for name, result in row["mutations"].items() if result["applicable"]}
        assert {"drop_evidence", "shift_cent"} <= applicable.keys(), row["case_id"]
        for name, result in applicable.items():
            assert result["verdict"] == "issue" and result["new_failures"], (row["case_id"], row["agent"], name)
    summary = report["summary"]
    assert summary["missed_mutations"] == [] and summary["catch_rate"] == 1.0


def test_clean_and_quickpay_decoy_approvals_are_not_false_alarms(report):
    for scenario in ("clean", "decoy_quickpay"):
        for row in rows(report, scenario):
            assert row["false_alarm_eligible"] and row["verdict"] == "pass", (scenario, row["case"], row["agent"])
        approval = root_row(report, scenario)
        assert approval["agent"] == "carrier_ap" and approval["outcome"] == "approve"
        assert approval["mutations"]["flip_decision"]["new_failures"] == ["decision_justified"]
    assert report["summary"]["false_alarms"] == 0


def test_answer_key_holds_are_reviewed_and_waits_are_recorded(report):
    held = root_row(report, "rate_mismatch")
    assert (held["outcome"], held["verdict"]) == ("hold", "pass")
    assert "approve_clear_of_above_tolerance" in held["mutations"]["flip_decision"]["new_failures"]
    renumbered = root_row(report, "duplicate_renumbered", "CASE-R")
    assert (renumbered["outcome"], renumbered["verdict"]) == ("reject", "pass")
    assert renumbered["mutations"]["flip_decision"]["new_failures"] == ["approve_clear_of_duplicate_invoice"]
    unsigned = root_row(report, "pod_unsigned")
    assert unsigned["status"] == "parked" and unsigned["detail"].startswith("awaiting_external_verification")
    assert report["summary"]["over_threshold_scenarios"] == []


def test_evaluation_is_deterministic(report, tmp_path):
    again = evaluate(["rate_mismatch"], workdir=tmp_path)
    first = {**report, "scenarios": [item for item in report["scenarios"] if item["scenario"] == "rate_mismatch"]}
    assert verdicts(again) == verdicts(first)
    assert again["scenarios"][0]["load_id"] == first["scenarios"][0]["load_id"]
