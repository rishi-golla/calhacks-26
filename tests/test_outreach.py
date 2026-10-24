import pytest

from common.outreach import OutreachBlocked, route_target


def test_routes_to_allowlist(monkeypatch):
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15555550100")
    t = route_target("phone", "+17755550112")
    assert t.intended_to == "+17755550112" and t.routed_to == "+15555550100"


def test_refuses_non_allowlisted_even_in_live_mode(monkeypatch):
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15555550100")
    monkeypatch.setenv("OUTREACH_MODE", "live")
    with pytest.raises(OutreachBlocked):
        route_target("phone", "+17755550112", demo_target="+17755550112")


def test_no_allowlist_blocks(monkeypatch):
    monkeypatch.delenv("DEMO_ALLOWLIST_EMAILS", raising=False)
    with pytest.raises(OutreachBlocked):
        route_target("email", "ap@shipper.com")
