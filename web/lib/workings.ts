/**
 * "Shown work" for agent and Validator numbers.
 *
 * Agents publish `findings.workings: Working[]`; Validator checks may carry
 * one `work`. This module parses those defensively (live rows can be old,
 * partial or malformed), formats values by unit, substitutes inputs into the
 * formula, and independently recomputes simple arithmetic formulas so the
 * dashboard can say whether the stated result follows from the stated inputs.
 * Pure: no React, no DOM.
 */

// `pct` is the dashboard's own (finance tiles); agents publish the units in common/workings.py.
export const WORKING_UNITS = ["usd", "min", "h", "mi", "ratio", "pct", "bool", "count", "text"] as const;
export type WorkingUnit = (typeof WORKING_UNITS)[number];

export type Working = {
  label: string;
  formula: string;
  inputs: Record<string, unknown>;
  result: unknown;
  unit: WorkingUnit;
  evidence_refs: string[];
  basis: string;
  /** "dashboard" when the page computed the figure itself (lib/show-work); agents never set it. */
  origin?: "dashboard";
};

export type Check = {
  name: string;
  ok: boolean;
  expected: unknown;
  actual: unknown;
  severity: "issue" | "advisory";
  basis: string;
  work?: Working;
};

const MAX_ITEMS = 60;
const MAX_TEXT = 400;

const text = (value: unknown, max = MAX_TEXT): string => (typeof value === "string" ? value.trim().slice(0, max) : "");
const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const fromJson = (value: unknown): unknown => {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
};

/** One Working, or null when the record is not recognizably one. */
export function parseWorking(value: unknown): Working | null {
  const raw = fromJson(value);
  if (!isRecord(raw)) return null;
  const label = text(raw.label, 160);
  const formula = text(raw.formula);
  if (!label && !formula) return null;
  const unit = WORKING_UNITS.includes(raw.unit as WorkingUnit) ? (raw.unit as WorkingUnit) : "text";
  const inputs = isRecord(raw.inputs) ? Object.fromEntries(Object.entries(raw.inputs).slice(0, 40)) : {};
  const refs = Array.isArray(raw.evidence_refs)
    ? [...new Set(raw.evidence_refs.filter((r): r is string => typeof r === "string" && !!r.trim()).map((r) => r.trim().slice(0, 240)))].slice(0, 20)
    : [];
  return {
    label: label || formula,
    formula,
    inputs,
    result: raw.result,
    unit,
    evidence_refs: refs,
    basis: text(raw.basis, 160),
    ...(raw.origin === "dashboard" ? { origin: "dashboard" as const } : {}),
  };
}

/** `findings.workings` (array or JSON string) -> valid Working rows. */
export function parseWorkings(value: unknown): Working[] {
  const raw = fromJson(value);
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, MAX_ITEMS).map(parseWorking).filter((w): w is Working => w !== null);
}

/** One Validator check. Older rows lack severity/basis/work; missing severity is an issue. */
export function parseCheck(value: unknown): Check | null {
  const raw = fromJson(value);
  if (!isRecord(raw)) return null;
  const name = text(raw.name, 160);
  if (!name) return null;
  const work = raw.work === undefined || raw.work === null ? null : parseWorking(raw.work);
  return {
    name,
    ok: raw.ok === true,
    expected: raw.expected,
    actual: raw.actual,
    severity: raw.severity === "advisory" ? "advisory" : "issue",
    basis: text(raw.basis, 160),
    ...(work ? { work } : {}),
  };
}

/** `review.checks_json` (JSON string or array) -> checks. */
export function parseChecks(value: unknown): Check[] {
  const raw = fromJson(value);
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, MAX_ITEMS).map(parseCheck).filter((c): c is Check => c !== null);
}

/* ------------------------------ formatting ------------------------------ */

const toNumber = (value: unknown): number | null => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
};
const grouped = (n: number, max = 2) => n.toLocaleString("en-US", { maximumFractionDigits: max });

/** Format a value for a unit: $1,234.56 · 47 min · 2.5 h · 1,160 mi · yes/no. */
export function formatValue(value: unknown, unit: WorkingUnit = "text"): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (unit === "bool") {
    if (value === "true" || value === 1) return "yes";
    if (value === "false" || value === 0) return "no";
  }
  const n = toNumber(value);
  if (n !== null && unit !== "text") {
    switch (unit) {
      case "usd":
        return `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      case "min":
        return `${grouped(n, 1)} min`;
      case "h":
        return `${grouped(n, 2)} h`;
      case "mi":
        return `${grouped(n, 1)} mi`;
      case "count":
        return grouped(n, 0);
      case "pct":
        return `${grouped(n, 1)}%`;
      default:
        return grouped(n, 4);
    }
  }
  if (typeof value === "number") return grouped(value, 4);
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.length ? value.slice(0, 12).map((v) => (typeof v === "string" && /^[a-z][a-z0-9_]*$/.test(v) ? inputLabel(v) : formatValue(v, unit))).join(", ") : "none";
  try {
    const s = JSON.stringify(value);
    return s.length > 80 ? `${s.slice(0, 80)}…` : s;
  } catch {
    return String(value);
  }
}

/**
 * Unit of one input. Workings carry a single unit for the result; inputs
 * follow it unless the key says otherwise (`free_time_min`, `tolerance_pct`,
 * `miles`, `*_amount`), and booleans/strings format as themselves.
 */
export function inputUnit(key: string, value: unknown, resultUnit: WorkingUnit): WorkingUnit {
  if (typeof value === "boolean") return "bool";
  if (toNumber(value) === null) return "text";
  const k = key.toLowerCase();
  if (/(^|_)(pct|percent|ratio|factor|multiplier|share|tolerance)$/.test(k)) return "ratio";
  if (/per_(mi|mile)$/.test(k)) return "usd";
  if (/(^|_)(min|mins|minutes)($|_)/.test(k)) return "min";
  if (/(^|_)(h|hr|hrs|hours)($|_)/.test(k)) return /rate/.test(k) ? "usd" : "h";
  if (/(^|_)(mi|miles)($|_)/.test(k)) return "mi";
  if (/(^|_)(count|n|days|pallets|pings)$/.test(k)) return "count";
  if (/(amount|usd|total|price|cost|fee|balance|delta|charge|paid|debit|credit|linehaul|fsc|rate|revenue|expense|profit|deposit|margin|limit|ceiling|hold|unsupported|collections)/.test(k)) return "usd";
  return ["usd", "min", "h", "mi", "count"].includes(resultUnit) ? resultUnit : "ratio";
}

/** One input formatted for display: a fractional `tolerance`/`*_pct` reads as a percent (0.02 -> 2%). */
export function formatInput(key: string, value: unknown, resultUnit: WorkingUnit): string {
  const unit = inputUnit(key, value, resultUnit);
  const n = toNumber(value);
  if (unit === "ratio" && n !== null && Math.abs(n) < 1 && /(^|_)(tolerance|pct|percent)$/.test(key.toLowerCase())) return `${grouped(n * 100, 2)}%`;
  return formatValue(value, unit);
}

export type FormulaPart = { kind: "value" | "ident" | "text"; text: string; key?: string };

const IDENT = /[A-Za-z_][A-Za-z0-9_.]*/g;

/** The formula with every identifier that names an input replaced by its formatted value. */
export function substituteFormula(w: Pick<Working, "formula" | "inputs" | "unit">): FormulaPart[] {
  const parts: FormulaPart[] = [];
  let last = 0;
  for (const match of w.formula.matchAll(IDENT)) {
    const name = match[0];
    const at = match.index ?? 0;
    if (at > last) parts.push({ kind: "text", text: w.formula.slice(last, at) });
    if (Object.hasOwn(w.inputs, name)) parts.push({ kind: "value", text: formatInput(name, w.inputs[name], w.unit), key: name });
    else parts.push({ kind: "ident", text: name });
    last = at + name.length;
  }
  if (last < w.formula.length) parts.push({ kind: "text", text: w.formula.slice(last) });
  return parts;
}

/* ---------------------------- recomputation ---------------------------- */

type Value = number | boolean;
type Token = { t: "num"; v: number } | { t: "id"; v: string } | { t: "op"; v: string };

const OP_ALIAS: Record<string, string> = { "×": "*", "÷": "/", "−": "-", "≤": "<=", "≥": ">=" };

function tokenize(src: string): Token[] | null {
  const out: Token[] = [];
  let i = 0;
  while (i < src.length) {
    const rest = src.slice(i);
    const space = /^\s+/.exec(rest);
    if (space) {
      i += space[0].length;
      continue;
    }
    const num = /^(\d[\d_]*(\.\d+)?|\.\d+)(e[+-]?\d+)?/i.exec(rest);
    if (num) {
      out.push({ t: "num", v: Number(num[0].replaceAll("_", "")) });
      i += num[0].length;
      continue;
    }
    const id = /^[A-Za-z_][A-Za-z0-9_.]*/.exec(rest);
    if (id) {
      out.push({ t: "id", v: id[0] });
      i += id[0].length;
      continue;
    }
    const op = /^(\*\*|<=|>=|==|!=|&&|\|\||[-+*/%()<>,!×÷−≤≥])/.exec(rest);
    if (!op) return null;
    out.push({ t: "op", v: OP_ALIAS[op[0]] ?? op[0] });
    i += op[0].length;
  }
  return out;
}

const FUNCS: Record<string, (args: number[]) => number> = {
  max: (a) => Math.max(...a),
  min: (a) => Math.min(...a),
  abs: (a) => Math.abs(a[0]),
  round: (a) => (a.length > 1 ? Math.round(a[0] * 10 ** a[1]) / 10 ** a[1] : Math.round(a[0])),
  ceil: (a) => Math.ceil(a[0]),
  floor: (a) => Math.floor(a[0]),
};

class Unsupported extends Error {}

/**
 * Evaluate a small arithmetic/comparison formula against `inputs`. Supports
 * + - * / % **, parentheses, comparisons, and/or/not, `a if cond else b`,
 * max/min/abs/round/ceil/floor.
 * Returns undefined for anything it does not understand (never throws, never evals).
 */
export function evaluateFormula(formula: string, inputs: Record<string, unknown>): Value | undefined {
  const parsed = tokenize(formula);
  if (!parsed || !parsed.length) return undefined;
  const tokens: Token[] = parsed;
  let pos = 0;
  const isOp = (v: string) => tokens[pos]?.t === "op" && tokens[pos].v === v;
  const isWord = (v: string) => tokens[pos]?.t === "id" && tokens[pos].v === v;
  const asNum = (v: Value): number => (typeof v === "boolean" ? (v ? 1 : 0) : v);
  const truthy = (v: Value) => (typeof v === "boolean" ? v : v !== 0);
  const expect = (v: string) => {
    if (!isOp(v)) throw new Unsupported();
    pos++;
  };

  function primary(): Value {
    const tok = tokens[pos++];
    if (!tok) throw new Unsupported();
    if (tok.t === "num") return tok.v;
    if (tok.t === "op") {
      if (tok.v === "(") {
        const v = ternary();
        expect(")");
        return v;
      }
      if (tok.v === "-") return -asNum(power());
      if (tok.v === "+") return asNum(power());
      if (tok.v === "!") return !truthy(primary());
      throw new Unsupported();
    }
    const name = tok.v;
    if (name === "true" || name === "True") return true;
    if (name === "false" || name === "False") return false;
    if (name === "not") return !truthy(compare());
    if (isOp("(")) {
      if (!Object.hasOwn(FUNCS, name)) throw new Unsupported();
      const fn = FUNCS[name];
      pos++;
      const args: number[] = [];
      while (!isOp(")")) {
        args.push(asNum(ternary()));
        if (isOp(",")) pos++;
        else break;
      }
      expect(")");
      if (!args.length) throw new Unsupported();
      return fn(args);
    }
    if (!Object.hasOwn(inputs, name)) throw new Unsupported();
    const raw = inputs[name];
    if (typeof raw === "boolean") return raw;
    const n = toNumber(raw);
    if (n === null) throw new Unsupported();
    return n;
  }
  function power(): Value {
    const base = primary();
    if (isOp("**")) {
      pos++;
      return asNum(base) ** asNum(power());
    }
    return base;
  }
  function term(): Value {
    let v = power();
    while (isOp("*") || isOp("/") || isOp("%")) {
      const op = tokens[pos++].v;
      const r = asNum(power());
      if ((op === "/" || op === "%") && r === 0) throw new Unsupported();
      v = op === "*" ? asNum(v) * r : op === "/" ? asNum(v) / r : asNum(v) % r;
    }
    return v;
  }
  function sum(): Value {
    let v = term();
    while (isOp("+") || isOp("-")) {
      const op = tokens[pos++].v;
      const r = asNum(term());
      v = op === "+" ? asNum(v) + r : asNum(v) - r;
    }
    return v;
  }
  function compare(): Value {
    const left = sum();
    const op = ["<=", ">=", "==", "!=", "<", ">"].find(isOp);
    if (!op) return left;
    pos++;
    const right = sum();
    if (op === "==" || op === "!=") {
      const same =
        typeof left === "boolean" || typeof right === "boolean" ? truthy(left) === truthy(right) : Math.abs(asNum(left) - asNum(right)) < 1e-9;
      return op === "==" ? same : !same;
    }
    const a = asNum(left);
    const b = asNum(right);
    return op === "<=" ? a <= b : op === ">=" ? a >= b : op === "<" ? a < b : a > b;
  }
  function and(): Value {
    let v = compare();
    while (isOp("&&") || isWord("and")) {
      pos++;
      const r = compare();
      v = truthy(v) && truthy(r);
    }
    return v;
  }
  function or(): Value {
    let v = and();
    while (isOp("||") || isWord("or")) {
      pos++;
      const r = and();
      v = truthy(v) || truthy(r);
    }
    return v;
  }
  // Python conditional expression, as the agents write it: `a if cond else b`.
  function ternary(): Value {
    const v = or();
    if (!isWord("if")) return v;
    pos++;
    const cond = or();
    if (!isWord("else")) throw new Unsupported();
    pos++;
    const otherwise = ternary();
    return truthy(cond) ? v : otherwise;
  }
  try {
    const v = ternary();
    if (pos !== tokens.length) return undefined;
    if (typeof v === "number" && !Number.isFinite(v)) return undefined;
    return v;
  } catch {
    return undefined;
  }
}

export type Recheck = { status: "match" | "mismatch" | "unchecked"; computed?: Value };

const asBool = (value: unknown): boolean | undefined =>
  typeof value === "boolean" ? value : value === "true" ? true : value === "false" ? false : undefined;

// Agents publish results rounded to display precision (cents, 0.01 h); a stated value that
// matches the recomputation once rounded the same way is a match, not an accusation.
const sameNumber = (stated: number, computed: number, unit: WorkingUnit) =>
  Math.abs(stated - computed) <=
  (unit === "usd" || unit === "h" || unit === "min" || unit === "mi" || unit === "ratio" || unit === "pct" ? 0.005 : Math.max(1e-6, Math.abs(stated) * 1e-6));

/**
 * Recompute the formula and compare with the stated result. Money compares to
 * the cent, other numbers to 1e-6 relative; anything unparseable is "unchecked".
 * Per-line `sum(...)` formulas over nested inputs are recomputed from the line table.
 */
export function recheckWorking(w: Pick<Working, "formula" | "inputs" | "result" | "unit">): Recheck {
  if (!w.formula) return { status: "unchecked" };
  const computed = evaluateFormula(w.formula, w.inputs);
  if (computed === undefined) return recheckTable(w);
  const stated = w.result;
  if (typeof computed === "boolean") {
    const s = asBool(stated);
    if (s === undefined) return { status: "unchecked" };
    return { status: s === computed ? "match" : "mismatch", computed };
  }
  const s = toNumber(stated);
  if (s === null) return { status: "unchecked" };
  return { status: sameNumber(s, computed, w.unit) ? "match" : "mismatch", computed };
}

/* -------------------------------- layout -------------------------------- */

/*
 * How a working reads on the page. Agents publish a formula string; the
 * dashboard lays the common shapes out the way a person would work them on
 * paper, and falls back to the substituted equation for everything else:
 *   tally       a - b + c: one line per input, operator in the margin, a rule, the result
 *   compare     a <= b: both sides with their labels, the verdict and the gap
 *   table       sum(...) over per-line inputs (receivables, journal lines): rows, columns, totals
 *   set         required - received documents: each one ticked or missing
 *   note        a single amount with a reason: `lumper (no receipt attached)`
 *   expression  anything else: the inputs listed, then the substituted equation
 */

export type Operand = { key: string; label: string; value: string; source?: string };
export type TallyLine = Operand & { op: "" | "+" | "−" };
export type Fact = { key: string; label: string; value: string; tone?: "yes" | "no" };
export type TableColumn = { key: string; label: string; unit: WorkingUnit };
export type TableRow = { key: string; label: string; cells: string[]; net?: string };
export type TableComparison = { left: Operand; op: string; right: Operand; holds: boolean };

export type WorkingLayout =
  | { kind: "tally"; lines: TallyLine[] }
  | { kind: "compare"; left: Operand; op: string; right: Operand; holds: boolean | undefined; gap?: string }
  | {
      kind: "table";
      columns: TableColumn[];
      rows: TableRow[];
      totals: string[];
      net?: { label: string; total: string };
      comparison?: TableComparison;
    }
  | { kind: "set"; items: { name: string; label: string; present: boolean }[]; title: string }
  | { kind: "note"; line: Operand; reason: string }
  | { kind: "expression"; inputs: Operand[]; parts: FormulaPart[] };

const LABELS: Record<string, string> = {
  invoice_amount: "Carrier invoice",
  contract_rate: "Contract rate",
  contract_amount: "Contract rate",
  tolerance: "Tolerance",
  ceiling: "Tolerance ceiling",
  tolerance_limit: "Tolerance ceiling",
  line_items_total: "Invoice lines",
  stated_total: "Stated total",
  paid_amount: "Already paid",
  shipper_invoice_amount: "Shipper invoice",
  matched_amount: "Cash matched",
  gps_dwell_min: "GPS dwell",
  free_time_min: "Free time",
  detention_hours_claimed: "Detention claimed",
  supportable_hours: "GPS supports",
  bank_change: "Bank change requested",
  unsupported: "Unsupported charges",
  revenue: "Revenue",
  revenue_30d: "Revenue, last 30 days",
  expense_30d: "Expenses, last 30 days",
  expense: "Expenses",
  gross_profit: "Gross profit",
  ar_balance: "AR balance",
  days: "Days",
  next_payment_amount: "Next payment",
  billed: "Billed to shipper",
  invoiced_carrier_cost: "Carrier cost as invoiced",
  contracted_carrier_cost: "Carrier cost at contract",
  expected_collections: "Expected collections",
  outstanding_carrier_cost: "Outstanding carrier cost",
  required_documents: "Required",
  received_documents: "Received",
  hold_amount: "Hold",
  delta: "Invoice delta",
};
const ACRONYMS = new Set(["gps", "fsc", "pod", "ap", "ar", "id", "rc", "ach", "bol", "eta"]);
const UNIT_TAILS = new Set(["min", "mins", "minutes", "h", "hr", "hrs", "usd"]);

/** `gps_dwell_min` -> "GPS dwell", `deposit_2` -> "Deposit 2"; the unit shows on the value instead. */
export function inputLabel(key: string): string {
  if (Object.hasOwn(LABELS, key)) return LABELS[key];
  const words = key.split(/[_\s]+/).filter(Boolean);
  if (words.length > 1 && UNIT_TAILS.has(words[words.length - 1].toLowerCase())) words.pop();
  const s = words.map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w.toLowerCase())).join(" ");
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : key;
}

// Which document answers for an input (mirrors common/workings.py DOCUMENT_KIND_FOR_INPUT),
// as the evidence-ref prefix that document carries.
const SOURCE_PREFIX: Record<string, string> = {
  contract_rate: "doc:RC-",
  contract_amount: "doc:RC-",
  linehaul: "doc:RC-",
  fsc: "doc:RC-",
  free_time_min: "doc:RC-",
  detention_rate_hr: "doc:RC-",
  invoice_amount: "doc:INV-",
  line_items_total: "doc:INV-",
  paid_amount: "doc:INV-",
  stated_total: "doc:INV-",
  detention_hours_claimed: "doc:INV-",
  shipper_invoice_amount: "doc:SINV-",
  gps_dwell_min: "doc:POD-",
  pod_signed: "doc:POD-",
};

/** The cited document an input was read from, when the working cites it. */
export function inputSource(key: string, refs: readonly string[]): string | undefined {
  const prefix = SOURCE_PREFIX[key];
  return prefix ? refs.find((r) => r.startsWith(prefix)) : undefined;
}

/** Formula operators as they are written by hand: `*` -> ×, `<=` -> ≤, `-` -> −. */
export function prettyOperators(s: string): string {
  return s
    .replaceAll("**", "^")
    .replaceAll("<=", "≤")
    .replaceAll(">=", "≥")
    .replaceAll("!=", "≠")
    .replaceAll("==", "=")
    .replaceAll("*", "×")
    .replaceAll("/", "÷")
    .replaceAll("-", "−");
}

const PRETTY_COMPARE: Record<string, string> = { "<=": "≤", ">=": "≥", "<": "<", ">": ">", "==": "=", "!=": "≠" };

const operand = (w: Pick<Working, "inputs" | "unit" | "evidence_refs">, key: string): Operand => {
  const value = w.inputs[key];
  const source = inputSource(key, w.evidence_refs);
  return { key, label: inputLabel(key), value: formatInput(key, value, w.unit), ...(source ? { source } : {}) };
};

/** A value of a result or nested input as label/value facts, so objects never render as JSON. */
export function resultFacts(value: unknown, unit: WorkingUnit): Fact[] | null {
  if (!isRecord(value)) return null;
  return Object.entries(value)
    .slice(0, 8)
    .map(([key, v]) => {
      const tone = typeof v === "boolean" ? (v ? "yes" : "no") : undefined;
      return { key, label: inputLabel(key), value: formatValue(v, inputUnit(key, v, unit)), ...(tone ? { tone } : {}) } as Fact;
    });
}

const setMembers = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((v) => typeof v === "string" || typeof v === "number").map(String) : [];

/** Lay a working out for reading. Pure; unrecognized shapes become an `expression`. */
export function layoutWorking(w: Pick<Working, "formula" | "inputs" | "result" | "unit" | "evidence_refs">): WorkingLayout {
  const f = w.formula.trim();
  const numeric = (key: string) => Object.hasOwn(w.inputs, key) && toNumber(w.inputs[key]) !== null;

  const table = lineTable(w);
  if (table) return table;

  // required_documents - received_documents, both lists.
  const set = /^([A-Za-z_]\w*)\s*-\s*([A-Za-z_]\w*)$/.exec(f);
  if (set && Array.isArray(w.inputs[set[1]]) && Array.isArray(w.inputs[set[2]])) {
    const have = new Set(setMembers(w.inputs[set[2]]));
    const names = [...new Set(setMembers(w.inputs[set[1]]))].slice(0, 20);
    return { kind: "set", title: inputLabel(set[1]), items: names.map((name) => ({ name, label: inputLabel(name), present: have.has(name) })) };
  }

  // lumper (no receipt attached)
  const note = /^([A-Za-z_]\w*)\s*\((.+)\)$/.exec(f);
  if (note && !Object.hasOwn(FUNCS, note[1]) && numeric(note[1])) return { kind: "note", line: operand(w, note[1]), reason: note[2].trim() };

  // a - b + c
  const tokens = tokenize(f);
  // A bare identifier (`detention`, `deposit_1`) is a one-line tally, not "$250 = $250".
  if (tokens && tokens.length % 2 === 1) {
    const isTerm = (t: Token) => t.t === "id" && numeric(t.v);
    const isSign = (t: Token) => t.t === "op" && (t.v === "+" || t.v === "-");
    if (tokens.every((t, i) => (i % 2 === 0 ? isTerm(t) : isSign(t))))
      return {
        kind: "tally",
        lines: tokens
          .filter((_, i) => i % 2 === 0)
          .map((t, n) => {
            const key = String(t.v);
            const line = operand(w, key);
            if (n === 0) return { ...line, op: "" as const };
            const amount = toNumber(w.inputs[key]) as number;
            const plus = (tokens[2 * n - 1].v === "+") === amount >= 0;
            return { ...line, value: formatInput(key, Math.abs(amount), w.unit), op: plus ? ("+" as const) : ("−" as const) };
          }),
      };
  }

  // a <= b
  if (tokens && tokens.length === 3 && tokens[0].t === "id" && tokens[2].t === "id" && tokens[1].t === "op" && Object.hasOwn(PRETTY_COMPARE, tokens[1].v)) {
    const [l, o, r] = [tokens[0].v, tokens[1].v, tokens[2].v];
    if (numeric(l) && numeric(r)) {
      const a = toNumber(w.inputs[l]) as number;
      const b = toNumber(w.inputs[r]) as number;
      const holds = evaluateFormula(f, w.inputs);
      const diff = Math.abs(a - b);
      const unit = inputUnit(l, w.inputs[l], w.unit);
      // Where the left side sits against the right; whether that is good depends on the rule.
      const word = a > b ? "over" : "under";
      return {
        kind: "compare",
        left: operand(w, l),
        op: PRETTY_COMPARE[o],
        right: operand(w, r),
        holds: typeof holds === "boolean" ? holds : undefined,
        ...(diff > 1e-9 ? { gap: `${formatValue(diff, unit)} ${word}` } : {}),
      };
    }
  }

  // A constant (`0` when nothing was unsupported) has nothing to show but its result.
  const parts = f && !/^-?[\d.]+$/.test(f) ? substituteFormula(w) : [];
  const used = parts.filter((p) => p.kind === "value").map((p) => p.key as string);
  return { kind: "expression", inputs: [...new Set(used)].map((k) => operand(w, k)), parts };
}

/* Per-line inputs: `{receivable_1: {amount, collected}, ...}` with `sum(receivable amount - collected)`,
 * or journal lines `{cash_1: {debit, credit}, ...}` with `sum(debits) == sum(credits)`. */

const singular = (s: string) => (s.endsWith("s") ? s.slice(0, -1) : s);

type LineModel = {
  rows: { key: string; fields: Record<string, unknown> }[];
  columns: string[];
  totals: number[];
  inner?: { expr: string; values: (number | undefined)[]; total: number | undefined };
  sides?: { left: string; op: string; right: string; holds: boolean };
};

function lineModel(w: Pick<Working, "formula" | "inputs">): LineModel | null {
  const entries = Object.entries(w.inputs);
  if (!entries.length || !entries.every(([, v]) => isRecord(v))) return null;
  const rows = entries.map(([key, v]) => ({ key, fields: v as Record<string, unknown> }));
  const columns: string[] = [];
  for (const r of rows)
    for (const [k, v] of Object.entries(r.fields)) if (toNumber(v) !== null && !columns.includes(k) && columns.length < 4) columns.push(k);
  if (!columns.length) return null;
  // Journal lines read debit before credit, whichever account came first.
  const [debit, credit] = [columns.indexOf("debit"), columns.indexOf("credit")];
  if (debit >= 0 && credit >= 0 && credit < debit) [columns[credit], columns[debit]] = ["debit", "credit"];
  const totals = columns.map((c) => rows.reduce((s, r) => s + (toNumber(r.fields[c]) ?? 0), 0));
  const model: LineModel = { rows, columns, totals };
  const f = w.formula.trim();
  const column = (name: string) => columns.find((c) => c === name || c === singular(name) || singular(c) === singular(name));

  const sides = /^sum\(\s*(\w+)\s*\)\s*(<=|>=|==|!=|<|>)\s*sum\(\s*(\w+)\s*\)$/.exec(f);
  if (sides) {
    const left = column(sides[1]);
    const right = column(sides[3]);
    if (left && right) {
      // Whole cents: float sums of balanced entries can differ in the last bits.
      const cents = (c: string) => Math.round(totals[columns.indexOf(c)] * 100);
      const holds = evaluateFormula(`a ${sides[2]} b`, { a: cents(left), b: cents(right) });
      if (typeof holds === "boolean") model.sides = { left, op: sides[2], right, holds };
    }
    return model;
  }
  const inner = /^sum\((.+)\)$/.exec(f);
  if (inner) {
    // "receivable amount - collected": a word naming the line, not a field, is dropped.
    const tokens = tokenize(inner[1]) ?? [];
    const kept = tokens.filter((t, i) => !(t.t === "id" && !columns.includes(t.v) && tokens[i + 1]?.t === "id"));
    const expr = kept.map((t) => (t.t === "num" ? String(t.v) : t.v)).join(" ");
    const values = rows.map((r) => {
      const v = evaluateFormula(expr, Object.fromEntries(columns.map((c) => [c, toNumber(r.fields[c]) ?? 0])));
      return typeof v === "number" ? v : undefined;
    });
    const total = values.every((v) => v !== undefined) ? values.reduce<number>((s, v) => s + (v as number), 0) : undefined;
    if (total !== undefined) model.inner = { expr, values, total };
  }
  return model;
}

function lineTable(w: Pick<Working, "formula" | "inputs" | "result" | "unit">): WorkingLayout | null {
  const m = lineModel(w);
  if (!m) return null;
  const units = m.columns.map((c) => inputUnit(c, m.rows.find((r) => r.fields[c] !== undefined)?.fields[c], w.unit));
  const columns = m.columns.map((key, i) => ({ key, label: inputLabel(key), unit: units[i] }));
  const netUnit: WorkingUnit = w.unit === "text" ? "usd" : w.unit;
  const layout: WorkingLayout = {
    kind: "table",
    columns,
    rows: m.rows.map((r, i) => ({
      key: r.key,
      label: inputLabel(r.key),
      cells: m.columns.map((c, j) => (r.fields[c] === undefined ? "—" : formatValue(r.fields[c], units[j]))),
      ...(m.inner ? { net: formatValue(m.inner.values[i], netUnit) } : {}),
    })),
    totals: m.totals.map((t, j) => formatValue(t, units[j])),
  };
  if (m.inner) layout.net = { label: prettyOperators(m.inner.expr.split(" ").map((t) => (m.columns.includes(t) ? inputLabel(t).toLowerCase() : t)).join(" ")), total: formatValue(m.inner.total, netUnit) };
  if (m.sides) {
    const side = (c: string) => ({ key: c, label: `${inputLabel(c)}s`, value: layout.totals[m.columns.indexOf(c)] });
    layout.comparison = { left: side(m.sides.left), op: PRETTY_COMPARE[m.sides.op], right: side(m.sides.right), holds: m.sides.holds };
  }
  return layout;
}

function recheckTable(w: Pick<Working, "formula" | "inputs" | "result" | "unit">): Recheck {
  const m = lineModel(w);
  if (m?.inner?.total !== undefined) {
    const s = toNumber(w.result);
    if (s === null) return { status: "unchecked" };
    return { status: sameNumber(s, m.inner.total, w.unit === "text" ? "usd" : w.unit) ? "match" : "mismatch", computed: m.inner.total };
  }
  if (m?.sides) {
    // The Controller states {debits, credits, balanced}; a bare boolean is accepted too.
    const stated = w.result;
    const flag = asBool(stated) ?? (isRecord(stated) ? Object.values(stated).map(asBool).find((v) => v !== undefined) : undefined);
    if (flag === undefined) return { status: "unchecked" };
    const statedTotals = isRecord(stated)
      ? [m.sides.left, m.sides.right].every((c) => {
          const v = toNumber(stated[`${c}s`] ?? stated[c]);
          return v === null || sameNumber(v, m.totals[m.columns.indexOf(c)], "usd");
        })
      : true;
    if (flag !== m.sides.holds) return { status: "mismatch", computed: m.sides.holds };
    // The verdict agrees but a stated total does not: no single recomputed value to show.
    return statedTotals ? { status: "match", computed: m.sides.holds } : { status: "mismatch" };
  }
  // required_documents - received_documents: the stated missing list is the set difference.
  const set = /^([A-Za-z_]\w*)\s*-\s*([A-Za-z_]\w*)$/.exec(w.formula.trim());
  if (set && Array.isArray(w.inputs[set[1]]) && Array.isArray(w.inputs[set[2]]) && Array.isArray(w.result)) {
    const have = new Set(setMembers(w.inputs[set[2]]));
    const missing = new Set(setMembers(w.inputs[set[1]]).filter((d) => !have.has(d)));
    const stated = new Set(setMembers(w.result));
    const same = missing.size === stated.size && [...missing].every((d) => stated.has(d));
    return { status: same ? "match" : "mismatch" };
  }
  return { status: "unchecked" };
}

/** `hold_matches_delta` -> "Hold matches delta". */
export function readableName(name: string): string {
  const s = name.replaceAll("_", " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** Policy ids (P-001) render like the existing policy chips; other bases are principles. */
export const isPolicyId = (basis: string) => /^P-\d{2,4}$/i.test(basis.trim());
