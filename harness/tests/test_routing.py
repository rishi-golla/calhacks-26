"""Deterministic first-hop table (PRD 7.3), including the temporary POD exception."""
import pytest

from harness.ingest.routing import CLASSIFIER_ALLOWLIST, NOT_ROUTED, ROUTES, table_route


@pytest.mark.parametrize(("etype", "skill"), [
    ("carrier_invoice.received", "process_carrier_invoice"),
    ("voice.inbound.dispute", "process_carrier_invoice"),
    ("shipper_payment.received", "reconcile_customer_payment"),
    ("bank.event", "reconcile_customer_payment"),
    ("bank_change.requested", "screen_bank_change"),
])
def test_typed_events_route(etype, skill):
    r = table_route(etype, {})
    assert r.status == "routed" and r.skill == skill


@pytest.mark.parametrize("etype", ["gps.ping", "load.tendered", "load.dispatched", "load.delivered",
                                   "pod.received"])
def test_not_routed(etype):
    r = table_route(etype, {})
    assert r.status == "not_routed" and r.skill is None


def test_pod_parked_until_ap_handles_invoiceless_pods():
    assert "pod.received" in NOT_ROUTED and "pod.received" not in ROUTES


def test_scenario_follows_generated_type():
    assert table_route("scenario.injected", {"generated_type": "load.tendered"}).status == "not_routed"
    r = table_route("scenario.injected", {"generated_type": "bank_change.requested"})
    assert r.status == "routed" and r.skill == "screen_bank_change"


@pytest.mark.parametrize(("etype", "payload"), [
    ("email.received", {}), ("something.new", {}), ("scenario.injected", {"generated_type": "weird"})])
def test_unlabeled_go_to_classifier(etype, payload):
    assert table_route(etype, payload) is None


def test_classifier_allowlist_is_first_hop_skills_only():
    assert set(CLASSIFIER_ALLOWLIST) == {*ROUTES.values(), "answer_payment_status"}
