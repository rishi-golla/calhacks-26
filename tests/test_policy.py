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
