"""Exact money reading (harness/ingest/amounts.py)."""
import pytest

from harness.ingest.amounts import money, read_invoice, read_payment

INVOICE = """Total due: $4,740.94. Remit to BROCKWAY TRANSPORT INC.
--- attachment: INV-1.pdf ---
Description Qty Rate Amount
Linehaul 1 $3,925.00 $3,925.00
Fuel surcharge 1363 $0.433 $590.18
Detention 2h 2 $112.88 $225.76
TOTAL DUE: $4,740.94
"""


@pytest.mark.parametrize(("text", "value"), [("$3,925.00", 3925.0), ("$0.433", 0.433), ("($12.50)", -12.5),
                                              ("$1,769.44", 1769.44), ("$ 865", 865.0), ("$-63.50", -63.5),
                                              ("abc", None), (None, None)])
def test_money(text, value):
    assert money(text) == value


def test_invoice_total_and_lines():
    r = read_invoice(INVOICE)
    assert r["invoice_amount"] == 4740.94 and r["issues"] == []
    assert [i["amount"] for i in r["line_items"]] == [3925.0, 590.18, 225.76]
    assert [i["desc"] for i in r["line_items"]] == ["Linehaul", "Fuel surcharge", "Detention"]


def test_conflicting_totals_are_not_guessed():
    r = read_invoice(INVOICE.replace("TOTAL DUE: $4,740.94", "TOTAL DUE: $4,470.94"))
    assert r["invoice_amount"] is None and r["issues"] == ["document states conflicting totals"]


def test_missing_total_is_flagged():
    assert read_invoice("Linehaul $100.00")["issues"] == ["no total found in document"]


def test_payment():
    assert read_payment("Payment of $1,848.97 sent via ACH for invoice SINV-1 (PO PO-4).")["amount"] == 1848.97
    assert read_payment("Thanks!")["amount"] is None


def test_quickpay_discount_line_with_sign_after_dollar():
    r = read_invoice("Description Qty Rate Amount\nLinehaul 1 $2,700.00 $2,700.00\nFuel surcharge 1097 $0.433 $475.00\n"
                     "QuickPay discount 2% 1 $-63.50 $-63.50\nTOTAL DUE: $3,111.50")
    assert r["invoice_amount"] == 3111.5 and [i["amount"] for i in r["line_items"]] == [2700.0, 475.0, -63.5]
