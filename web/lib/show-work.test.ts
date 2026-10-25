import assert from "node:assert/strict";
import test from "node:test";
import { detentionWorkings, dsoWorkings, exposureWorkings, marginWorkings, rateDeltaWorkings, shortPayWorkings, type CaseFileFigures } from "./show-work";
import { formatValue, layoutWorking, recheckWorking, type Working } from "./workings";

const statuses = (ws: Working[]) => ws.map((w) => recheckWorking(w).status);
const refs = [
  { ref: "doc:INV-LD-1-1", kind: "carrier_invoice" },
  { ref: "doc:RC-LD-1", kind: "rate_confirmation" },
  { ref: "doc:BOL-LD-1", kind: "bill_of_lading" },
];

test("payable exposure is the overage, floored at zero, as ingest computes it", () => {
  const [over] = exposureWorkings("CASE-LD-1", { invoice_id: "INV-1", invoice_amount: 2150, contract_rate: 2000 }, 150, refs);
  assert.equal(over.formula, "invoice_amount - contract_rate");
  // Cites the documents it reads, not the whole case file.
  assert.deepEqual(over.evidence_refs, ["doc:INV-LD-1-1", "doc:RC-LD-1"]);
  assert.equal(layoutWorking(over).kind, "tally");
  assert.equal(recheckWorking(over).status, "match");
  const [under] = exposureWorkings("CASE-LD-2", { invoice_amount: 1900, contract_rate: 2000 }, 0, refs);
  assert.equal(under.formula, "max(invoice_amount - contract_rate, 0)");
  assert.equal(recheckWorking(under).status, "match");
  // A stated exposure the record does not support is flagged, not hidden.
  assert.equal(recheckWorking(exposureWorkings("CASE-LD-1", { invoice_amount: 2150, contract_rate: 2000 }, 400, refs)[0]).status, "mismatch");
  assert.deepEqual(exposureWorkings("CASE-LD-3", { invoice_amount: 2150 }, 150, refs), []);
});

test("bank-change exposure is the whole payment; receivable exposure is what is unpaid", () => {
  const [bank] = exposureWorkings("CASE-LD-1-BANK", { invoice_amount: 2150 }, 2150, refs);
  assert.equal(bank.formula, "invoice_amount");
  assert.equal(recheckWorking(bank).status, "match");
  const [next] = exposureWorkings("CASE-LD-9", { bank_change_requested: true, bank_change: { next_payment_amount: 900 } }, 900, []);
  assert.deepEqual(next.inputs, { next_payment_amount: 900 });
  const [ar] = exposureWorkings("CASE-LD-1-AR", { shipper_invoice_amount: 3000, deposits: [{ amount: 1000 }, { amount: "500" }, { id: "no-amount" }] }, 1500, []);
  assert.equal(ar.formula, "shipper_invoice_amount - deposit_1 - deposit_2 - deposit_3");
  assert.equal(recheckWorking(ar).status, "match");
  const tally = layoutWorking(ar);
  assert.deepEqual(tally.kind === "tally" && tally.lines.map((l) => `${l.op}${l.value}`), ["$3,000.00", "−$1,000.00", "−$500.00", "−$0.00"]);
  const [overpaid] = exposureWorkings("CASE-LD-2-AR", { shipper_invoice_amount: 100, deposits: [{ amount: 150 }] }, 0, []);
  assert.equal(overpaid.formula, "max(shipper_invoice_amount - deposit_1, 0)");
  assert.equal(recheckWorking(overpaid).status, "match");
});

const file: CaseFileFigures = {
  billed: 2400,
  applied: [
    { id: "BNK-1", amount: 1000 },
    { id: "BNK-2", amount: 1200 },
  ],
  rate: { contract: 2000, invoice: 2150, delta: 150 },
  detention: { claimedHours: 2, dwellMin: 167, freeTimeMin: 120, billableHours: 0.78, overClaimed: true },
  documents: [
    { ref: "doc:RC-LD-1", kind: "rate_confirmation" },
    { ref: "doc:INV-LD-1-1", kind: "carrier_invoice" },
    { ref: "doc:POD-LD-1", kind: "pod" },
    { ref: "doc:BOL-LD-1", kind: "bill_of_lading" },
  ],
};

test("case file chips: short pay, rate delta and detention recheck to the chip", () => {
  const short = shortPayWorkings(file, 200);
  assert.equal(short[0].label, "Short pay (deposits BNK-1, BNK-2)");
  assert.deepEqual(statuses(short), ["match"]);
  assert.deepEqual(statuses(rateDeltaWorkings(file)), ["match"]);
  const tally = layoutWorking(rateDeltaWorkings(file)[0]);
  assert.deepEqual(tally.kind === "tally" && tally.lines.map((l) => l.source), ["doc:INV-LD-1-1", "doc:RC-LD-1"]);
  const detention = detentionWorkings(file);
  assert.deepEqual(statuses(detention), ["match", "match"]);
  assert.equal(layoutWorking(detention[1]).kind, "compare");
  assert.deepEqual(detention[0].evidence_refs, ["doc:RC-LD-1", "doc:POD-LD-1"]);
  // Nothing to show without the inputs.
  assert.deepEqual(shortPayWorkings({ ...file, billed: null }, 0), []);
  assert.deepEqual(rateDeltaWorkings({ ...file, rate: { contract: null, invoice: 10, delta: null } }), []);
  assert.deepEqual(detentionWorkings({ ...file, detention: { ...file.detention!, dwellMin: null, billableHours: null } }), []);
  assert.equal(detentionWorkings({ ...file, detention: { ...file.detention!, claimedHours: null } }).length, 1);
});

test("finance DSO and margin match the tiles", () => {
  const f = { ar: 45_000, revenue30d: 90_000, expense30d: 72_123.45, grossMarginPct: ((90_000 - 72_123.45) / 90_000) * 100 };
  const dso = dsoWorkings(f);
  assert.deepEqual(statuses(dso), ["match"]);
  assert.equal(formatValue(dso[0].result, dso[0].unit), "15");
  const margin = marginWorkings(f, "since 9:41");
  assert.deepEqual(statuses(margin), ["match", "match"]);
  assert.equal(formatValue(margin[1].result, margin[1].unit), "19.9%");
  assert.match(margin[0].basis, /since 9:41/);
  assert.equal(layoutWorking(margin[0]).kind, "tally");
  assert.deepEqual(dsoWorkings({ ...f, revenue30d: 0 }), []);
  assert.deepEqual(marginWorkings({ ...f, revenue30d: 0 }), []);
});
