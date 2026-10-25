from common.policy import check


def test_invoice_over_contract_needs_approval():
    d = check("evaluate_invoice", "carrier_ap", {"contract_rate": 1850, "invoice_amount": 2150})
    assert d.result == "needs_approval" and "P-001" in d.policy_ids


def test_requester_cannot_approve():
    assert check("release_hold", "controller", {"requested_by": "controller", "amount": 10}).result == "deny"


def test_big_release_needs_human():
    assert check("release_hold", "controller", {"amount": 9400}).result == "needs_approval"


def test_unbalanced_entry_denied():
    d = check("post_journal_entry", "controller", {"lines": [{"debit": 100}, {"credit": 90}]})
    assert d.result == "deny" and d.policy_ids == ["P-006"]


def test_validator_cannot_change_policy():
    assert check("change_policy", "validator", {}).result == "deny"


def test_release_hold_threshold_counts_every_hold_on_the_invoice():
    from decimal import Decimal

    from common.policy import invoice_hold_exposure, release_hold_needs_approval
    # Released holds still count, so placing and releasing in turn cannot reset the total.
    holds = [{"amount": 4700, "status": "released"}, {"amount": 4700, "status": "active"}]
    assert invoice_hold_exposure(holds) == Decimal(9400)
    split = {"amount": 4700, "hold_amount": 4700, "invoice_hold_total": 9400}
    assert check("release_hold", "controller", split).result == "needs_approval"
    # A large hold partially released to $4,900 remaining still needs a human.
    shrunk = {"amount": 4900, "hold_amount": 9400, "invoice_hold_total": 9400}
    assert check("release_hold", "controller", shrunk).result == "needs_approval"
    small = {"amount": 300, "hold_amount": 300, "invoice_hold_total": 5000}
    assert check("release_hold", "controller", small).result == "allow"
    assert check("release_hold", "controller", {**split, "human_approval_id": "APP-1"}).result == "allow"
    assert release_hold_needs_approval({"invoice_hold_total": "5000.01"}) is True
    assert release_hold_needs_approval({}) is False


def test_release_hold_threshold_rejects_invalid_amounts():
    import pytest

    from common.policy import invoice_hold_exposure
    for value in (None, True, "abc", "NaN"):
        with pytest.raises(ValueError):
            check("release_hold", "controller", {"amount": 1, "invoice_hold_total": value})
    with pytest.raises(ValueError):
        invoice_hold_exposure([{"amount": "Infinity"}])
