import assert from "node:assert/strict";
import test from "node:test";
import {
  evaluateFormula,
  formatInput,
  formatValue,
  inputLabel,
  inputSource,
  inputUnit,
  layoutWorking,
  prettyOperators,
  isPolicyId,
  parseChecks,
  parseWorkings,
  readableName,
  recheckWorking,
  resultFacts,
  substituteFormula,
  type Working,
} from "./workings";

const delta: Working = {
  label: "Delta vs contract",
  formula: "invoice_amount - contract_rate",
  inputs: { invoice_amount: 2150, contract_rate: 2000 },
  result: 150,
  unit: "usd",
  evidence_refs: ["doc:INV-LD-1", "doc:RC-LD-1"],
  basis: "P-001",
};

test("parseWorkings keeps valid rows and tolerates junk", () => {
  assert.deepEqual(parseWorkings([delta]), [delta]);
  assert.deepEqual(parseWorkings(JSON.stringify([delta])), [delta]);
  assert.deepEqual(parseWorkings(undefined), []);
  assert.deepEqual(parseWorkings("not json"), []);
  assert.deepEqual(parseWorkings({ label: "x" }), []);
  const [w] = parseWorkings([null, 3, { label: "Only label", unit: "parsecs", evidence_refs: ["a", "a", 5, ""] }]);
  assert.equal(w.unit, "text");
  assert.deepEqual(w.evidence_refs, ["a"]);
  assert.deepEqual(w.inputs, {});
  assert.equal(w.basis, "");
});

test("parseChecks defaults missing severity to issue and parses work", () => {
  const checks = parseChecks(
    JSON.stringify([
      { name: "debits_equal_credits", ok: true, expected: "balanced", actual: "balanced" },
      { name: "cites_source", ok: false, expected: true, actual: false, severity: "advisory", basis: "every number cites a document", work: delta },
      { ok: true },
    ]),
  );
  assert.equal(checks.length, 2);
  assert.equal(checks[0].severity, "issue");
  assert.equal(checks[0].basis, "");
  assert.equal(checks[0].work, undefined);
  assert.equal(checks[1].severity, "advisory");
  assert.deepEqual(checks[1].work, delta);
  assert.deepEqual(parseChecks("[broken"), []);
});

test("formatValue by unit", () => {
  assert.equal(formatValue(1234.5, "usd"), "$1,234.50");
  assert.equal(formatValue(-150, "usd"), "-$150.00");
  assert.equal(formatValue(47, "min"), "47 min");
  assert.equal(formatValue(2.5, "h"), "2.5 h");
  assert.equal(formatValue(1160, "mi"), "1,160 mi");
  assert.equal(formatValue(true, "bool"), "yes");
  assert.equal(formatValue(false, "usd"), "no");
  assert.equal(formatValue(0, "bool"), "no");
  assert.equal(formatValue(3, "count"), "3");
  assert.equal(formatValue(1.02, "ratio"), "1.02");
  assert.equal(formatValue("Net 30", "text"), "Net 30");
  assert.equal(formatValue(null, "usd"), "—");
  assert.equal(formatValue({ a: 1 }, "text"), '{"a":1}');
});

test("input units follow key hints, then the result unit", () => {
  assert.equal(inputUnit("free_time_min", 120, "usd"), "min");
  assert.equal(inputUnit("tolerance_pct", 1.02, "usd"), "ratio");
  assert.equal(inputUnit("invoice_amount", 2150, "bool"), "usd");
  assert.equal(inputUnit("detention_rate_hr", 75, "usd"), "usd");
  assert.equal(inputUnit("claimed", 120, "min"), "min");
  assert.equal(inputUnit("x", 3, "bool"), "ratio");
  assert.equal(inputUnit("approved", true, "usd"), "bool");
  assert.equal(inputUnit("detention_hours_claimed", 3, "bool"), "h");
  assert.equal(inputUnit("ceiling", 2040, "bool"), "usd");
  assert.equal(inputUnit("tolerance", 0.02, "usd"), "ratio");
});

test("substituteFormula replaces input identifiers only", () => {
  const parts = substituteFormula({ formula: "max(0, on_site_min - free_time_min)", inputs: { on_site_min: 47, free_time_min: 120 }, unit: "min" });
  assert.equal(parts.map((p) => p.text).join(""), "max(0, 47 min - 120 min)");
  assert.deepEqual(parts.filter((p) => p.kind === "value").map((p) => p.key), ["on_site_min", "free_time_min"]);
  assert.equal(parts[0].kind, "ident");
  assert.equal(substituteFormula(delta).map((p) => p.text).join(""), "$2,150.00 - $2,000.00");
});

test("evaluateFormula handles arithmetic, comparisons and functions without eval", () => {
  assert.equal(evaluateFormula("invoice_amount - contract_rate", delta.inputs), 150);
  assert.equal(evaluateFormula("contract * 1.02", { contract: 2000 }), 2040);
  assert.equal(evaluateFormula("a <= b and not c", { a: 1, b: 2, c: false }), true);
  assert.equal(evaluateFormula("max(0, a - b)", { a: 47, b: 120 }), 0);
  assert.equal(evaluateFormula("round(a / b, 2)", { a: 1, b: 3 }), 0.33);
  assert.equal(evaluateFormula("(a + b) * 2 ** 2", { a: 1, b: 2 }), 12);
  assert.equal(evaluateFormula("-a + 5", { a: 2 }), 3);
  assert.equal(evaluateFormula("a == b", { a: 0.1 + 0.2, b: 0.3 }), true);
  assert.equal(evaluateFormula("a × b", { a: 3, b: 4 }), 12);
  // Formulas as the Python agents publish them.
  const hold = "invoice_amount if bank_change else max(invoice_amount - contract_rate, unsupported)";
  assert.equal(evaluateFormula(hold, { invoice_amount: 2150, contract_rate: 2000, unsupported: 0, bank_change: false }), 150);
  assert.equal(evaluateFormula(hold, { invoice_amount: 2150, contract_rate: 2000, unsupported: 0, bank_change: true }), 2150);
  assert.equal(evaluateFormula("max(gps_dwell_min - free_time_min, 0) / 60", { gps_dwell_min: 210, free_time_min: 120 }), 1.5);
  assert.equal(evaluateFormula("contract_rate * (1 + tolerance)", { contract_rate: 2000, tolerance: 0.02 }), 2040);
  assert.equal(evaluateFormula("bank_change or invoice_amount > contract_rate * 1.02 or unsupported > 0", { bank_change: false, invoice_amount: 2150, contract_rate: 2000, unsupported: 0 }), true);
  assert.equal(evaluateFormula("gps_dwell_min and free_time_min recorded", { gps_dwell_min: 1, free_time_min: 1 }), undefined);
  assert.equal(evaluateFormula("sum(receivable amount - collected)", {}), undefined);
  assert.equal(evaluateFormula("a if b", { a: 1, b: true }), undefined);
  // Unknown identifiers, functions, syntax and division by zero stay unchecked.
  assert.equal(evaluateFormula("a - missing", { a: 1 }), undefined);
  assert.equal(evaluateFormula("constructor(1)", {}), undefined);
  assert.equal(evaluateFormula("a / b", { a: 1, b: 0 }), undefined);
  assert.equal(evaluateFormula("a +", { a: 1 }), undefined);
  assert.equal(evaluateFormula("a b", { a: 1, b: 2 }), undefined);
  assert.equal(evaluateFormula("sum(lines)", { lines: [1, 2] }), undefined);
  assert.equal(evaluateFormula("label", { label: "text" }), undefined);
  assert.equal(evaluateFormula("toString", {}), undefined);
});

test("recheckWorking compares the stated result", () => {
  assert.deepEqual(recheckWorking(delta), { status: "match", computed: 150 });
  assert.equal(recheckWorking({ ...delta, result: 140 }).status, "mismatch");
  assert.equal(recheckWorking({ ...delta, result: 150.004 }).status, "match");
  // Agents round hours to 0.01; a correctly rounded result is a match, not an accusation.
  assert.equal(recheckWorking({ formula: "max(d - f, 0) / 60", inputs: { d: 200, f: 120 }, result: 1.33, unit: "h" }).status, "match");
  assert.equal(recheckWorking({ formula: "max(d - f, 0) / 60", inputs: { d: 200, f: 120 }, result: 1.4, unit: "h" }).status, "mismatch");
  assert.equal(recheckWorking({ formula: "a <= b", inputs: { a: 1, b: 2 }, result: true, unit: "bool" }).status, "match");
  assert.equal(recheckWorking({ formula: "a <= b", inputs: { a: 3, b: 2 }, result: true, unit: "bool" }).status, "mismatch");
  assert.equal(recheckWorking({ formula: "lookup(carrier)", inputs: {}, result: "ACTIVE", unit: "text" }).status, "unchecked");
  assert.equal(recheckWorking({ formula: "a", inputs: { a: 1 }, result: "n/a", unit: "count" }).status, "unchecked");
});

test("names and policy ids", () => {
  assert.equal(readableName("hold_matches_delta"), "Hold matches delta");
  assert.equal(isPolicyId("P-001"), true);
  assert.equal(isPolicyId("every number cites a document"), false);
});

const at = (w: Partial<Working> & Pick<Working, "formula" | "inputs">): Working => ({ label: "x", result: null, unit: "usd", evidence_refs: [], basis: "", ...w });

test("input labels read as words and cite the document they came from", () => {
  assert.equal(inputLabel("gps_dwell_min"), "GPS dwell");
  assert.equal(inputLabel("invoice_amount"), "Carrier invoice");
  assert.equal(inputLabel("deposit_2"), "Deposit 2");
  assert.equal(inputLabel("received_before_payment_h"), "Received before payment");
  assert.equal(inputSource("contract_rate", ["doc:INV-LD-1", "doc:RC-LD-1"]), "doc:RC-LD-1");
  assert.equal(inputSource("contract_rate", ["doc:INV-LD-1"]), undefined);
  assert.equal(inputSource("deposit_1", ["doc:RC-LD-1"]), undefined);
  assert.equal(formatInput("tolerance", 0.02, "usd"), "2%");
  assert.equal(formatInput("tolerance_pct", 1.02, "usd"), "1.02");
  assert.equal(formatInput("contract_rate", 1000, "usd"), "$1,000.00");
  assert.equal(prettyOperators(" * (1 + "), " × (1 + ");
  assert.equal(prettyOperators(" <= "), " ≤ ");
  assert.equal(prettyOperators(" - "), " − ");
});

test("sums and differences lay out as a tally", () => {
  const layout = layoutWorking(delta);
  assert.equal(layout.kind, "tally");
  if (layout.kind !== "tally") return;
  assert.deepEqual(
    layout.lines.map((l) => [l.op, l.label, l.value, l.source]),
    [
      ["", "Carrier invoice", "$2,150.00", "doc:INV-LD-1"],
      ["−", "Contract rate", "$2,000.00", "doc:RC-LD-1"],
    ],
  );
  const lines = layoutWorking(at({ formula: "linehaul + fsc + detention", inputs: { linehaul: 1800, fsc: 250, detention: 100 }, result: 2150 }));
  assert.equal(lines.kind, "tally");
  if (lines.kind === "tally") assert.deepEqual(lines.lines.map((l) => l.op), ["", "+", "+"]);
  // Anything that is not a flat a ± b chain over numeric inputs is an expression.
  assert.equal(layoutWorking(at({ formula: "contract_rate * (1 + tolerance)", inputs: { contract_rate: 1000, tolerance: 0.02 } })).kind, "expression");
  assert.equal(layoutWorking(at({ formula: "deposit_1", inputs: { deposit_1: 5 } })).kind, "tally");
  assert.deepEqual(layoutWorking(at({ formula: "0", inputs: {} })), { kind: "expression", inputs: [], parts: [] });
  assert.equal(layoutWorking(at({ formula: "a - b", inputs: { a: 1, b: "x" } })).kind, "expression");
});

test("comparisons show both sides, the verdict and the gap", () => {
  const over = layoutWorking(at({ formula: "detention_hours_claimed <= supportable_hours", inputs: { detention_hours_claimed: 2, supportable_hours: 0.78 }, result: false, unit: "bool" }));
  assert.equal(over.kind, "compare");
  if (over.kind !== "compare") return;
  assert.equal(over.op, "≤");
  assert.equal(over.holds, false);
  assert.equal(over.left.value, "2 h");
  assert.equal(over.gap, "1.22 h over");
  const equal = layoutWorking(at({ formula: "line_items_total == invoice_amount", inputs: { line_items_total: 10, invoice_amount: 10 }, result: true, unit: "bool" }));
  assert.equal(equal.kind === "compare" && equal.holds, true);
  assert.equal(equal.kind === "compare" && equal.gap, undefined);
});

test("expressions list their inputs before the substituted equation", () => {
  const layout = layoutWorking(at({ formula: "max(gps_dwell_min - free_time_min, 0) / 60", inputs: { gps_dwell_min: 167, free_time_min: 120 }, result: 0.78, unit: "h" }));
  assert.equal(layout.kind, "expression");
  if (layout.kind !== "expression") return;
  assert.deepEqual(layout.inputs.map((o) => `${o.label} ${o.value}`), ["GPS dwell 167 min", "Free time 120 min"]);
});

// As agents/cfo/main.py publishes them.
const collections: Working = at({
  label: "Expected collections",
  formula: "sum(receivable amount - collected)",
  inputs: { receivable_1: { amount: 2400, collected: 400 }, receivable_2: { amount: 600, collected: 0 } },
  result: 2600,
});
// As agents/controller/main.py publishes them.
const journal: Working = at({
  label: "Journal balances",
  formula: "sum(debits) == sum(credits)",
  inputs: { ap_1: { credit: 2150, debit: 0 }, expense_2: { debit: 2150, credit: 0 } },
  result: { debits: 2150, credits: 2150, balanced: true },
  unit: "text",
  basis: "P-006",
});

test("per-line sums lay out as a table with totals and recheck", () => {
  const layout = layoutWorking(collections);
  assert.equal(layout.kind, "table");
  if (layout.kind !== "table") return;
  assert.deepEqual(layout.columns.map((c) => c.label), ["Amount", "Collected"]);
  assert.deepEqual(layout.rows.map((r) => [r.label, ...r.cells, r.net]), [
    ["Receivable 1", "$2,400.00", "$400.00", "$2,000.00"],
    ["Receivable 2", "$600.00", "$0.00", "$600.00"],
  ]);
  assert.deepEqual(layout.totals, ["$3,000.00", "$400.00"]);
  assert.deepEqual(layout.net, { label: "amount − collected", total: "$2,600.00" });
  assert.deepEqual(recheckWorking(collections), { status: "match", computed: 2600 });
  assert.equal(recheckWorking({ ...collections, result: 3000 }).status, "mismatch");
});

test("journal lines compare debit and credit totals; the object result is facts, not JSON", () => {
  const layout = layoutWorking(journal);
  assert.equal(layout.kind, "table");
  if (layout.kind !== "table") return;
  assert.deepEqual(layout.columns.map((c) => c.key), ["debit", "credit"]);
  assert.deepEqual(layout.rows[0].cells, ["$0.00", "$2,150.00"]);
  assert.deepEqual(layout.totals, ["$2,150.00", "$2,150.00"]);
  assert.deepEqual(layout.comparison && [layout.comparison.left.label, layout.comparison.op, layout.comparison.right.value, layout.comparison.holds], ["Debits", "=", "$2,150.00", true]);
  assert.deepEqual(recheckWorking(journal), { status: "match", computed: true });
  assert.equal(recheckWorking({ ...journal, result: { debits: 2150, credits: 2150, balanced: false } }).status, "mismatch");
  assert.equal(recheckWorking({ ...journal, result: { debits: 9, credits: 2150, balanced: true } }).status, "mismatch");
  assert.deepEqual(resultFacts(journal.result, "text"), [
    { key: "debits", label: "Debits", value: "$2,150.00" },
    { key: "credits", label: "Credits", value: "$2,150.00" },
    { key: "balanced", label: "Balanced", value: "yes", tone: "yes" },
  ]);
  assert.equal(resultFacts(150, "usd"), null);
});

test("document packets, notes and lists", () => {
  const packet = at({
    formula: "required_documents - received_documents",
    inputs: { required_documents: ["rate_confirmation", "pod"], received_documents: ["rate_confirmation"] },
    result: ["pod"],
    unit: "text",
  });
  const layout = layoutWorking(packet);
  assert.equal(layout.kind, "set");
  if (layout.kind === "set") assert.deepEqual(layout.items.map((d) => [d.label, d.present]), [["Rate confirmation", true], ["POD", false]]);
  assert.equal(recheckWorking(packet).status, "match");
  assert.equal(recheckWorking({ ...packet, result: [] }).status, "mismatch");
  assert.equal(formatValue(["pod"], "text"), "POD");
  assert.equal(formatValue([], "text"), "none");
  const note = layoutWorking(at({ formula: "lumper (no receipt attached)", inputs: { lumper: 150, receipt_ref: null }, result: 150 }));
  assert.deepEqual(note.kind === "note" && [note.line.label, note.line.value, note.reason], ["Lumper", "$150.00", "no receipt attached"]);
  // A function call is not a note.
  assert.equal(layoutWorking(at({ formula: "abs(delta)", inputs: { delta: -5 } })).kind, "expression");
});
