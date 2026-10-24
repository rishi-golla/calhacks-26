"""Operating-account bank feed: one BAI2-style file per business day under data/bank/, plus a structured
transaction for the event stream. Incoming shipper ACH/wires are type 165/195; outgoing carrier ACH is 455."""
import os
import pathlib
import uuid
from datetime import datetime

ACCOUNT = "GGF-OPERATING-0042"
TYPE_CODES = {"ach_credit": "165", "wire_credit": "195", "ach_debit": "455"}


def bank_dir() -> pathlib.Path:
    d = pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / "bank"
    d.mkdir(parents=True, exist_ok=True)
    return d


def post(kind: str, amount: float, counterparty: str, reference: str, at: datetime) -> dict:
    """Append a 16-record to the day's BAI2 file and return the bank.transaction payload."""
    txn_id = f"BNK-{uuid.uuid4().hex[:10].upper()}"
    cents = round(amount * 100)
    path = bank_dir() / f"BAI2-{at:%Y%m%d}.txt"
    if not path.exists():
        path.write_text(f"01,GGFBANK,GGF,{at:%y%m%d},0000,1,,,2/\n02,GGF,GGFBANK,1,{at:%y%m%d},,USD,2/\n"
                        f"03,{ACCOUNT},USD,/\n")
    record = f"16,{TYPE_CODES[kind]},{cents},0,{txn_id},{reference},{counterparty}/"
    with path.open("a") as f:
        f.write(record + "\n")
    return {"txn_id": txn_id, "account": ACCOUNT, "kind": kind, "type_code": TYPE_CODES[kind],
            "amount": round(amount, 2), "counterparty": counterparty, "reference": reference,
            "posted_at": at.isoformat(), "file": path.name, "record": record}
