"""Regex redaction layer: what must be tokenized, what must survive, and vault round-trips."""
import pytest

from harness.ingest.redact import leftovers, redact_obj, redact_text, replace_spans
from harness.ingest.vault import TOKEN_RE, Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault.sqlite")


@pytest.mark.parametrize(("text", "kind", "secret"), [
    ("Reach me at dispatch@sierrahaul.com today", "EMAIL", "dispatch@sierrahaul.com"),
    ("Call (775) 555-0142 for the driver", "PHONE", "(775) 555-0142"),
    ("cell 775-555-0142", "PHONE", "775-555-0142"),
    ("Call 415-555-0199.", "PHONE", "415-555-0199"),
    ("text +17755550142", "PHONE", "+17755550142"),
    ("SSN 123-45-6789 on file", "SSN", "123-45-6789"),
    ("SSN: 123456789", "SSN", "123456789"),
    ("Our EIN is 12-3456789", "EIN", "12-3456789"),
    ("Tax ID 123456789", "EIN", "123456789"),
    ("card 4111 1111 1111 1111 exp 09/27", "CARD", "4111 1111 1111 1111"),
    ("amex 3782 822463 10005", "CARD", "3782 822463 10005"),
    ("New routing number: 121000358", "ROUTING", "121000358"),
    ("ABA 021000021 please", "ROUTING", "021000021"),
    ("Account #: 000123456789", "ACCOUNT", "000123456789"),
    ("acct no. 4455 6677 8899", "ACCOUNT", "4455 6677 8899"),
])
def test_redacts(vault, text, kind, secret):
    out, toks = redact_text(text, vault)
    assert secret not in out
    assert len(toks) == 1 and toks[0].startswith(f"[[{kind}_")
    assert vault.lookup(toks[0]) == secret
    assert vault.detokenize(out) == text


@pytest.mark.parametrize("text", [
    "LOAD-4821 Stockton, CA -> Reno, NV, contract $1,850.00, invoice $2,150.00",
    "Detention 2h $300.00; arrived 14:02 departed 14:49 (47 min); free time 120 min",
    "Carrier C-311 USDOT 3141592 MC 123456 shipper S-07",
    "ts 2026-06-14T15:02:11Z EVT-1a2b3c4d CASE-4821",
    "routing 123456789 fails the ABA checksum",           # not a valid routing number
    "card 4111 1111 1111 1112",                          # fails Luhn
    "amounts 1850 2150 300 47 120 in prose",
])
def test_leaves_business_data(vault, text):
    out, toks = redact_text(text, vault)
    assert out == text and toks == []


def test_tokens_are_deterministic_and_idempotent(vault):
    a, _ = redact_text("pay to acct 99887766 and email ap@x.io", vault)
    b, _ = redact_text("pay to acct 99887766 and email ap@x.io", vault)
    assert a == b
    again, toks = redact_text(a, vault)
    assert again == a and toks == []


def test_tokens_stable_across_vault_reopen(tmp_path):
    t1 = Vault(tmp_path / "v.sqlite").tokenize("EMAIL", "a@b.co")
    t2 = Vault(tmp_path / "v.sqlite").tokenize("EMAIL", "a@b.co")
    assert t1 == t2 and TOKEN_RE.fullmatch(t1)


def test_different_vaults_give_different_tokens(tmp_path):
    assert Vault(tmp_path / "a.sqlite").tokenize("EMAIL", "a@b.co") != \
        Vault(tmp_path / "b.sqlite").tokenize("EMAIL", "a@b.co")


def test_redact_obj_by_key_and_pattern(vault):
    payload = {"carrier": {"id": "C-77", "remit_email": "pay@rapidridge.com"},
               "bank": {"routing_number": "121000358", "account_number": 123456789012},
               "body": "Please update. Call 415-555-0199.", "amount": 9400.0, "line_items": [{"amount": 1.0}]}
    out, toks = redact_obj(payload, vault)
    flat = str(out)
    for secret in ("pay@rapidridge.com", "121000358", "123456789012", "415-555-0199"):
        assert secret not in flat
    assert out["amount"] == 9400.0 and out["carrier"]["id"] == "C-77" and out["line_items"] == [{"amount": 1.0}]
    assert out["bank"]["account_number"].startswith("[[ACCOUNT_")
    assert len(toks) == 4
    assert leftovers(out) == []


@pytest.mark.parametrize("key", ["new_account", "account", "bank_account"])
def test_redact_obj_bare_account_keys(vault, key):
    out, toks = redact_obj({key: "123456789012", "amount": 9400.0}, vault)
    assert out[key].startswith("[[ACCOUNT_") and vault.lookup(out[key]) == "123456789012"
    assert out["amount"] == 9400.0 and len(toks) == 1


def test_replace_spans_and_leftovers(vault):
    tok = vault.tokenize("NAME", "Dale Whitaker")
    obj = {"note": "Signed by Dale Whitaker", "items": ["Dale Whitaker, owner"]}
    assert "Dale Whitaker" not in str(replace_spans(obj, {"Dale Whitaker": tok}))
    assert replace_spans("Dale and Daleville", {"Dale": "[[NAME_00000000]]"}) == "[[NAME_00000000]] and Daleville"
    assert leftovers({"x": "mail me: bob@example.com"}) != []


def test_structured_address_and_person_keys(vault):
    payload = {"shipper": {"name": "Bay Produce Co", "address": "2200 SECAUCUS RD, NORTH BERGEN, NJ, 07047"},
               "signed_by": "R. Gomez", "receiver": "Bay Produce Co", "requested_by": "Marcus Bell"}
    out, toks = redact_obj(payload, vault)
    assert out["shipper"]["address"].startswith("[[ADDR_") and out["shipper"]["name"] == "Bay Produce Co"
    assert out["signed_by"].startswith("[[NAME_") and out["requested_by"].startswith("[[NAME_")
    assert out["receiver"] == "Bay Produce Co"          # company names stay
    assert len(toks) == 3


def test_known_spans_scrub_free_text(vault):
    from harness.ingest.redact import known_spans
    payload = {"carrier": {"address": "2200 SECAUCUS RD, NORTH BERGEN, NJ, 07047"},
               "text": "REMIT TO\n2200 Secaucus Rd, North Bergen NJ 07047\nSigned: R. Gomez", "signed_by": "R. Gomez"}
    out, toks = redact_obj(payload, vault)
    out = replace_spans(out, known_spans(toks, vault))
    assert "Secaucus" not in out["text"] and "Gomez" not in out["text"]
    assert out["text"].count("[[ADDR_") == 1 and out["text"].count("[[NAME_") == 1


@pytest.mark.parametrize("key", ["demo_target", "demotarget", "phone", "contact", "routed_to", "signed_by"])
def test_demo_allowlist_labels_are_never_tokenized(vault, key):
    """W4's Voice Desk resolves team-N to an allowlisted number at dial time; tokenizing it would block demo calls."""
    out, toks = redact_obj({key: "team-1"}, vault)
    assert out[key] == "team-1" and toks == []
    assert redact_text("Route the call to team-2.", vault)[0] == "Route the call to team-2."
