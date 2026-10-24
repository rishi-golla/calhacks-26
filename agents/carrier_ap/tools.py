from common.policy import check


def three_way_match(subject: dict) -> dict:
    d = check("evaluate_invoice", "carrier_ap", subject)
    delta = round(subject["invoice_amount"] - subject["contract_rate"], 2)
    return {"matched": d.result == "allow", "delta": delta, "policy_ids": d.policy_ids, "reason": d.reason}
