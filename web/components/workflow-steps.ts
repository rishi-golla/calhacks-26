"use client";
import { money, str, timestamp, type Row, type Snapshot } from "@/lib/data";

export type Line = { key: string; ts: number; text: string; meta?: string };
export type Step = {
  id: string;
  title: string;
  subtitle: string;
  operator: string;
  agent?: string;
  sources: string[];
  feed: (d: Snapshot) => Line[];
};

export const rows = (d: Snapshot, table: string): Row[] =>
  (d as Record<string, Row[]>)[table] ?? [];
const line = (table: string, r: Row, ts: unknown, text: string): Line => ({
  key: `${table}:${str(r.id ?? r.case_id ?? r.name ?? text)}`,
  ts: timestamp(ts),
  text,
  meta: r.case_id ? str(r.case_id) : undefined,
});
const byAgent = (agent: string) => (d: Snapshot) => [
  ...rows(d, "audit_event")
    .filter((r) => r.agent === agent)
    .map((r) => line("audit", r, r.ts, str(r.summary || r.kind))),
  ...rows(d, "task")
    .filter((r) => r.agent === agent)
    .map((r) =>
      line(
        "task",
        { ...r, id: r.task_id },
        r.updated_at || r.started_at,
        `${str(r.skill)} · ${str(r.business_status || r.a2a_state)}`,
      ),
    ),
];

export const STEPS: Step[] = [
  {
    id: "generator",
    title: "Synthetic generator",
    subtitle: "Loads, invoices, PODs, bank lines",
    operator: "Simulator",
    sources: ["case", "invoice"],
    feed: (d) => [
      ...rows(d, "case").map((r) =>
        line(
          "case",
          r,
          r.opened_at,
          `Opened ${str(r.case_id)} · ${str(r.title)}`,
        ),
      ),
      ...rows(d, "invoice").map((r) =>
        line(
          "invoice",
          r,
          r.issued_at,
          `${str(r.id)} · ${money(r.amount)} · ${str(r.status)}`,
        ),
      ),
    ],
  },
  {
    id: "ingest",
    title: "Ingestion",
    subtitle: "Raw documents to routable events",
    operator: "Ingest agent",
    agent: "ingest",
    sources: ["doc_read", "audit_event"],
    feed: (d) => [
      ...rows(d, "doc_read").map((r) =>
        line(
          "doc",
          r,
          0,
          `${str(r.doc_ref)} · p${str(r.page)} · ${str(r.span)}`,
        ),
      ),
      ...byAgent("ingest")(d),
    ],
  },
  {
    id: "asus",
    title: "ASUS secure env",
    subtitle: "Redaction, local LLM, routing",
    operator: "ASUS GX10 router",
    sources: ["routing_outbox", "delegation_edge"],
    feed: (d) => [
      ...rows(d, "routing_outbox").map((r) =>
        line(
          "route",
          r,
          r.created_at,
          `${str(r.kind)} → ${str(r.target_agent)} · ${str(r.status)}`,
        ),
      ),
      ...rows(d, "delegation_edge").map((r) =>
        line(
          "edge",
          r,
          r.ts,
          `${str(r.from_agent)} → ${str(r.to_agent)} · ${str(r.skill)}`,
        ),
      ),
    ],
  },
  {
    id: "carrier_ap",
    title: "Carrier AP",
    subtitle: "Three-way match: rate con, POD, invoice",
    operator: "Carrier AP agent",
    agent: "carrier_ap",
    sources: ["audit_event", "task"],
    feed: byAgent("carrier_ap"),
  },
  {
    id: "shipper_ar",
    title: "Shipper AR",
    subtitle: "Payments, short-pay, collections",
    operator: "Shipper AR agent",
    agent: "shipper_ar",
    sources: ["audit_event", "task"],
    feed: byAgent("shipper_ar"),
  },
  {
    id: "risk",
    title: "Risk",
    subtitle: "Bank-change and fraud screening",
    operator: "Risk agent",
    agent: "risk",
    sources: ["audit_event", "task"],
    feed: byAgent("risk"),
  },
  {
    id: "contacts",
    title: "Live data · contacts",
    subtitle: "FMCSA carriers, SEC shippers",
    operator: "Verification agent",
    sources: ["company", "contact"],
    feed: (d) => [
      ...rows(d, "company").map((r) =>
        line(
          "company",
          r,
          0,
          `${str(r.name)} · USDOT ${str(r.usdot)} · ${str(r.phone)}`,
        ),
      ),
      ...rows(d, "contact").map((r) =>
        line(
          "contact",
          r,
          0,
          `${str(r.name)} · ${str(r.title)} · ${str(r.phone)} · ${str(r.email)}`,
        ),
      ),
    ],
  },
  {
    id: "verification",
    title: "Verification",
    subtitle: "Confirms identity against public records",
    operator: "Verification agent",
    agent: "verification",
    sources: ["audit_event", "task"],
    feed: byAgent("verification"),
  },
  {
    id: "voice_desk",
    title: "Voice Desk",
    subtitle: "Demo-routed calls and email",
    operator: "Voice Desk agent",
    agent: "voice_desk",
    sources: ["call", "audit_event"],
    feed: (d) => [
      ...rows(d, "call").map((r) =>
        line(
          "call",
          r,
          r.ts ?? r.started_at,
          `${str(r.id)} · ${str(r.status)}`,
        ),
      ),
      ...byAgent("voice_desk")(d),
    ],
  },
  {
    id: "controller",
    title: "Controller",
    subtitle: "Deterministic pass: holds, ledger",
    operator: "Controller agent",
    agent: "controller",
    sources: ["hold", "ledger_entry", "audit_event"],
    feed: (d) => [
      ...rows(d, "hold").map((r) =>
        line(
          "hold",
          r,
          r.placed_at,
          `${str(r.id)} · ${money(r.amount)} · ${str(r.status)}`,
        ),
      ),
      ...rows(d, "ledger_entry").map((r) =>
        line("ledger", r, r.posted_at, `${str(r.id)} · ${str(r.memo)}`),
      ),
      ...byAgent("controller")(d),
    ],
  },
  {
    id: "cfo",
    title: "CFO",
    subtitle: "Cash forecast and capital",
    operator: "CFO agent",
    agent: "cfo",
    sources: ["cash_forecast", "audit_event"],
    feed: (d) => [
      ...rows(d, "cash_forecast").map((r) =>
        line(
          "forecast",
          r,
          r.updated_at,
          `${str(r.period)} · ${str(r.category)} · ${money(r.amount)}`,
        ),
      ),
      ...byAgent("cfo")(d),
    ],
  },
  {
    id: "payments",
    title: "Payments · Nessie",
    subtitle: "Capital One sandbox payouts",
    operator: "Carrier AP agent",
    sources: ["payment_proposal", "invoice"],
    feed: (d) => [
      ...rows(d, "payment_proposal").map((r) =>
        line(
          "payment",
          r,
          r.proposed_at,
          `${str(r.id)} · ${money(r.amount)} · ${str(r.status)}`,
        ),
      ),
      ...rows(d, "invoice")
        .filter((r) => Number(r.paid_amount) > 0)
        .map((r) =>
          line(
            "paid",
            r,
            r.issued_at,
            `${str(r.id)} paid ${money(r.paid_amount)}`,
          ),
        ),
    ],
  },
  {
    id: "audit",
    title: "Audit trail",
    subtitle: "Git-style change sets per result",
    operator: "Audit writer",
    sources: ["change_set"],
    feed: (d) =>
      rows(d, "change_set").map((r) =>
        line(
          "cs",
          r,
          r.ts,
          `${str(r.hash).slice(0, 7)} ${str(r.agent)}: ${str(r.summary)}`,
        ),
      ),
  },
  {
    id: "human",
    title: "Human input",
    subtitle: "Approvals and flags",
    operator: "Human reviewer",
    sources: ["approval", "flag"],
    feed: (d) => [
      ...rows(d, "approval").map((r) =>
        line(
          "approval",
          r,
          r.decided_at || r.expires_at,
          `${str(r.action)} · ${money(r.amount)} · ${str(r.decision || "pending")}`,
        ),
      ),
      ...rows(d, "flag").map((r) =>
        line("flag", r, r.ts, `${str(r.kind)}: ${str(r.note)}`),
      ),
    ],
  },
  {
    id: "validator",
    title: "Validator",
    subtitle: "Reviews every agent output",
    operator: "Validator agent",
    agent: "validator",
    sources: ["review", "audit_event"],
    feed: (d) => [
      ...rows(d, "review").map((r) =>
        line("review", r, r.ts, `${str(r.target_task_id)} · ${str(r.verdict)}`),
      ),
      ...byAgent("validator")(d),
    ],
  },
  {
    id: "instructions",
    title: "Instructions & memory",
    subtitle: "What the agents learn",
    operator: "Validator agent",
    sources: ["instruction_version", "agent_memory"],
    feed: (d) => [
      ...rows(d, "instruction_version").map((r) =>
        line(
          "instr",
          r,
          r.created_at,
          `${str(r.agent)} v${str(r.version)} · ${str(r.reason)}`,
        ),
      ),
      ...rows(d, "agent_memory").map((r) =>
        line("memory", r, r.created_at, `${str(r.agent)}: ${str(r.text)}`),
      ),
    ],
  },
];

export const stepById = (id: string) => STEPS.find((s) => s.id === id);
export const feedOf = (step: Step, d: Snapshot) =>
  [...step.feed(d)].sort((a, b) => b.ts - a.ts);
export const clock = (ts: number) =>
  ts
    ? new Date(ts).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "";

export function scope(d: Snapshot, caseId: string): Snapshot {
  if (!caseId) return d;
  const taskCase = new Map(
    rows(d, "task").map((r) => [str(r.task_id), str(r.case_id)]),
  );
  const out: Record<string, Row[]> = {};
  for (const [k, v] of Object.entries(d))
    out[k] = (v as Row[]).filter((r) => {
      if (r.case_id) return r.case_id === caseId;
      if (r.task_id) return taskCase.get(str(r.task_id)) === caseId;
      return !("case_id" in r);
    });
  return out as Snapshot;
}

export function latestCase(d: Snapshot): string {
  let best = "";
  let bestTs = -1;
  const see = (caseId: unknown, ts: unknown) => {
    const t = timestamp(ts);
    if (caseId && t >= bestTs) {
      best = str(caseId);
      bestTs = t;
    }
  };
  for (const r of rows(d, "case")) see(r.case_id, r.opened_at);
  for (const r of rows(d, "task")) see(r.case_id, r.updated_at || r.started_at);
  for (const r of rows(d, "delegation_edge")) see(r.case_id, r.ts);
  for (const r of rows(d, "audit_event")) see(r.case_id, r.ts);
  return best;
}

const WORK = ["carrier_ap", "shipper_ar", "risk", "verification", "voice_desk"];
const AGENT_STEPS = [...WORK, "controller", "cfo", "validator"];

export function activeSteps(d: Snapshot): Set<string> {
  const s = new Set(["generator", "ingest", "asus"]);
  const used = new Set<string>();
  for (const r of rows(d, "task")) used.add(str(r.agent));
  for (const r of rows(d, "audit_event")) used.add(str(r.agent));
  for (const r of rows(d, "delegation_edge")) {
    used.add(str(r.from_agent));
    used.add(str(r.to_agent));
  }
  for (const id of AGENT_STEPS) if (used.has(id)) s.add(id);
  const any = (t: string) => rows(d, t).length > 0;
  if (any("hold") || any("ledger_entry")) s.add("controller");
  if (any("cash_forecast")) s.add("cfo");
  if (
    any("payment_proposal") ||
    rows(d, "invoice").some((r) => Number(r.paid_amount) > 0)
  )
    s.add("payments");
  if (s.has("verification")) s.add("contacts");
  if (any("change_set")) s.add("audit");
  if (any("approval") || any("flag")) s.add("human");
  if (
    any("review") ||
    rows(d, "routing_outbox").some((r) => r.target_agent === "validator")
  )
    s.add("validator");
  if (s.has("validator")) s.add("instructions");
  return s;
}

export type Link = { from: string; to: string };

export function linksFor(visible: Set<string>, d: Snapshot): Link[] {
  const out = new Map<string, Link>();
  const add = (from: string, to: string) => {
    if (from !== to && visible.has(from) && visible.has(to))
      out.set(`${from}-${to}`, { from, to });
  };
  add("generator", "ingest");
  add("ingest", "asus");
  for (const r of rows(d, "delegation_edge"))
    add(str(r.from_agent), str(r.to_agent));
  const work = WORK.filter((id) => visible.has(id));
  const has = (pred: (l: Link) => boolean) => [...out.values()].some(pred);
  for (const id of work)
    if (!has((l) => l.to === id && WORK.includes(l.from))) add("asus", id);
  if (!work.length)
    add("asus", visible.has("controller") ? "controller" : "audit");
  add("contacts", "verification");
  for (const id of work)
    if (!has((l) => l.from === id))
      add(id, visible.has("controller") ? "controller" : "audit");
  add("controller", "cfo");
  add(visible.has("cfo") ? "cfo" : "controller", "payments");
  add("controller", "audit");
  add("audit", "human");
  add(visible.has("human") ? "human" : "audit", "validator");
  add("validator", "instructions");
  add("instructions", "asus");
  return [...out.values()];
}

/** Column per node: breadth-first distance from the generator. */
export function layout(visible: Set<string>, links: Link[]) {
  const level = new Map<string, number>([["generator", 0]]);
  const queue = ["generator"];
  while (queue.length) {
    const id = queue.shift()!;
    for (const l of links)
      if (l.from === id && !level.has(l.to)) {
        level.set(l.to, level.get(id)! + 1);
        queue.push(l.to);
      }
  }
  const max = Math.max(0, ...level.values());
  const columns = new Map<number, string[]>();
  for (const step of STEPS)
    if (visible.has(step.id)) {
      const c = level.get(step.id) ?? max + 1;
      columns.set(c, [...(columns.get(c) ?? []), step.id]);
    }
  const pos = new Map<string, { x: number; y: number }>();
  for (const [c, ids] of columns)
    ids.forEach((id, i) =>
      pos.set(id, { x: c * 320, y: (i - (ids.length - 1) / 2) * 260 }),
    );
  return { pos, level };
}

export const FARM = process.env.NEXT_PUBLIC_BROWSERFARM_URL ?? "/farm";
export const INTERNAL =
  process.env.NEXT_PUBLIC_DASHBOARD_INTERNAL_URL ?? "http://127.0.0.1:3001";

export function targetUrl(
  id: string,
  d: Snapshot,
  all: Snapshot,
  caseId: string,
) {
  const companies = rows(all, "company");
  const carrierId = rows(d, "invoice")[0]?.carrier_id;
  const carrier = companies.find((c) => c.id === carrierId && c.usdot);
  const safer = carrier
    ? `https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string=${encodeURIComponent(str(carrier.usdot))}`
    : "";
  const shipper = companies.find((c) => c.kind === "shipper");
  if (id === "verification" && safer) return safer;
  if (id === "contacts" && shipper)
    return `https://www.sec.gov/cgi-bin/browse-edgar?company=${encodeURIComponent(str(shipper.name))}&action=getcompany`;
  if (id === "contacts" && safer) return safer;
  return `${INTERNAL}/node/${id}?case=${encodeURIComponent(caseId)}`;
}

export const displayUrl = (url: string) =>
  url.startsWith(INTERNAL)
    ? url.replace(INTERNAL, "ggf:/").replace(/\?case=$/, "")
    : url.replace(/^https?:\/\//, "");
