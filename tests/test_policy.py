from common.policy import check


def test_invoice_over_contract_needs_approval():
    d = check("evaluate_invoice", "carrier_ap", {"contract_rate": 1850, "invoice_amount": 2150})
    assert d.result == "needs_approval" and "P-001" in d.policy_ids


def test_duplicate_invoice_needs_approval():
    d = check("evaluate_invoice", "carrier_ap", {
        "contract_rate": 1000, "invoice_amount": 1000, "duplicate_of": "INV-1"})
    assert d.result == "needs_approval" and d.policy_ids == ["P-007"]
    assert d.reason == "duplicate of INV-1 (same load/BOL)"


def test_unsigned_pod_needs_approval():
    d = check("evaluate_invoice", "carrier_ap", {
        "contract_rate": 1000, "invoice_amount": 1000, "pod_signed": False})
    assert d.result == "needs_approval" and d.policy_ids == ["P-013"]


def test_accessorial_without_receipt_needs_approval():
    payload = {"contract_rate": 1000, "invoice_amount": 1000,
               "line_items": [{"desc": "Detention 2h", "amount": 150}], "receipt_attached": False}
    d = check("evaluate_invoice", "carrier_ap", payload)
    assert d.result == "needs_approval" and d.policy_ids == ["P-014"]
    assert check("evaluate_invoice", "carrier_ap", {**payload, "receipt_attached": True}).result == "allow"


def test_remit_to_without_noa_needs_approval():
    payload = {"contract_rate": 1000, "invoice_amount": 1000, "carrier": {"name": "Carrier"},
               "remit_to": {"name": "Factor"}}
    d = check("evaluate_invoice", "carrier_ap", payload)
    assert d.result == "needs_approval" and d.policy_ids == ["P-004"]
    assert check("evaluate_invoice", "carrier_ap", {
        **payload, "carrier": {"name": "Carrier", "factor": "Factor"}}).result == "allow"


def test_clean_contract_priced_invoice_is_allowed():
    d = check("evaluate_invoice", "carrier_ap", {"contract_rate": 1000, "invoice_amount": 1000})
    assert d.result == "allow"


def test_invoice_policy_returns_all_matching_ids():
    d = check("evaluate_invoice", "carrier_ap", {
        "contract_rate": 1000, "invoice_amount": 1100, "line_items": [{"desc": "lumper"}],
        "receipt_attached": False})
    assert d.result == "needs_approval" and d.policy_ids == ["P-014", "P-001"]


def test_requester_cannot_approve():
    assert check("release_hold", "controller", {"requested_by": "controller", "amount": 10}).result == "deny"


def test_big_release_needs_human():
    assert check("release_hold", "controller", {"amount": 9400}).result == "needs_approval"


def test_unbalanced_entry_denied():
    d = check("post_journal_entry", "controller", {"lines": [{"debit": 100}, {"credit": 90}]})
    assert d.result == "deny" and d.policy_ids == ["P-006"]


def test_validator_cannot_change_policy():
    assert check("change_policy", "validator", {}).result == "deny"
