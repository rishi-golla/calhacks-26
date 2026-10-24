/** Public table names are also the fixed allowlist for the read-only fallback. */
export const TABLES = [
  "case",
  "task",
  "agent",
  "agent_config",
  "ledger_entry",
  "hold",
  "payment_proposal",
  "approval",
  "audit_event",
  "call",
  "precedent",
  "llm_call",
  "metric",
  "change_set",
  "cell_change",
  "doc_read",
  "review",
  "agent_memory",
  "instruction_version",
  "flag",
  "company",
  "contact",
  "delegation_edge",
  "invoice",
  "cash_forecast",
  "principal",
  "routing_outbox",
  "memory_revision",
  "accounting_period",
] as const;
export type TableName = (typeof TABLES)[number];
export type Row = Record<string, unknown>;
export type Snapshot = Record<TableName, Row[]>;
export const emptySnapshot = (): Snapshot =>
  Object.fromEntries(TABLES.map((name) => [name, [] as Row[]])) as Snapshot;
export const camel = (name: string) =>
  name.replace(/_([a-z])/g, (_, char: string) => char.toUpperCase());
export function normalize(value: unknown): unknown {
  if (typeof value === "bigint")
    return Number.isSafeInteger(Number(value))
      ? Number(value)
      : value.toString();
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === "object") {
    if ("toHexString" in value && typeof value.toHexString === "function")
      return value.toHexString();
    if ("toDate" in value && typeof value.toDate === "function")
      return value.toDate().toISOString();
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`),
        normalize(item),
      ]),
    );
  }
  return value;
}
export const str = (value: unknown, fallback = "—"): string =>
  value === undefined || value === null || value === ""
    ? fallback
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
export const num = (value: unknown): number =>
  Number.isFinite(Number(value)) ? Number(value) : 0;
export const json = <T = unknown>(value: unknown, fallback: T): T => {
  if (typeof value !== "string") return (value ?? fallback) as T;
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
};
export const money = (value: unknown) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(num(value));
export function timestamp(value: unknown): number {
  if (typeof value === "number")
    return value > 1e14 ? value / 1000 : value > 1e11 ? value : value * 1000;
  if (typeof value === "string" && /^\d+$/.test(value))
    return timestamp(Number(value));
  return Date.parse(str(value, "")) || 0;
}
export const date = (value: unknown) => {
  const ms = timestamp(value);
  return ms
    ? new Date(ms).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "Not recorded";
};
export const newest = (rows: Row[], field = "ts") =>
  [...rows].sort(
    (a, b) =>
      timestamp(b[field]) - timestamp(a[field]) ||
      num(b.sequence) - num(a.sequence),
  );
export function safeUrl(value: unknown): string | undefined {
  try {
    const url = new URL(str(value));
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
export const AGENTS = [
  {
    id: "carrier_ap",
    name: "Carrier Settlement",
    short: "CS",
    role: "Carrier invoices & payment readiness",
    color: "blue",
  },
  {
    id: "shipper_ar",
    name: "Shipper Billing & Collections",
    short: "AR",
    role: "Billing packets, short-pay & collections",
    color: "blue",
  },
  {
    id: "risk",
    name: "Accessorial Audit",
    short: "AA",
    role: "Detention, receipts & extra charges",
    color: "amber",
  },
  {
    id: "verification",
    name: "Carrier Trust & Verification",
    short: "CV",
    role: "Carrier identity & payment verification",
    color: "violet",
  },
  {
    id: "controller",
    name: "Settlement Approvals",
    short: "CT",
    role: "Ledger & deterministic controls",
    color: "teal",
  },
  {
    id: "voice_desk",
    name: "Dispatcher Outreach",
    short: "VD",
    role: "Demo-routed conversations",
    color: "violet",
  },
  {
    id: "cfo",
    name: "Load Profitability",
    short: "LP",
    role: "Load margin & cash impact",
    color: "teal",
  },
  {
    id: "validator",
    name: "Settlement Quality Review",
    short: "VA",
    role: "Review & continuous learning",
    color: "amber",
  },
  {
    id: "documents",
    name: "POD & Document Chase",
    short: "DC",
    role: "Delivery paperwork & missing receipts",
    color: "violet",
  },
] as const;
export const agentName = (id: unknown) =>
  AGENTS.find((agent) => agent.id === id)?.name ?? str(id);
export type HumanReducer =
  | "decide_approval"
  | "raise_flag"
  | "update_case"
  | "set_agent_model"
  | "revert_instruction_version"
  | "upsert_memory";
export function exportCsv(name: string, rows: Row[]) {
  if (!rows.length) return;
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const escape = (value: unknown) => {
    const text = str(value, "");
    return `"${(/^[=+@\-\t\r]/.test(text) ? "'" : "") + text.replaceAll('"', '""')}"`;
  };
  const csv = [
    keys.map(escape).join(","),
    ...rows.map((row) => keys.map((key) => escape(row[key])).join(",")),
  ].join("\r\n");
  const url = URL.createObjectURL(
    new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}
