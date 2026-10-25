/**
 * Shown work for figures the dashboard derives itself: case exposure, the
 * case file's short pay, rate delta and detention chips, and the finance DSO
 * and margin tiles. Each builder returns Working rows in the agents' shape so
 * <Workings> lays them out (tally, comparison, inputs then equation). The
 * stated result is always the figure the page shows. Figures the dashboard
 * computes itself are marked `origin: "dashboard"` (their recheck repeats the
 * same arithmetic, so it proves nothing); exposure was computed by ingest, so
 * its recheck is a real comparison. Pure: no React.
 */
import type { Working } from "./workings";

type Row = Record<string, unknown>;

const num = (value: unknown): number | null => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
};
const isRecord = (value: unknown): value is Row => !!value && typeof value === "object" && !Array.isArray(value);
/** Python truthiness, for replaying ingest's `x or y` and `if x:`. */
const truthy = (value: unknown) =>
  !(value === undefined || value === null || value === false || value === 0 || value === "" || (Array.isArray(value) && !value.length) || (isRecord(value) && !Object.keys(value).length));

type Draft = Omit<Working, "evidence_refs" | "basis"> & Partial<Pick<Working, "evidence_refs" | "basis">>;
const work = (w: Draft): Working => ({ evidence_refs: [], basis: "", ...w });
/** A figure the dashboard computed itself, from these inputs. */
const derived = (w: Draft): Working => work({ ...w, origin: "dashboard" });

export type CaseDocument = { ref: string; kind: string };

/** Refs of the case documents of these kinds: a working cites what it reads, not the whole file. */
const cite = (documents: readonly CaseDocument[], ...kinds: string[]) => documents.filter((d) => kinds.includes(d.kind) && d.ref).map((d) => d.ref);

/** `a - b`, or `max(a - b, 0)` when the difference would go below zero. */
function floored(label: string, a: [string, number], b: [string, number], result: number, refs: string[], basis: string): Working {
  const inputs = { [a[0]]: a[1], [b[0]]: b[1] };
  const formula = a[1] - b[1] >= 0 ? `${a[0]} - ${b[0]}` : `max(${a[0]} - ${b[0]}, 0)`;
  return work({ label, formula, inputs, result, unit: "usd", evidence_refs: refs, basis });
}

/* ------------------------------- exposure ------------------------------- */

/**
 * Ingest's exposure derivation (ingest/mirror.py `_kind` and `exposure`),
 * replayed on the case's source record. `stated` is the case row's
 * exposure_usd, set when the case opened; the record keeps syncing after
 * that, so a later deposit or resubmitted invoice can make them differ, and
 * the recheck then says so.
 */
const AT_OPEN = "set when the case opened; checked against the current record";

export function exposureWorkings(caseId: string, record: Row, stated: number, documents: readonly CaseDocument[]): Working[] {
  if (truthy(record.bank_change_requested) || caseId.endsWith("-BANK")) {
    // `invoice_amount or bank_change.next_payment_amount`
    const next = isRecord(record.bank_change) ? record.bank_change.next_payment_amount : undefined;
    const [key, raw] = truthy(record.invoice_amount) ? ["invoice_amount", record.invoice_amount] : ["next_payment_amount", next];
    const value = num(raw);
    if (value === null) return [];
    return [
      work({
        label: "Payment a bank change would redirect",
        formula: key,
        inputs: { [key]: value },
        result: stated,
        unit: "usd",
        evidence_refs: cite(documents, "carrier_invoice"),
        basis: AT_OPEN,
      }),
    ];
  }
  if (caseId.endsWith("-AR")) {
    const billed = num(record.shipper_invoice_amount);
    if (billed === null) return [];
    const deposits = (Array.isArray(record.deposits) ? record.deposits : []).filter(isRecord);
    const inputs: Row = { shipper_invoice_amount: billed };
    deposits.forEach((d, i) => (inputs[`deposit_${i + 1}`] = num(d.amount) ?? 0));
    const keys = Object.keys(inputs);
    const short = (inputs.shipper_invoice_amount as number) - keys.slice(1).reduce((s, k) => s + (inputs[k] as number), 0);
    const formula = keys.join(" - ");
    return [
      work({
        label: "Unpaid by the shipper, all deposits",
        formula: short >= 0 ? formula : `max(${formula}, 0)`,
        inputs,
        result: stated,
        unit: "usd",
        evidence_refs: cite(documents, "shipper_invoice"),
        basis: AT_OPEN,
      }),
    ];
  }
  // A case without an invoice is a delivery, with no exposure.
  const invoice = num(record.invoice_amount);
  const contract = num(record.contract_rate);
  if (!truthy(record.invoice_id) || invoice === null || contract === null) return [];
  return [floored("Billed over contract", ["invoice_amount", invoice], ["contract_rate", contract], stated, cite(documents, "carrier_invoice", "rate_confirmation"), AT_OPEN)];
}

/* ------------------------------ case file ------------------------------ */

/** The figures a case file carries (components/cases/case-file-model `CaseFile` satisfies this). */
export type CaseFileFigures = {
  billed: number | null;
  /** Deposits applied to this case's invoice from this case's shipper. */
  applied: { id: string; amount: number | null }[];
  rate: { contract: number | null; invoice: number | null; delta: number | null } | null;
  detention: { claimedHours: number | null; dwellMin: number | null; freeTimeMin: number | null; billableHours: number | null; overClaimed: boolean } | null;
  documents: CaseDocument[];
};

/** Billed minus each applied deposit = short. */
export function shortPayWorkings(f: CaseFileFigures, short: number): Working[] {
  if (f.billed === null) return [];
  const inputs: Row = { shipper_invoice_amount: f.billed };
  f.applied.forEach((d, i) => (inputs[`deposit_${i + 1}`] = d.amount ?? 0));
  const ids = f.applied.map((d) => d.id).filter(Boolean);
  return [
    derived({
      label: ids.length ? `Short pay (deposits ${ids.join(", ")})` : "Short pay",
      formula: Object.keys(inputs).join(" - "),
      inputs,
      result: short,
      unit: "usd",
      evidence_refs: cite(f.documents, "shipper_invoice"),
      basis: "cash applied to this invoice from this shipper",
    }),
  ];
}

/** Invoice minus contract = the delta chip. */
export function rateDeltaWorkings(f: CaseFileFigures): Working[] {
  const r = f.rate;
  if (!r || r.invoice === null || r.contract === null || r.delta === null) return [];
  return [
    derived({
      label: "Invoice vs rate confirmation",
      formula: "invoice_amount - contract_rate",
      inputs: { invoice_amount: r.invoice, contract_rate: r.contract },
      result: r.delta,
      unit: "usd",
      evidence_refs: cite(f.documents, "carrier_invoice", "rate_confirmation"),
      basis: "three-way match",
    }),
  ];
}

/** Hours the GPS supports, then claimed against supported. */
export function detentionWorkings(f: CaseFileFigures): Working[] {
  const d = f.detention;
  if (!d || d.dwellMin === null || d.freeTimeMin === null || d.billableHours === null) return [];
  const out = [
    derived({
      label: "Detention hours the GPS supports",
      formula: "max(gps_dwell_min - free_time_min, 0) / 60",
      inputs: { gps_dwell_min: d.dwellMin, free_time_min: d.freeTimeMin },
      result: d.billableHours,
      unit: "h",
      evidence_refs: cite(f.documents, "pod", "rate_confirmation"),
      basis: "detention accrues after free time",
    }),
  ];
  if (d.claimedHours !== null)
    out.push(
      derived({
        label: "Claimed hours within support",
        formula: "detention_hours_claimed <= supportable_hours",
        inputs: { detention_hours_claimed: d.claimedHours, supportable_hours: d.billableHours },
        result: !d.overClaimed,
        unit: "bool",
        evidence_refs: cite(f.documents, "carrier_invoice", "pod", "rate_confirmation"),
        basis: "a detention claim needs GPS dwell beyond free time",
      }),
    );
  return out;
}

/* -------------------------------- finance ------------------------------- */

type FinanceFigures = { ar: number; revenue30d: number; expense30d: number; grossMarginPct: number };

/** Days of sales outstanding: AR over a day's revenue, on the tile's 30-day base. */
export function dsoWorkings(f: FinanceFigures): Working[] {
  if (!(f.revenue30d > 0)) return [];
  return [
    derived({
      label: "Days sales outstanding",
      formula: "ar_balance / (revenue_30d / days)",
      inputs: { ar_balance: f.ar, revenue_30d: f.revenue30d, days: 30 },
      result: f.ar / (f.revenue30d / 30),
      unit: "count",
      basis: "ledger balances; revenue posted in the last 30 days",
    }),
  ];
}

/** Gross profit as a tally, then the margin percentage, over the tile's window ("last 30 days", "since 9:41"). */
export function marginWorkings(f: FinanceFigures, window = "last 30 days"): Working[] {
  if (!(f.revenue30d > 0)) return [];
  const profit = f.revenue30d - f.expense30d;
  return [
    derived({
      label: "Gross profit",
      formula: "revenue - expense",
      inputs: { revenue: f.revenue30d, expense: f.expense30d },
      result: profit,
      unit: "usd",
      basis: `revenue and expense posted to the ledger, ${window}`,
    }),
    derived({
      label: "Gross margin",
      formula: "gross_profit / revenue * 100",
      inputs: { gross_profit: profit, revenue: f.revenue30d },
      result: f.grossMarginPct,
      unit: "pct",
      basis: "gross profit over revenue",
    }),
  ];
}
