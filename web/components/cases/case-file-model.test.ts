import assert from "node:assert/strict";
import test from "node:test";
import type { Row } from "@/lib/data";
import { buildCaseFile, caseFileSummary, docKindLabel, type CaseFile } from "./case-file-model";

const ID = "CASE-LD-1025-0E60ED88";

/** The record ingest mirrors for CASE-LD-1025-0E60ED88, trimmed. */
const RECORD = {
  bol: "BOL28812509",
  carrier: { id: "C-OFF-42", mc: "MC-604192", name: "Prairie Freight LLC", usdot: "2253408" },
  carrier_id: "C-OFF-42",
  contract_rate: 1829.29,
  deposits: [{ amount: 2029.29, id: "BNK-98D0D41299", invoice_id: "SINV-LD-1025-0E60ED88", posted_at: "2027-06-20T05:15:36.346755Z", shipper_id: "S-OFF-13" }],
  detention_hours_claimed: 0,
  documents: [
    { kind: "rate_confirmation", load_id: "LD-1025-0E60ED88", ref: "doc:RC-LD-1025-0E60ED88" },
    { kind: "bill_of_lading", load_id: "LD-1025-0E60ED88", ref: "doc:BOL-LD-1025-0E60ED88" },
    { kind: "pod", load_id: "LD-1025-0E60ED88", ref: "doc:POD-LD-1025-0E60ED88", signed: true },
    { kind: "carrier_invoice", load_id: "LD-1025-0E60ED88", ref: "doc:INV-LD-1025-0E60ED88-1" },
  ],
  evidence_refs: ["doc:RC-LD-1025-0E60ED88", "doc:BOL-LD-1025-0E60ED88", "doc:POD-LD-1025-0E60ED88", "doc:INV-LD-1025-0E60ED88-1", "mail:MAIL-51f503982c"],
  free_time_min: 120,
  gps: { delivery_dwell_min: 51, free_time_min: 120 },
  gps_dwell_min: 51,
  ingest_flags: [],
  invoice_amount: 1829.29,
  invoice_carrier_usdot: "2253408",
  required_documents: ["rate_confirmation", "pod"],
  shipper: { id: "S-OFF-13", name: "Sunrise Machinery Inc" },
  shipper_invoice_amount: 2029.29,
  shipper_invoice_id: "SINV-LD-1025-0E60ED88",
};

const row = (record: unknown, patch: Row = {}): Row => ({
  id: `case:${ID}`,
  kind: "case",
  case_id: ID,
  record_json: typeof record === "string" ? record : JSON.stringify(record),
  digest: "d",
  updated_at: "2026-10-25T08:59:25.124136Z",
  ...patch,
});

function file(record: unknown): CaseFile {
  const result = buildCaseFile([row(record)], ID);
  assert.equal(result.state, "ready");
  return (result as { file: CaseFile }).file;
}

test("a full case record lists every document, email and bank line", () => {
  const f = file(RECORD);
  assert.deepEqual(
    f.documents.map((d) => [d.label, d.ref]),
    [
      ["Rate confirmation", "doc:RC-LD-1025-0E60ED88"],
      ["Bill of lading", "doc:BOL-LD-1025-0E60ED88"],
      ["Proof of delivery", "doc:POD-LD-1025-0E60ED88"],
      ["Carrier invoice", "doc:INV-LD-1025-0E60ED88-1"],
      ["Shipper invoice", "doc:SINV-LD-1025-0E60ED88"],
    ],
  );
  assert.equal(f.documents.find((d) => d.kind === "pod")?.signed, true);
  assert.equal(f.documents.find((d) => d.kind === "shipper_invoice")?.amount, 2029.29);
  assert.deepEqual(f.missing, []);
  assert.deepEqual(f.emails, ["mail:MAIL-51f503982c"]);
  assert.deepEqual(f.bank, [
    { id: "BNK-98D0D41299", amount: 2029.29, postedAt: "2027-06-20T05:15:36.346755Z", invoiceId: "SINV-LD-1025-0E60ED88", shipperId: "S-OFF-13", unapplied: false, remittanceRef: "" },
  ]);
  assert.equal(f.billed, 2029.29);
  assert.equal(f.deposited, 2029.29);
  assert.deepEqual(f.carrier, { name: "Prairie Freight LLC", id: "C-OFF-42", usdot: "2253408", mc: "MC-604192" });
  assert.equal(f.invoiceCarrier, null);
  assert.equal(f.shipper, "Sunrise Machinery Inc");
  assert.equal(f.bol, "BOL28812509");
  assert.deepEqual(f.rate, { contract: 1829.29, invoice: 1829.29, delta: 0 });
  assert.deepEqual(f.detention, { claimedHours: 0, dwellMin: 51, freeTimeMin: 120, billableHours: 0, overClaimed: false });
  assert.deepEqual(f.flags, []);
  assert.equal(f.recordId, `case:${ID}`);
  assert.equal(caseFileSummary(f), "5 docs · 1 email · 1 bank line");
});

test("cited documents the list leaves out are inferred from their ref, once", () => {
  const f = file({ documents: [{ kind: "pod", ref: "doc:POD-X" }], evidence_refs: ["doc:POD-X#p2", "doc:DET-X", "doc:RC-X", "doc:ZZZ"], pod_signed: false });
  assert.deepEqual(
    f.documents.map((d) => [d.kind, d.ref]),
    [
      ["rate_confirmation", "doc:RC-X"],
      ["pod", "doc:POD-X"],
      ["detention_log", "doc:DET-X"],
      ["document", "doc:ZZZ"],
    ],
  );
  // The record's pod_signed fills in for a document entry that does not say.
  assert.equal(f.documents.find((d) => d.kind === "pod")?.signed, false);
  assert.equal(f.documents.find((d) => d.kind === "document")?.label, "Document");
});

test("required documents with no document are missing", () => {
  const f = file({ documents: [{ kind: "rate_confirmation", ref: "doc:RC-1" }], required_documents: ["rate_confirmation", "pod", "pod"] });
  assert.deepEqual(f.missing, [{ kind: "pod", label: "Proof of delivery" }]);
});

test("over-contract invoices, detention over GPS and identity flags", () => {
  const f = file({
    contract_rate: 1000,
    invoice_amount: "1250.5",
    detention_hours_claimed: 3,
    gps_dwell_min: 210,
    free_time_min: 120,
    carrier: { name: "Tendered LLC", usdot: "111" },
    carrier_identity: { flags: ["usdot_mismatch", "carrier_mismatch"], invoice_carrier_name: "Other LLC", invoice_usdot: "222" },
    ingest_flags: ["usdot_mismatch"],
  });
  assert.deepEqual(f.rate, { contract: 1000, invoice: 1250.5, delta: 250.5 });
  assert.deepEqual(f.detention, { claimedHours: 3, dwellMin: 210, freeTimeMin: 120, billableHours: 1.5, overClaimed: true });
  assert.deepEqual(f.invoiceCarrier, { name: "Other LLC", usdot: "222" });
  assert.deepEqual(f.flags, ["usdot_mismatch", "carrier_mismatch"]);
});

test("unapplied deposits, short pays and bank change emails", () => {
  const f = file({
    shipper_invoice_id: "SINV-9",
    shipper_id: "S-1",
    shipper_invoice_amount: 3000,
    deposits: [{ amount: 1000, id: "BNK-1", invoice_id: "SINV-9", shipper_id: "S-1" }],
    unapplied_deposits: [{ amount: 3280.4, id: "BNK-2", candidate_invoice_id: "SINV-9", remittance_ref: "SINV-MISSING-9", posted_at: "2026-11-10T08:17:33Z" }],
    bank_change: { new_payee: "Keystone Freight Capital LLC", new_account_last4: "0775", channel: "email", source_email: "MAIL-38af20d4f2", urgency_language: "URGENT", sender_matches_registered: false },
    evidence_refs: ["mail:MAIL-197b15f163", "fixture://x"],
  });
  assert.deepEqual(
    f.bank.map((b) => [b.id, b.unapplied, b.invoiceId, b.remittanceRef]),
    [
      ["BNK-1", false, "SINV-9", ""],
      ["BNK-2", true, "SINV-9", "SINV-MISSING-9"],
    ],
  );
  // Only applied deposits count as received.
  assert.equal(f.deposited, 1000);
  assert.deepEqual(f.emails, ["mail:MAIL-197b15f163", "mail:MAIL-38af20d4f2"]);
  assert.deepEqual(f.otherRefs, ["fixture://x"]);
  assert.deepEqual(f.bankChange, { payee: "Keystone Freight Capital LLC", accountLast4: "0775", via: "email", urgency: "URGENT", senderMatches: false });
});

test("received counts only cash applied to this invoice from this shipper", () => {
  const f = file({
    shipper_invoice_id: "SINV-A",
    shipper_id: "S-1",
    shipper_invoice_amount: 1000,
    deposits: [
      { amount: 300, id: "BNK-1", invoice_id: "SINV-A", shipper_id: "S-1" },
      { amount: 700, id: "BNK-2", invoice_id: "SINV-B", shipper_id: "S-1" },
      { amount: 50, id: "BNK-3", invoice_id: "SINV-A", shipper_id: "S-2" },
    ],
  });
  // Every line is still listed; only the matching one is received.
  assert.equal(f.bank.length, 3);
  assert.equal(f.deposited, 300);
});

test("a shipper invoice already cited keeps its billed amount", () => {
  const f = file({ evidence_refs: ["doc:SINV-A"], shipper_invoice_id: "SINV-A", shipper_invoice_amount: 1000 });
  assert.deepEqual(
    f.documents.map((d) => [d.ref, d.kind, d.amount]),
    [["doc:SINV-A", "shipper_invoice", 1000]],
  );
});

test("no row, other cases' rows and unreadable records", () => {
  assert.deepEqual(buildCaseFile([], ID), { state: "none" });
  assert.deepEqual(buildCaseFile([row(RECORD, { id: "case:OTHER", case_id: "OTHER" })], ID), { state: "none" });
  assert.deepEqual(buildCaseFile([row("{not json")], ID), { state: "invalid", recordId: `case:${ID}` });
  assert.deepEqual(buildCaseFile([row([1, 2])], ID), { state: "invalid", recordId: `case:${ID}` });
});

test("an empty or odd record still builds, with nothing invented", () => {
  const f = file({ documents: "nope", deposits: [null, 4], evidence_refs: [7, "", "mail:../../etc"], carrier: null, gps: [] });
  assert.deepEqual(f.documents, []);
  assert.deepEqual(f.bank, []);
  assert.deepEqual(f.emails, []);
  // An unsafe mail ref is not openable, so it is listed as another source.
  assert.deepEqual(f.otherRefs, ["mail:../../etc"]);
  assert.equal(f.carrier, null);
  assert.equal(f.rate, null);
  assert.equal(f.detention, null);
  assert.equal(f.billed, null);
  assert.equal(caseFileSummary(f), "Empty");
});

test("document kind labels", () => {
  assert.equal(docKindLabel("pod"), "Proof of delivery");
  assert.equal(docKindLabel("lumper_receipt"), "Lumper receipt");
  assert.equal(docKindLabel("fuel_ticket"), "Fuel ticket");
});
