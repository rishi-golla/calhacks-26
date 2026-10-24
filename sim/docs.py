"""Source documents as a brokerage actually receives them: PDF rate cons, BOLs, signed PODs, carrier invoices
(each carrier has its own layout and numbering), shipper invoices, lumper receipts and detention logs.
Each document is written twice under data/docs/: `<ref>.pdf` (what a human or OCR sees) and `<ref>.json`
(ground truth for scoring extraction)."""
import hashlib
import json
import os
import pathlib

from fpdf import FPDF

BROKER = {"name": "Golden Gate Freight (fictional brokerage)", "address": "1 Ferry Building, San Francisco, CA 94111",
          "mc": "MC-000000 (fictional)", "ap_email": "ap@goldengatefreight.example"}


def docs_dir() -> pathlib.Path:
    d = pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _t(v) -> str:
    return str("" if v is None else v).encode("latin-1", "replace").decode("latin-1")


def _money(v: float) -> str:
    return f"${v:,.2f}"


class _Doc(FPDF):
    def __init__(self, title: str, issuer: dict, accent: tuple[int, int, int] = (20, 60, 120), style: int = 0):
        super().__init__(format="Letter")
        self.set_auto_page_break(True, 15)
        self.add_page()
        self.accent, self.style = accent, style
        self.set_fill_color(*accent)
        if style == 1:
            self.rect(0, 0, 216, 22, "F")
            self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 15)
        self.set_xy(10, 6)
        self.cell(120, 8, _t(issuer.get("name")))
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 8, _t(title), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 8)
        for line in (issuer.get("address"), " | ".join(_t(x) for x in (issuer.get("mc"), issuer.get("usdot") and
                     f"USDOT {issuer['usdot']}", issuer.get("phone"), issuer.get("email")) if x)):
            if line:
                self.cell(0, 4, _t(line), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        if style != 1:
            self.set_draw_color(*accent)
            self.line(10, self.get_y() + 1, 206, self.get_y() + 1)
        self.ln(5)

    def kv(self, rows: list[tuple[str, object]], cols: int = 2) -> None:
        w = 196 / cols
        for i, (k, v) in enumerate(rows):
            self.set_font("Helvetica", "B", 8)
            self.cell(w * 0.38, 5, _t(k))
            self.set_font("Helvetica", "", 8)
            self.cell(w * 0.62, 5, _t(v)[:60], new_x="LMARGIN" if i % cols == cols - 1 else "RIGHT",
                      new_y="NEXT" if i % cols == cols - 1 else "TOP")
        self.ln(7)

    def block(self, title: str, lines: list[str]) -> None:
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*self.accent)
        self.cell(0, 5, _t(title), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 8)
        for ln in lines:
            if ln:
                self.cell(0, 4, _t(ln), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def table(self, head: list[str], rows: list[list], widths: list[int]) -> None:
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(230, 235, 242)
        for h, w in zip(head, widths, strict=True):
            self.cell(w, 6, _t(h), border=1, fill=True)
        self.ln()
        self.set_font("Helvetica", "", 8)
        for r in rows:
            for v, w in zip(r, widths, strict=True):
                self.cell(w, 6, _t(v), border=1, align="R" if isinstance(v, str) and v.startswith("$") else "L")
            self.ln()
        self.ln(3)

    def sign(self, label: str, name: str | None, when: str | None = None) -> None:
        self.ln(4)
        self.set_font("Helvetica", "I" if name else "", 10 if name else 8)
        self.cell(90, 6, _t(name or ""), border="B")
        self.set_font("Helvetica", "", 8)
        self.cell(10, 6, "")
        self.cell(60, 6, _t(when or ""), border="B", new_x="LMARGIN", new_y="NEXT")
        self.cell(100, 4, _t(label))
        self.cell(60, 4, "Date / time", new_x="LMARGIN", new_y="NEXT")


def _party(c: dict) -> list[str]:
    return [c.get("legal_name") or c.get("name"), c.get("address"),
            " | ".join(x for x in (c.get("mc"), c.get("usdot") and f"USDOT {c['usdot']}", c.get("phone"),
                                   c.get("email")) if x)]


def carrier_style(usdot: str) -> int:
    return int(hashlib.sha1(str(usdot).encode()).hexdigest(), 16) % 3


def invoice_number(carrier: dict, seq: int) -> str:
    """Carrier-specific numbering, like real carriers' billing systems."""
    style, n = carrier_style(carrier["usdot"]), 1000 + seq
    initials = "".join(w[0] for w in (carrier.get("name") or "X").split()[:3] if w[0].isalpha()).upper() or "C"
    return [f"{initials}-{n:05d}", f"{n:06d}", f"INV{str(carrier['usdot'])[-3:]}{n:04d}"][style]


def rate_confirmation(d: dict) -> FPDF:
    p = _Doc("RATE CONFIRMATION", BROKER)
    p.kv([("Load #", d["load_id"]), ("Date", d["date"]), ("PRO #", d["pro"]), ("PO #", d["po"]),
          ("Equipment", d["equipment"]), ("Commodity", d["commodity"]), ("Weight", f"{d['weight_lb']:,} lb"),
          ("Pallets", d["pallets"]), ("Miles", d["miles"]), ("Temp", d.get("temp_f") or "n/a")])
    p.block("CARRIER", _party(d["carrier"]))
    p.block("PICKUP", [d["shipper"]["name"], d["shipper"].get("address"), f"Appt: {d['pickup_appt']}"])
    p.block("DELIVERY", [d["consignee"]["name"], d["consignee"].get("address"), f"Appt: {d['delivery_appt']}"])
    p.table(["Charge", "Basis", "Amount"], [["Linehaul", "flat", _money(d["linehaul"])],
            ["Fuel surcharge", f"{d['miles']} mi x ${d['fsc_per_mile']:.3f}", _money(d["fsc"])],
            ["TOTAL", "", _money(d["total"])]], [80, 70, 46])
    a = d["accessorials"]
    p.block("ACCESSORIAL SCHEDULE", [
        f"Detention: {d['free_time_min']} min free, then ${d['detention_rate_hr']:.2f}/hr; requires signed in/out times",
        f"Lumper: {a['lumper']}",
        f"Layover ${a['layover']:.0f} | TONU ${a['tonu']:.0f} | Stop-off ${a['stop_off']:.0f}",
        f"Payment terms: {d['terms']}. Invoices with signed POD to {BROKER['ap_email']}.",
        "No changes to remit-to without verbal verification at the number on file."])
    p.sign("Carrier signature (dispatch)", d["carrier"].get("name"), d["date"])
    return p


def bol(d: dict) -> FPDF:
    p = _Doc("STRAIGHT BILL OF LADING", {"name": d["shipper"]["name"], "address": d["shipper"].get("address")})
    p.kv([("BOL #", d["bol"]), ("Ship date", d["ship_date"]), ("PO #", d["po"]), ("Seal #", d["seal"]),
          ("Carrier", d["carrier"]["name"]), ("Trailer", d["trailer"]), ("SCAC / USDOT", d["carrier"]["usdot"]),
          ("Load #", d["load_id"])])
    p.block("CONSIGNEE", [d["consignee"]["name"], d["consignee"].get("address")])
    p.table(["Pieces", "Type", "Description", "NMFC class", "Weight"],
            [[d["pallets"], "PLT", d["commodity"], d["nmfc_class"], f"{d['weight_lb']:,} lb"]], [20, 20, 96, 30, 30])
    p.sign("Shipper (loaded by)", d.get("loader"), d.get("loaded_at"))
    p.sign("Driver", d.get("driver"), d.get("loaded_at"))
    return p


def pod(d: dict) -> FPDF:
    p = _Doc("PROOF OF DELIVERY", {"name": d["consignee"]["name"], "address": d["consignee"].get("address")})
    p.kv([("BOL #", d["bol"]), ("Load #", d["load_id"]), ("PO #", d["po"]), ("Carrier", d["carrier"]["name"]),
          ("Arrived", d["arrived"]), ("Departed", d["departed"]), ("Pieces received", d["pallets_received"]),
          ("OS&D", d.get("osd") or "None")])
    if d.get("receiver"):
        p.sign("Received in good order by", d["receiver"], d["departed"])
    else:
        p.block("RECEIVER SIGNATURE", ["(not signed)"])
    return p


def carrier_invoice(d: dict) -> FPDF:
    c = d["carrier"]
    style = carrier_style(c["usdot"])
    p = _Doc("INVOICE", {**c, "address": c.get("address")}, [(20, 60, 120), (150, 40, 30), (30, 110, 70)][style],
             style)
    p.kv([("Invoice #", d["invoice_number"]), ("Invoice date", d["date"]), ("Load / ref #", d["load_id"]),
          ("BOL #", d["bol"]), ("Terms", d["terms"]), ("Due", d["due"])], cols=2 if style != 2 else 3)
    p.block("BILL TO", [BROKER["name"], BROKER["address"], BROKER["ap_email"]])
    p.block("REMIT TO", [d["remit_to"]["name"], d["remit_to"].get("address"),
                         f"ACH routing ****{d['remit_to']['routing_last4']} acct ****{d['remit_to']['account_last4']}"])
    p.table(["Description", "Qty", "Rate", "Amount"],
            [[li["desc"], li.get("qty", 1), _money(li.get("rate", li["amount"])), _money(li["amount"])]
             for li in d["line_items"]], [96, 20, 40, 40])
    p.set_font("Helvetica", "B", 11)
    p.cell(0, 8, _t(f"TOTAL DUE: {_money(d['total'])}"), align="R", new_x="LMARGIN", new_y="NEXT")
    p.set_font("Helvetica", "", 8)
    p.cell(0, 5, _t(f"Shipment {d['origin']} -> {d['destination']}. Signed POD attached."))
    return p


def shipper_invoice(d: dict) -> FPDF:
    p = _Doc("FREIGHT INVOICE", BROKER)
    p.kv([("Invoice #", d["invoice_id"]), ("Date", d["date"]), ("PO #", d["po"]), ("BOL #", d["bol"]),
          ("Terms", d["terms"]), ("Due", d["due"])])
    p.block("BILL TO", [d["shipper"]["name"], d["shipper"].get("address"), "Attn: Accounts Payable"])
    p.table(["Description", "Amount"], [[li["desc"], _money(li["amount"])] for li in d["line_items"]], [150, 46])
    p.set_font("Helvetica", "B", 11)
    p.cell(0, 8, _t(f"AMOUNT DUE: {_money(d['total'])}"), align="R")
    return p


def lumper_receipt(d: dict) -> FPDF:
    p = _Doc("LUMPER RECEIPT", {"name": d["lumper_co"], "address": d["consignee"].get("address")})
    p.kv([("Receipt #", d["receipt_no"]), ("Date", d["date"]), ("PO #", d["po"]), ("Carrier", d["carrier"]["name"]),
          ("Pallets unloaded", d["pallets"]), ("Amount paid", _money(d["amount"]))])
    p.sign("Unloader", d.get("unloader"), d["date"])
    return p


def detention_log(d: dict) -> FPDF:
    p = _Doc("DRIVER CHECK-IN / CHECK-OUT", {"name": d["consignee"]["name"], "address": d["consignee"].get("address")})
    p.kv([("Load #", d["load_id"]), ("BOL #", d["bol"]), ("Appointment", d["appt"]), ("Checked in", d["arrived"]),
          ("Checked out", d["departed"]), ("Time on site", f"{d['dwell_min']} min")])
    p.sign("Facility clerk", d.get("clerk"), d["departed"])
    return p


RENDER = {"rate_confirmation": rate_confirmation, "bill_of_lading": bol, "proof_of_delivery": pod,
          "carrier_invoice": carrier_invoice, "shipper_invoice": shipper_invoice, "lumper_receipt": lumper_receipt,
          "detention_log": detention_log}


def write(ref: str, kind: str, data: dict) -> str:
    """Render `kind` to <ref>.pdf and save ground truth to <ref>.json. Returns the evidence ref `doc:<ref>`."""
    d = docs_dir()
    RENDER[kind](data).output(str(d / f"{ref}.pdf"))
    (d / f"{ref}.json").write_text(json.dumps({"type": kind, "ref": ref, "pdf": f"{ref}.pdf", **data}, indent=1,
                                              default=str))
    return f"doc:{ref}"
