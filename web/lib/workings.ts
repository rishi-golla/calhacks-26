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

export const WORKING_UNITS = ["usd", "min", "h", "mi", "ratio", "bool", "count", "text"] as const;
export type WorkingUnit = (typeof WORKING_UNITS)[number];

export type Working = {
  label: string;
  formula: string;
  inputs: Record<string, unknown>;
  result: unknown;
  unit: WorkingUnit;
  evidence_refs: string[];
  basis: string;
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
  return { label: label || formula, formula, inputs, result: raw.result, unit, evidence_refs: refs, basis: text(raw.basis, 160) };
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
      default:
        return grouped(n, 4);
    }
  }
  if (typeof value === "number") return grouped(value, 4);
  if (typeof value === "string") return value;
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
  if (/(amount|usd|total|price|cost|fee|balance|delta|charge|paid|debit|credit|linehaul|fsc|rate|revenue|margin|limit|ceiling|hold|unsupported|collections)/.test(k)) return "usd";
  return ["usd", "min", "h", "mi", "count"].includes(resultUnit) ? resultUnit : "ratio";
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
    if (Object.hasOwn(w.inputs, name)) parts.push({ kind: "value", text: formatValue(w.inputs[name], inputUnit(name, w.inputs[name], w.unit)), key: name });
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

/**
 * Recompute the formula and compare with the stated result. Money compares to
 * the cent, other numbers to 1e-6 relative; anything unparseable is "unchecked".
 */
export function recheckWorking(w: Pick<Working, "formula" | "inputs" | "result" | "unit">): Recheck {
  if (!w.formula) return { status: "unchecked" };
  const computed = evaluateFormula(w.formula, w.inputs);
  if (computed === undefined) return { status: "unchecked" };
  const stated = w.result;
  if (typeof computed === "boolean") {
    const s = typeof stated === "boolean" ? stated : stated === "true" ? true : stated === "false" ? false : undefined;
    if (s === undefined) return { status: "unchecked" };
    return { status: s === computed ? "match" : "mismatch", computed };
  }
  const s = toNumber(stated);
  if (s === null) return { status: "unchecked" };
  const tol = w.unit === "usd" ? 0.005 : Math.max(1e-6, Math.abs(s) * 1e-6);
  return { status: Math.abs(s - computed) <= tol ? "match" : "mismatch", computed };
}

/** `hold_matches_delta` -> "Hold matches delta". */
export function readableName(name: string): string {
  const s = name.replaceAll("_", " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** Policy ids (P-001) render like the existing policy chips; other bases are principles. */
export const isPolicyId = (basis: string) => /^P-\d{2,4}$/i.test(basis.trim());
