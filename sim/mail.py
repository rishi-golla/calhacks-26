"""Raw inbound email (RFC 822 .eml) as it lands in Golden Gate Freight's AP/AR inboxes: carrier invoices with
PDF attachments, shipper remittance advice, detention disputes and bank-change requests (including lookalike-domain
fraud). Nothing is sent: messages are written to data/mail/ and announced on the event stream."""
import os
import pathlib
import uuid
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from sim.docs import docs_dir

AP_INBOX = "ap@goldengatefreight.example"
AR_INBOX = "ar@goldengatefreight.example"


def mail_dir() -> pathlib.Path:
    d = pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / "mail"
    d.mkdir(parents=True, exist_ok=True)
    return d


def domain(addr: str | None) -> str:
    return (addr or "").partition("@")[2]


def lookalike(addr: str) -> str:
    """A spoofed sender one character off the real domain (classic payment-redirection fraud)."""
    user, _, dom = addr.partition("@")
    name, _, tld = dom.rpartition(".")
    swaps = {"l": "1", "o": "0", "i": "l", "m": "rn"}
    for k, v in swaps.items():
        if k in name:
            return f"{user}@{name.replace(k, v, 1)}.{tld}"
    return f"{user}@{name}-billing.{tld}"


def write(sender: str, to: str, subject: str, body: str, sent_at, attachments: list[str] | None = None,
          reply_to: str | None = None, in_reply_to: str | None = None) -> dict:
    """Write one .eml; attachments are doc refs whose PDFs get attached. Returns the email.received payload."""
    m = EmailMessage()
    msg_id = make_msgid(domain=domain(sender) or "mail.example")
    m["Message-ID"], m["From"], m["To"], m["Subject"] = msg_id, sender, to, subject
    m["Date"] = format_datetime(sent_at)
    if reply_to:
        m["Reply-To"] = reply_to
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m["X-GGF-Simulated"] = "true; never delivered"
    m.set_content(body)
    names = []
    for ref in attachments or []:
        name = f"{ref.removeprefix('doc:')}.pdf"
        m.add_attachment((docs_dir() / name).read_bytes(), maintype="application", subtype="pdf", filename=name)
        names.append(name)
    mail_id = f"MAIL-{uuid.uuid4().hex[:10]}"
    (mail_dir() / f"{mail_id}.eml").write_bytes(bytes(m))
    return {"mail_id": mail_id, "message_id": msg_id, "from": sender, "to": to, "reply_to": reply_to,
            "subject": subject, "sent_at": sent_at.isoformat(), "attachments": names, "snippet": body[:200]}
