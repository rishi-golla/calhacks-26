"""Operating-account bank feed: one BAI2-style file per business day under data/bank/.
Incoming shipper ACH/wires are type 165/195; outgoing carrier ACH is 455."""
import os
import pathlib
import random
import uuid
from datetime import datetime

ACCOUNT = "GGF-OPERATING-0042"
TYPE_CODES = {"ach_credit": "165", "wire_credit": "195", "ach_debit": "455"}
ABA_ROUTING_NUMBERS = ("121000358", "021000021", "011000015", "026009593", "322271627")


def aba_valid(routing_number: str) -> bool:
    if len(routing_number) != 9 or not routing_number.isdigit():
        return False
    return sum(int(digit) * (3, 7, 1)[index % 3] for index, digit in enumerate(routing_number)) % 10 == 0


assert all(aba_valid(routing_number) for routing_number in ABA_ROUTING_NUMBERS)


def random_routing_number(rnd: random.Random) -> str:
    return rnd.choice(ABA_ROUTING_NUMBERS)


def random_account_number(rnd: random.Random) -> str:
    length = rnd.randint(10, 12)
    return str(rnd.randrange(10 ** (length - 1), 10 ** length))


def bank_dir() -> pathlib.Path:
    d = pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / "bank"
    d.mkdir(parents=True, exist_ok=True)
    return d


def post(kind: str, amount: float, counterparty: str, reference: str, at: datetime) -> dict:
    """Append a 16-record to the day's BAI2 file and return its raw record and metadata."""
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
