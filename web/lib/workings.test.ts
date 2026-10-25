import assert from "node:assert/strict";
import test from "node:test";
import {
  evaluateFormula,
  formatValue,
  inputUnit,
  isPolicyId,
  parseChecks,
  parseWorkings,
  readableName,
  recheckWorking,
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
