/**
 * Client-side simulation of the FreightLine back office.
 *
 * Produces the same `Snapshot` shape as the live SpacetimeDB subscription so
 * every view renders identically against either source. Everything here is
 * explicitly SIMULATED: the data provider labels it, and nothing is written
 * anywhere. Routing is dynamic: a case only visits the specialists its
 * scenario needs, respecting the delegation allowlist, depth <= 3 and the
 * no-repeated-ancestor rule from `common/config.py`.
 */
import { AGENT_IDS, AGENT_META, type AgentId } from "./agents";
import { emptySnapshot, str, type Row, type Snapshot, type TableName } from "./data";

/* ----------------------------- utilities ----------------------------- */

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const iso = (ms: number) => new Date(ms).toISOString();
const round2 = (n: number) => Math.round(n * 100) / 100;

/* ----------------------------- fixtures ------------------------------ */

const CARRIERS = [
  ["C-311", "Sierra Haul LLC", "3141592", "Reno, NV"],
  ["C-77", "Rapid Ridge Transport", "2718281", "Stockton, CA"],
  ["C-104", "Blue Oak Logistics", "1618033", "Fresno, CA"],
  ["C-212", "Cascade Freightways", "1414213", "Medford, OR"],
  ["C-390", "Delta Valley Carriers", "1732050", "Tracy, CA"],
  ["C-418", "Ironwood Trucking", "2236067", "Phoenix, AZ"],
  ["C-507", "Pacific Crest Transport", "2449489", "Salem, OR"],
  ["C-633", "Redwood Express", "2645751", "Eureka, CA"],
  ["C-701", "Golden Plains Haulers", "2828427", "Bakersfield, CA"],
  ["C-845", "Summit Line Freight", "3000000", "Sparks, NV"],
] as const;
const SHIPPERS = [
  ["S-07", "Bay Produce Co"],
  ["S-12", "Central Valley Foods"],
  ["S-19", "Pier 9 Imports"],
  ["S-23", "Sacramento Steel"],
  ["S-31", "NorCal Beverage"],
  ["S-44", "Tahoe Outfitters"],
] as const;
const CITIES = [
  "Stockton, CA",
  "Reno, NV",
  "Oakland, CA",
  "Sacramento, CA",
  "Fresno, CA",
  "Los Angeles, CA",
  "Portland, OR",
  "Las Vegas, NV",
  "Phoenix, AZ",
  "Salt Lake City, UT",
  "Boise, ID",
  "San Jose, CA",
];

type ScenarioKey =
  | "clean_invoice"
  | "detention_unsupported"
  | "lumper_no_receipt"
  | "missing_pod"
  | "bank_change"
  | "duplicate_invoice"
  | "rate_mismatch"
  | "shipper_short_pay"
  | "overdue_collections"
  | "customer_payment";

type Scenario = {
  key: ScenarioKey;
  weight: number;
  kind: "payable" | "receivable" | "bank_change";
  root: AgentId;
  rootSkill: string;
  priority: "P0" | "P1" | "P2" | "P3";
  title: (c: CaseCtx) => string;
};

const SCENARIOS: Scenario[] = [
  {
    key: "clean_invoice",
    weight: 42,
    kind: "payable",
    root: "carrier_ap",
    rootSkill: "process_carrier_invoice",
    priority: "P3",
    title: (c) => `Carrier invoice ${c.invoiceId} · ${c.carrierName} · ${c.lane}`,
  },
  {
    key: "detention_unsupported",
    weight: 12,
    kind: "payable",
    root: "carrier_ap",
    rootSkill: "process_carrier_invoice",
    priority: "P1",
    title: (c) => `Detention claimed without receipt · ${c.carrierName} · ${c.loadId}`,
  },
  {
    key: "lumper_no_receipt",
    weight: 5,
    kind: "payable",
    root: "carrier_ap",
    rootSkill: "process_carrier_invoice",
    priority: "P2",
    title: (c) => `Lumper fee missing receipt · ${c.carrierName} · ${c.loadId}`,
  },
  {
    key: "missing_pod",
    weight: 8,
    kind: "payable",
    root: "carrier_ap",
    rootSkill: "process_carrier_invoice",
    priority: "P2",
    title: (c) => `Signed POD missing · ${c.carrierName} · ${c.loadId}`,
  },
  {
    key: "bank_change",
    weight: 4,
    kind: "bank_change",
    root: "risk",
    rootSkill: "screen_bank_change",
    priority: "P0",
    title: (c) => `Bank detail change before ${money(c.amount)} payment · ${c.carrierName}`,
  },
  {
    key: "duplicate_invoice",
    weight: 4,
    kind: "payable",
    root: "carrier_ap",
    rootSkill: "process_carrier_invoice",
    priority: "P1",
    title: (c) => `Possible duplicate invoice · ${c.carrierName} · ${c.loadId}`,
  },
  {
    key: "rate_mismatch",
    weight: 5,
    kind: "payable",
    root: "carrier_ap",
    rootSkill: "process_carrier_invoice",
    priority: "P2",
    title: (c) => `Invoice exceeds rate con · ${c.carrierName} · ${c.loadId}`,
  },
  {
    key: "shipper_short_pay",
    weight: 7,
    kind: "receivable",
    root: "shipper_ar",
    rootSkill: "reconcile_customer_payment",
    priority: "P1",
    title: (c) => `Short-pay dispute · ${c.shipperName} · ${c.invoiceId}`,
  },
  {
    key: "overdue_collections",
    weight: 5,
    kind: "receivable",
    root: "shipper_ar",
    rootSkill: "chase_overdue",
    priority: "P2",
    title: (c) => `Overdue ${c.daysOverdue}d · ${c.shipperName} · ${c.invoiceId}`,
  },
  {
    key: "customer_payment",
    weight: 8,
    kind: "receivable",
    root: "shipper_ar",
    rootSkill: "reconcile_customer_payment",
    priority: "P3",
    title: (c) => `Customer deposit received · ${c.shipperName} · ${c.invoiceId}`,
  },
];
export const SCENARIO_KEYS = SCENARIOS.map((s) => s.key);

const money = (n: number) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(n);

const TOOLS: Record<AgentId, string[]> = {
  carrier_ap: ["read_invoice", "match_rate_con", "match_pod", "duplicate_check", "get_case_evidence"],
  risk: ["gps_geofence_check", "detention_calc", "receipt_lookup", "anomaly_score", "fmcsa_lookup"],
  documents: ["list_document_packet", "parse_pod", "request_document", "email_carrier"],
  verification: ["fmcsa_lookup", "scrape_company", "verify_bank_details", "directory_lookup"],
  controller: ["hold_ledger_check", "policy_check", "post_journal_entry", "request_approval"],
  cfo: ["load_margin", "cash_forecast_update", "collections_timing"],
  voice_desk: ["place_call", "transcript_extract"],
  validator: ["deterministic_checks", "friction_review", "write_memory"],
  shipper_ar: ["match_deposit", "remittance_parse", "aging_lookup", "draft_dunning"],
};

const DOCS = (c: CaseCtx): string[] => [
  `invoice:${c.invoiceId}.pdf#p1`,
  `ratecon:RC-${c.loadNo}.pdf#p1`,
  `pod:POD-${c.loadNo}.pdf#p2`,
  `gps:ELD-${c.loadNo}.json`,
  `email:msg-${c.loadNo}-dispatch`,
  `bank:BAI2-${c.loadNo}.txt`,
];

/* ------------------------------- types ------------------------------- */

type CaseCtx = {
  caseId: string;
  loadId: string;
  loadNo: number;
  invoiceId: string;
  carrierId: string;
  carrierName: string;
  shipperId: string;
  shipperName: string;
  lane: string;
  amount: number;
  contract: number;
  daysOverdue: number;
  scenario: Scenario;
  usedAgents: Set<AgentId>;
  rootTaskId?: string;
  invoiceRowId?: string;
};

type TaskCtx = {
  taskId: string;
  caseId: string;
  agent: AgentId;
  skill: string;
  parentTaskId: string;
  depth: number;
  ancestors: AgentId[];
  displayId: string;
};

export type SimOptions = {
  seed?: number;
  /** New cases per minute of simulated time. */
  ratePerMinute?: number;
  /** Simulation speed multiplier (1 = real time). */
  speed?: number;
  /** Minutes of history generated at construction. */
  historyMinutes?: number;
  /** Wall-clock time the sim treats as "now" at construction. */
  now?: number;
};

/* ------------------------------ the sim ------------------------------ */

export class DemoSim {
  private rng: () => number;
  private tables: Record<TableName, Row[]>;
  private index = new Map<TableName, Map<string, number>>();
  private dirty = new Set<TableName>();
  private queue: { at: number; seq: number; fn: () => void }[] = [];
  private seq = 0;
  private wallStart: number;
  private simStart: number;
  private simNow: number;
  private rate: number;
  private speed: number;
  private nextArrival: number;
  private loadCounter = 4821;
  private caseCounter = 0;
  private snapshotCache: Snapshot;
  private agentDone: Record<AgentId, number>;
  private agentErrors: Record<AgentId, number>;
  private approvalCounter = 0;
  private lastAgentRefresh = 0;
  private lastMetricRefresh = 0;
  /** Monotonic counter so audit `sequence` breaks timestamp ties. */
  private auditSeq = 0;

  constructor(options: SimOptions = {}) {
    this.rng = mulberry32(options.seed ?? 1337);
    this.tables = emptySnapshot();
    for (const name of Object.keys(this.tables) as TableName[]) this.index.set(name, new Map());
    this.rate = options.ratePerMinute ?? 8;
    this.speed = options.speed ?? 1;
    const now = options.now ?? Date.now();
    this.wallStart = now;
    const history = (options.historyMinutes ?? 45) * 60_000;
    this.simStart = now - history;
    this.simNow = this.simStart;
    this.agentDone = Object.fromEntries(AGENT_IDS.map((id) => [id, 0])) as Record<AgentId, number>;
    this.agentErrors = Object.fromEntries(AGENT_IDS.map((id) => [id, 0])) as Record<AgentId, number>;
    this.seedCounterparties();
    // History seeding draws from its own stream so case ids stay stable across
    // reloads regardless of the weekday-dependent number of seed rows.
    const main = this.rng;
    this.rng = mulberry32((options.seed ?? 1337) ^ 0x9e3779b9);
    this.seedLedgerHistory(now);
    this.seedForecast(now);
    this.rng = main;
    this.nextArrival = this.simStart + this.arrivalGap();
    // Fast-forward through the history window so the world is already busy.
    this.advanceTo(now);
    this.refreshAgents(true);
    this.refreshMetrics(true);
    this.lastPrune = -Infinity;
    this.pruneHistory();
    this.snapshotCache = this.buildSnapshot();
  }

  /* -------- public API -------- */

  /** Advance to the given wall-clock time. Returns true when data changed. */
  tick(wallNow = Date.now()): boolean {
    const target = this.simStart + (wallNow - this.wallStart) * this.speed + (this.wallStart - this.simStart);
    this.advanceTo(target);
    this.refreshAgents(false);
    this.refreshMetrics(false);
    this.pruneHistory();
    if (!this.dirty.size) return false;
    this.snapshotCache = this.buildSnapshot();
    return true;
  }
  snapshot(): Snapshot {
    return this.snapshotCache;
  }
  get ratePerMinute() {
    return this.rate;
  }
  setRate(perMinute: number) {
    this.rate = Math.max(0, perMinute);
    this.nextArrival = Math.min(this.nextArrival, this.simNow + this.arrivalGap());
  }
  /** Start a case immediately (judge injection / demo console). */
  inject(scenarioKey?: ScenarioKey): string {
    const scenario = SCENARIOS.find((s) => s.key === scenarioKey) ?? this.pickScenario();
    const ctx = this.openCase(scenario, this.simNow);
    return ctx.caseId;
  }

  /* -------- scheduling -------- */

  private schedule(at: number, fn: () => void) {
    const item = { at, seq: this.seq++, fn };
    // Binary insert keeps the queue ordered.
    let lo = 0;
    let hi = this.queue.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const q = this.queue[mid];
      if (q.at < at || (q.at === at && q.seq < item.seq)) lo = mid + 1;
      else hi = mid;
    }
    this.queue.splice(lo, 0, item);
  }
  private advanceTo(target: number) {
    while (true) {
      const next = this.queue[0];
      const arrival = this.rate > 0 ? this.nextArrival : Infinity;
      const nextAt = Math.min(next?.at ?? Infinity, arrival);
      if (nextAt > target) break;
      this.simNow = nextAt;
      if (arrival <= (next?.at ?? Infinity)) {
        this.nextArrival = this.simNow + this.arrivalGap();
        this.openCase(this.pickScenario(), this.simNow);
      } else {
        this.queue.shift();
        next!.fn();
      }
    }
    this.simNow = target;
  }
  private arrivalGap() {
    const mean = 60_000 / Math.max(this.rate, 0.01);
    // Exponential inter-arrival, clamped so bursts stay readable.
    return Math.min(mean * 4, Math.max(mean * 0.15, -Math.log(1 - this.rng()) * mean));
  }
  private pickScenario(): Scenario {
    const total = SCENARIOS.reduce((sum, s) => sum + s.weight, 0);
    let r = this.rng() * total;
    for (const s of SCENARIOS) {
      r -= s.weight;
      if (r <= 0) return s;
    }
    return SCENARIOS[0];
  }
  private pick<T>(items: readonly T[]): T {
    return items[Math.floor(this.rng() * items.length)];
  }
  private between(lo: number, hi: number) {
    return lo + this.rng() * (hi - lo);
  }
  private chance(p: number) {
    return this.rng() < p;
  }

  /* -------- table helpers -------- */

  private key(table: TableName): string {
    return table === "case" ? "case_id" : table === "task" ? "task_id" : table === "agent" ? "name" : table === "metric" ? "name" : table === "dispatch" ? "event_id" : table === "agent_config" ? "agent" : "id";
  }
  private upsert(table: TableName, row: Row) {
    const idx = this.index.get(table)!;
    const id = String(row[this.key(table)]);
    const rows = this.tables[table];
    const at = idx.get(id);
    if (at === undefined) {
      idx.set(id, rows.length);
      rows.push(row);
    } else rows[at] = { ...rows[at], ...row };
    this.dirty.add(table);
  }
  private get(table: TableName, id: string): Row | undefined {
    const at = this.index.get(table)!.get(id);
    return at === undefined ? undefined : this.tables[table][at];
  }
  private audit(row: Omit<Row, "id" | "ts" | "sequence"> & { ts?: number }) {
    const ts = row.ts ?? this.simNow;
    this.auditSeq += 1;
    this.upsert("audit_event", {
      id: `AE-${this.auditSeq.toString(36)}`,
      ts: iso(ts),
      sequence: this.auditSeq,
      evidence_refs: [],
      policy_ids: [],
      model_used: "",
      prev_hash: "",
      hash: `sim${(this.auditSeq * 2654435761 % 0xffffffff).toString(16).padStart(8, "0")}`,
      ...row,
    });
  }
  /**
   * Keep the browser snapshot bounded at high arrival rates: the audit trail
   * and document reads of long-resolved cases are dropped once the tables pass
   * a ceiling. Cases, tasks and finance rows are kept so counts stay honest.
   */
  private lastPrune = 0;
  private pruneHistory() {
    if (this.simNow - this.lastPrune < 10_000) return;
    this.lastPrune = this.simNow;
    const caps: Partial<Record<TableName, number>> = { audit_event: 30_000, doc_read: 8_000, delegation_edge: 20_000, dispatch: 20_000, call: 4_000 };
    for (const [table, cap] of Object.entries(caps) as [TableName, number][]) {
      const rows = this.tables[table];
      if (rows.length <= cap * 1.2) continue;
      const resolved = new Set(this.tables.case.filter((c) => c.status === "resolved").map((c) => String(c.case_id)));
      const keepFrom = rows.length - cap;
      const kept = rows.filter((r, i) => i >= keepFrom || !resolved.has(String(r.case_id)));
      this.tables[table] = kept;
      const idx = this.index.get(table)!;
      idx.clear();
      const key = this.key(table);
      kept.forEach((r, i) => idx.set(String(r[key]), i));
      this.dirty.add(table);
    }
  }
  private buildSnapshot(): Snapshot {
    const next = { ...this.snapshotCache } as Snapshot;
    for (const name of this.dirty) next[name] = [...this.tables[name]];
    if (!this.snapshotCache) for (const name of Object.keys(this.tables) as TableName[]) next[name] = [...this.tables[name]];
    this.dirty.clear();
    return next;
  }

  /* -------- seeding -------- */

  private seedCounterparties() {
    for (const [id, name, usdot, city] of CARRIERS) {
      this.upsert("company", {
        id,
        kind: "carrier",
        name,
        usdot,
        mc: `MC-${usdot.slice(0, 6)}`,
        phone: `+1 775 555 0${usdot.slice(-3)}`,
        website: `https://${name.toLowerCase().replace(/[^a-z]+/g, "")}.example`,
        source_url: `https://safer.fmcsa.dot.gov/query.asp?usdot=${usdot}`,
        city,
      });
      this.upsert("contact", {
        id: `CT-${id}`,
        company_id: id,
        name: this.pick(["Dana Whitfield", "Marcus Lee", "Priya Natarajan", "Luis Ortega", "Hannah Kim", "Omar Haddad"]),
        title: "Dispatcher",
        phone: `+1 775 555 0${usdot.slice(-3)}`,
        email: `dispatch@${name.toLowerCase().replace(/[^a-z]+/g, "")}.example`,
        source: this.pick(["fmcsa", "website", "context_dev"]),
        confidence: round2(this.between(0.7, 0.98)),
      });
    }
    for (const [id, name] of SHIPPERS)
      this.upsert("company", { id, kind: "shipper", name, usdot: "", mc: "", phone: "", website: "", source_url: "" });
    for (const id of AGENT_IDS) this.upsert("agent_config", { agent: id, model: "auto", enabled: true });
    this.upsert("accounting_period", { period: iso(this.simStart).slice(0, 7), status: "open", updated_at: iso(this.simStart) });
  }

  /** 30 days of posted journals so trends and balances exist on day one. */
  private seedLedgerHistory(now: number) {
    const day = 86_400_000;
    let n = 0;
    for (let d = 30; d >= 1; d--) {
      const at = now - d * day + this.between(8, 18) * 3_600_000;
      const weekend = [0, 6].includes(new Date(at).getDay());
      const loads = weekend ? Math.round(this.between(2, 5)) : Math.round(this.between(9, 16));
      for (let i = 0; i < loads; i++) {
        const revenue = round2(this.between(1_400, 4_200));
        const cost = round2(revenue * this.between(0.78, 0.9));
        const ts = at + i * 600_000;
        const caseId = `CASE-H${d}${i}`;
        // Billing: AR / revenue
        this.upsert("ledger_entry", {
          id: `JE-H${++n}`,
          case_id: caseId,
          period: iso(ts).slice(0, 7),
          lines: [
            { account: "accounts_receivable", debit: revenue, credit: 0 },
            { account: "freight_revenue", debit: 0, credit: revenue },
          ],
          memo: "Customer billing",
          requested_by: "shipper_ar",
          approved_by: "controller",
          posted_at: iso(ts),
        });
        // Carrier cost: expense / AP
        this.upsert("ledger_entry", {
          id: `JE-H${++n}`,
          case_id: caseId,
          period: iso(ts).slice(0, 7),
          lines: [
            { account: "freight_expense", debit: cost, credit: 0 },
            { account: "accounts_payable", debit: 0, credit: cost },
          ],
          memo: "Carrier settlement",
          requested_by: "carrier_ap",
          approved_by: "controller",
          posted_at: iso(ts + 120_000),
        });
        // Cash movements lag: collections ~ 70% within window, payments ~ 80%.
        if (d > 3 && this.chance(0.7))
          this.upsert("ledger_entry", {
            id: `JE-H${++n}`,
            case_id: caseId,
            period: iso(ts).slice(0, 7),
            lines: [
              { account: "cash", debit: revenue, credit: 0 },
              { account: "accounts_receivable", debit: 0, credit: revenue },
            ],
            memo: "Customer deposit matched",
            requested_by: "shipper_ar",
            approved_by: "controller",
            posted_at: iso(Math.min(now - 60_000, ts + this.between(2, 20) * day)),
          });
        if (d > 2 && this.chance(0.8))
          this.upsert("ledger_entry", {
            id: `JE-H${++n}`,
            case_id: caseId,
            period: iso(ts).slice(0, 7),
            lines: [
              { account: "accounts_payable", debit: cost, credit: 0 },
              { account: "cash", debit: 0, credit: cost },
            ],
            memo: "Carrier payment released",
            requested_by: "carrier_ap",
            approved_by: "controller",
            posted_at: iso(Math.min(now - 60_000, ts + this.between(1, 14) * day)),
          });
      }
    }
    // Opening cash balance so the position is positive.
    this.upsert("ledger_entry", {
      id: "JE-OPEN",
      case_id: "",
      period: iso(now - 31 * day).slice(0, 7),
      lines: [
        { account: "cash", debit: 412_500, credit: 0 },
        { account: "opening_balance_equity", debit: 0, credit: 412_500 },
      ],
      memo: "Opening balance",
      requested_by: "controller",
      approved_by: "human:controller",
      posted_at: iso(now - 31 * day),
    });
  }

  private seedForecast(now: number) {
    const week = 7 * 86_400_000;
    for (let w = 0; w < 6; w++) {
      const period = iso(now + w * week).slice(0, 10);
      this.upsert("cash_forecast", {
        id: `CF-${w}-in`,
        case_id: "",
        period,
        category: "collections",
        amount: round2(this.between(120_000, 190_000)),
        confidence: round2(this.between(0.6, 0.92) - w * 0.05),
        assumptions_json: JSON.stringify({ basis: "AR aging + promised payments", week: w }),
        updated_at: iso(now),
      });
      this.upsert("cash_forecast", {
        id: `CF-${w}-out`,
        case_id: "",
        period,
        category: "carrier_payables",
        amount: round2(-this.between(100_000, 160_000)),
        confidence: round2(this.between(0.7, 0.95) - w * 0.04),
        assumptions_json: JSON.stringify({ basis: "approved settlements + open invoices", week: w }),
        updated_at: iso(now),
      });
    }
  }

  /* -------- cases -------- */

  private openCase(scenario: Scenario, at: number): CaseCtx {
    const loadNo = ++this.loadCounter;
    const [carrierId, carrierName] = this.pick(CARRIERS);
    const [shipperId, shipperName] = this.pick(SHIPPERS);
    const from = this.pick(CITIES);
    let to = this.pick(CITIES);
    while (to === from) to = this.pick(CITIES);
    const contract = Math.round(this.between(900, 6_800) / 10) * 10;
    let amount = contract;
    switch (scenario.key) {
      case "detention_unsupported":
        amount = contract + this.pick([150, 250, 300, 375, 450]);
        break;
      case "lumper_no_receipt":
        amount = contract + this.pick([75, 125, 180, 220]);
        break;
      case "rate_mismatch":
        amount = Math.round(contract * this.between(1.04, 1.18));
        break;
      case "bank_change":
        amount = Math.round(this.between(4_000, 14_000));
        break;
      case "shipper_short_pay":
        amount = Math.round(contract * this.between(1.12, 1.25));
        break;
      case "overdue_collections":
        amount = Math.round(contract * this.between(1.1, 1.3));
        break;
      case "customer_payment":
        amount = Math.round(contract * 1.18);
        break;
    }
    this.caseCounter += 1;
    const suffix = scenario.kind === "bank_change" ? "-BANK" : scenario.kind === "receivable" ? "-AR" : "";
    const ctx: CaseCtx = {
      caseId: `CASE-${loadNo}${suffix}`,
      loadId: `LOAD-${loadNo}`,
      loadNo,
      invoiceId: `INV-${loadNo}`,
      carrierId,
      carrierName,
      shipperId,
      shipperName,
      lane: `${from.split(",")[0]} → ${to.split(",")[0]}`,
      amount,
      contract,
      daysOverdue: Math.round(this.between(31, 75)),
      scenario,
      usedAgents: new Set(),
    };
    const exposure =
      scenario.kind === "bank_change"
        ? amount
        : scenario.kind === "receivable"
          ? scenario.key === "customer_payment"
            ? 0
            : Math.round(amount * (scenario.key === "shipper_short_pay" ? 0.18 : 1))
          : Math.max(0, amount - contract);
    this.upsert("case", {
      case_id: ctx.caseId,
      load_id: ctx.loadId,
      opened_at: iso(at),
      status: "open",
      priority: scenario.priority,
      title: scenario.title(ctx),
      exposure_usd: exposure,
      resolved_at: "",
      scenario: scenario.key,
      carrier_id: carrierId,
      shipper_id: shipperId,
      lane: ctx.lane,
    });
    if (scenario.kind !== "receivable") {
      ctx.invoiceRowId = ctx.invoiceId;
      this.upsert("invoice", {
        id: ctx.invoiceId,
        case_id: ctx.caseId,
        carrier_id: carrierId,
        load_id: ctx.loadId,
        amount,
        contract_amount: contract,
        paid_amount: 0,
        issued_at: iso(at - this.between(1, 4) * 86_400_000),
        status: "open",
      });
    }
    // Intake / first-hop routing: the harness classifies, then dispatches.
    const queueMs = Math.round(this.between(120, 900));
    const rootTaskId = `TASK-${loadNo}-1`;
    this.upsert("dispatch", {
      event_id: `EVT-${loadNo}`,
      case_id: ctx.caseId,
      skill: scenario.rootSkill,
      agent: scenario.root,
      task_id: rootTaskId,
      status: "accepted",
      reason: `classified ${scenario.key.replaceAll("_", " ")} (confidence ${round2(this.between(0.72, 0.98))})`,
      queue_ms: queueMs,
      policy_version: "routing-v3",
      ts: iso(at + queueMs),
    });
    ctx.rootTaskId = rootTaskId;
    this.schedule(at + queueMs + this.between(300, 1_500), () =>
      this.startTask(ctx, {
        taskId: rootTaskId,
        caseId: ctx.caseId,
        agent: scenario.root,
        skill: scenario.rootSkill,
        parentTaskId: "",
        depth: 0,
        ancestors: [],
        displayId: `${ctx.caseId}/1`,
      }),
    );
    return ctx;
  }

  private taskCount = new Map<string, number>();
  private reviewed = new Set<string>();
  private resumeTries = new Map<string, number>();
  private pendingRetries = new Map<string, number>();
  private nextTaskId(ctx: CaseCtx) {
    const n = (this.taskCount.get(ctx.caseId) ?? 1) + 1;
    this.taskCount.set(ctx.caseId, n);
    return { taskId: `TASK-${ctx.loadNo}-${n}`, displayId: `${ctx.caseId}/${n}` };
  }

  private startTask(ctx: CaseCtx, t: TaskCtx) {
    ctx.usedAgents.add(t.agent);
    const now = this.simNow;
    this.upsert("task", {
      task_id: t.taskId,
      case_id: t.caseId,
      parent_task_id: t.parentTaskId,
      agent: t.agent,
      skill: t.skill,
      display_id: t.displayId,
      a2a_state: "working",
      business_status: "investigating",
      outcome: "",
      confidence: 0,
      model_used: this.pick(["gpt-6-luna", "gpt-6-luna", "claude-opus-5-5"]),
      started_at: iso(now),
      updated_at: iso(now),
      evidence_refs: [],
      artifact_json: "",
      policy_ids: [],
      depth: t.depth,
    });
    // Post-resolution quality review does not reopen the case.
    if (this.get("case", ctx.caseId)?.status !== "resolved") this.upsert("case", { case_id: ctx.caseId, status: "in_progress" });
    this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: t.agent, kind: "upsert_task", reason_code: "TASK_WORKING", summary: `${AGENT_META[t.agent].name} started ${t.skill.replaceAll("_", " ")}` });

    // Tool calls and document reads, spread across the task's duration.
    const duration = this.taskDuration(t);
    const tools = TOOLS[t.agent];
    const toolCount = 1 + Math.floor(this.rng() * Math.min(3, tools.length));
    const docs = DOCS(ctx);
    let cursor = now + this.between(300, 900);
    const evidence: string[] = [];
    for (let i = 0; i < toolCount; i++) {
      const tool = tools[(i + Math.floor(this.rng() * tools.length)) % tools.length];
      const callId = `${t.taskId}-tc${i + 1}`;
      const toolMs = Math.round(this.between(400, 2_600));
      const startAt = cursor;
      const args = this.toolArgs(tool, ctx);
      this.schedule(startAt, () => this.toolEvent(ctx, t, callId, tool, "started", args, undefined, 0));
      this.schedule(startAt + toolMs, () => this.toolEvent(ctx, t, callId, tool, "completed", args, this.toolResult(tool, ctx), toolMs));
      if (this.chance(0.55)) {
        const doc = docs[Math.floor(this.rng() * (ctx.scenario.kind === "bank_change" ? docs.length : docs.length - 1))];
        evidence.push(doc);
        this.schedule(startAt + toolMs * 0.5, () =>
          this.upsert("doc_read", {
            id: `DR-${callId}`,
            change_set_id: "",
            task_id: t.taskId,
            agent: t.agent,
            doc_ref: doc,
            page: doc.includes("#p") ? Number(doc.split("#p")[1]) : 1,
            span: `L${Math.round(this.between(1, 12))}-L${Math.round(this.between(13, 40))}`,
            extracted_json: JSON.stringify(this.extraction(doc, ctx)),
            ts: iso(startAt + toolMs * 0.5),
          }),
        );
      }
      cursor = startAt + toolMs + this.between(200, 1_200);
    }
    const finishAt = Math.max(cursor + 400, now + duration);
    if (t.skill === "place_call") this.scheduleCall(ctx, t, now, finishAt);
    this.schedule(finishAt, () => this.completeTask(ctx, t, evidence));
  }

  private taskDuration(t: TaskCtx) {
    const base: Record<AgentId, [number, number]> = {
      carrier_ap: [5, 14],
      shipper_ar: [5, 13],
      cfo: [4, 9],
      risk: [6, 16],
      controller: [4, 10],
      verification: [6, 14],
      voice_desk: [18, 45],
      validator: [5, 11],
      documents: [6, 15],
    };
    const [lo, hi] = base[t.agent];
    return this.between(lo, hi) * 1_000;
  }

  private toolEvent(ctx: CaseCtx, t: TaskCtx, callId: string, tool: string, status: string, args: Row, result: Row | undefined, durationMs: number) {
    this.audit({
      case_id: ctx.caseId,
      task_id: t.taskId,
      agent: t.agent,
      kind: "tool_call",
      reason_code: `tool_${status}`,
      summary: JSON.stringify({ version: 1, tool_call_id: callId, tool_name: tool, status, arguments: args, result, duration_ms: durationMs, model: "gpt-6-luna" }),
      model_used: "gpt-6-luna",
    });
  }
  private toolArgs(tool: string, ctx: CaseCtx): Row {
    switch (tool) {
      case "fmcsa_lookup":
        return { usdot: CARRIERS.find((c) => c[0] === ctx.carrierId)?.[2] };
      case "place_call":
        return { purpose: ctx.scenario.kind === "receivable" ? "collections" : "verification", counterparty_id: ctx.carrierId, routed_to: "team line (demo allowlist)" };
      case "gps_geofence_check":
        return { load_id: ctx.loadId, stop: "receiver" };
      default:
        return { case_id: ctx.caseId, load_id: ctx.loadId };
    }
  }
  private toolResult(tool: string, ctx: CaseCtx): Row {
    switch (tool) {
      case "gps_geofence_check":
        return { arrived: "14:02", departed: "14:49", on_site_minutes: 47, free_time_minutes: 120 };
      case "detention_calc":
        return { claimed_hours: 2, supported_hours: 0, owed_usd: 0 };
      case "match_rate_con":
        return { contract_amount: ctx.contract, invoice_amount: ctx.amount, delta: ctx.amount - ctx.contract };
      case "duplicate_check":
        return { duplicates: ctx.scenario.key === "duplicate_invoice" ? 1 : 0, window_days: 14 };
      case "fmcsa_lookup":
        return { legal_name: ctx.carrierName, authority_status: "ACTIVE", insurance_on_file: true, oos_rate: round2(this.between(0.02, 0.2)) };
      case "load_margin":
        return { revenue: Math.round(ctx.contract * 1.18), cost: ctx.amount, margin_pct: round2(((ctx.contract * 1.18 - ctx.amount) / (ctx.contract * 1.18)) * 100) };
      case "verify_bank_details":
        return { verified: ctx.scenario.key !== "bank_change", method: "out_of_band_phone" };
      case "match_deposit":
        return { deposit: Math.round(ctx.amount * (ctx.scenario.key === "shipper_short_pay" ? 0.82 : 1)), invoice: ctx.amount };
      default:
        return { ok: true };
    }
  }
  private extraction(doc: string, ctx: CaseCtx): Row {
    if (doc.startsWith("invoice")) return { total: ctx.amount, line_items: [{ desc: "Linehaul", amount: ctx.contract }, ...(ctx.amount > ctx.contract ? [{ desc: "Accessorial", amount: ctx.amount - ctx.contract }] : [])] };
    if (doc.startsWith("ratecon")) return { rate: ctx.contract, lane: ctx.lane, free_time_hours: 2 };
    if (doc.startsWith("pod")) return { signed: ctx.scenario.key !== "missing_pod", signer: "Receiving clerk", delivered_at: "14:49" };
    if (doc.startsWith("gps")) return { pings: 212, geofence_minutes: 47 };
    if (doc.startsWith("bank")) return { new_routing: "•••• 2291", previous_routing: "•••• 8830", changed_by_email: true };
    return { subject: `Re: ${ctx.loadId}`, from: "dispatch@carrier" };
  }

  private scheduleCall(ctx: CaseCtx, t: TaskCtx, start: number, end: number) {
    const id = `CALL-${t.taskId}`;
    const purpose = ctx.scenario.kind === "receivable" ? "collections" : "verification";
    this.upsert("task", { task_id: t.taskId, business_status: "awaiting_call" });
    this.upsert("call", {
      id,
      case_id: ctx.caseId,
      task_id: t.taskId,
      persona: purpose,
      direction: "outbound",
      to_masked: "+1 775 ••• ••12",
      intended_to: "+1 775 555 0112 (FMCSA-registered)",
      routed_to: "team line (demo allowlist)",
      company_id: ctx.carrierId,
      status: "ringing",
      started_at: iso(start + 1_500),
      ended_at: "",
      findings_json: "",
      transcript_redacted: "",
      disclosure_given: true,
    });
    this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "voice_desk", kind: "upsert_call", reason_code: "CALL_RINGING", summary: `Dialing ${ctx.carrierName} dispatcher (demo-routed)`, policy_ids: ["P-008", "P-011"] });
    this.schedule(start + 6_000, () => this.upsert("call", { id, status: "in_progress" }));
    this.schedule(end - 300, () => {
      const reached = this.chance(0.8);
      this.upsert("call", {
        id,
        status: reached ? "completed" : "no_answer",
        ended_at: iso(end - 300),
        findings_json: JSON.stringify(reached ? { reached: true, claim_confirmed: false, promised_document: "detention receipt", promised_by: "end of day" } : { reached: false }),
        transcript_redacted: reached ? "AI: Hi, this is an AI assistant calling for Golden Gate Freight about load " + ctx.loadNo + "… Dispatcher: Let me check with the driver and send the receipt." : "",
      });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "voice_desk", kind: "upsert_call", reason_code: reached ? "CALL_COMPLETED" : "CALL_NO_ANSWER", summary: reached ? "Call completed; dispatcher will send the receipt" : "No answer; will retry within calling window" });
    });
  }

  private completeTask(ctx: CaseCtx, t: TaskCtx, evidence: string[]) {
    const failed = this.chance(0.025);
    const now = this.simNow;
    const outcome = failed ? "failed" : this.outcomeFor(ctx, t);
    const needsHuman = !failed && this.needsHumanApproval(ctx, t);
    const artifact = this.artifactFor(ctx, t, outcome);
    this.upsert("task", {
      task_id: t.taskId,
      a2a_state: failed ? "failed" : needsHuman ? "input-required" : "completed",
      business_status: failed ? "failed" : needsHuman ? "awaiting_human_approval" : outcome === "rejected" ? "rejected" : "resolved",
      outcome,
      confidence: round2(this.between(0.74, 0.99)),
      updated_at: iso(now),
      evidence_refs: evidence,
      artifact_json: JSON.stringify(artifact),
      policy_ids: artifact.policy_ids ?? [],
    });
    if (failed) {
      this.agentErrors[t.agent] += 1;
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: t.agent, kind: "upsert_task", reason_code: "TASK_FAILED", summary: `${AGENT_META[t.agent].name} failed: provider timeout after 3 retries; task parked for retry`, evidence_refs: evidence });
      // Retry later with a new task id so the case still finishes. The case
      // stays open until the retry has started.
      this.pendingRetries.set(ctx.caseId, (this.pendingRetries.get(ctx.caseId) ?? 0) + 1);
      this.schedule(now + this.between(20, 45) * 1_000, () => {
        const { taskId, displayId } = this.nextTaskId(ctx);
        this.pendingRetries.set(ctx.caseId, Math.max(0, (this.pendingRetries.get(ctx.caseId) ?? 1) - 1));
        this.audit({ case_id: ctx.caseId, task_id: taskId, agent: t.agent, kind: "upsert_task", reason_code: "TASK_RETRY", summary: `Retrying ${t.skill.replaceAll("_", " ")} after failure` });
        if (t.parentTaskId) {
          // A retried child is re-reserved by its parent, so it gets a fresh edge.
          const from = this.get("task", t.parentTaskId)?.agent;
          this.upsert("delegation_edge", { id: `DE-${taskId}`, ts: iso(this.simNow), case_id: ctx.caseId, from_agent: str(from, t.ancestors[t.ancestors.length - 1] ?? "intake"), to_agent: t.agent, skill: t.skill, task_id: taskId, parent_task_id: t.parentTaskId, depth: t.depth });
          this.upsert("task", { task_id: t.parentTaskId, a2a_state: "working", business_status: "awaiting_other_agent" });
          this.audit({ case_id: ctx.caseId, task_id: taskId, agent: str(from, "intake"), kind: "record_delegation", reason_code: "A2A_REDELEGATE", summary: `${str(from, "Parent")} re-delegated ${t.skill.replaceAll("_", " ")} to ${AGENT_META[t.agent].name} after a failure` });
          const parent = this.get("task", t.parentTaskId);
          if (parent) this.schedule(this.simNow + this.between(12, 25) * 1_000, () => this.resumeParent(ctx, { taskId: t.parentTaskId, caseId: ctx.caseId, agent: str(parent.agent) as AgentId, skill: str(parent.skill), parentTaskId: str(parent.parent_task_id, ""), depth: t.depth - 1, ancestors: t.ancestors.slice(0, -1), displayId: str(parent.display_id) }));
        }
        this.startTask(ctx, { ...t, taskId, displayId });
      });
      return;
    }
    this.agentDone[t.agent] += 1;
    this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: t.agent, kind: "upsert_task", reason_code: needsHuman ? "AWAITING_HUMAN" : "TASK_COMPLETED", summary: artifact.summary as string, evidence_refs: evidence, policy_ids: (artifact.policy_ids as string[]) ?? [] });
    this.financialEffects(ctx, t, outcome, now);
    if (needsHuman) this.requestApproval(ctx, t, now);

    // Dynamic routing: decide who, if anyone, this agent hands off to.
    const children = this.childrenFor(ctx, t);
    for (const [i, child] of children.entries()) {
      const { taskId, displayId } = this.nextTaskId(ctx);
      const at = now + this.between(400, 1_800) + i * 600;
      this.schedule(at, () => {
        this.upsert("delegation_edge", {
          id: `DE-${taskId}`,
          ts: iso(at),
          case_id: ctx.caseId,
          from_agent: t.agent,
          to_agent: child.to,
          skill: child.skill,
          task_id: taskId,
          parent_task_id: t.taskId,
          depth: t.depth + 1,
        });
        this.upsert("task", { task_id: t.taskId, a2a_state: "working", business_status: "awaiting_other_agent" });
        this.audit({ case_id: ctx.caseId, task_id: taskId, agent: t.agent, kind: "record_delegation", reason_code: "A2A_DELEGATE", summary: `${AGENT_META[t.agent].name} → ${AGENT_META[child.to].name}: ${child.skill.replaceAll("_", " ")}` });
        this.startTask(ctx, { taskId, caseId: ctx.caseId, agent: child.to, skill: child.skill, parentTaskId: t.taskId, depth: t.depth + 1, ancestors: [...t.ancestors, t.agent], displayId });
      });
    }
    if (children.length) {
      // Parent resumes once children are done: approximate by re-completing after the longest child.
      const resumeAt = now + 2_000 + children.length * this.between(9, 20) * 1_000;
      this.schedule(resumeAt, () => this.resumeParent(ctx, t));
    } else this.maybeCloseCase(ctx, now);
  }

  private resumeParent(ctx: CaseCtx, t: TaskCtx) {
    const row = this.get("task", t.taskId);
    if (!row) return;
    const open =
      (this.pendingRetries.get(ctx.caseId) ?? 0) > 0 ||
      this.tables.task.some((r) => r.parent_task_id === t.taskId && !["completed", "failed", "canceled", "rejected"].includes(String(r.a2a_state)));
    if (open) {
      // Children still running (a call, a parked approval): poll with backoff, bounded.
      const tries = (this.resumeTries.get(t.taskId) ?? 0) + 1;
      this.resumeTries.set(t.taskId, tries);
      if (tries < 120) this.schedule(this.simNow + Math.min(30_000, 3_000 * tries), () => this.resumeParent(ctx, t));
      return;
    }
    if (row.business_status === "awaiting_other_agent") {
      this.upsert("task", { task_id: t.taskId, a2a_state: "completed", business_status: "resolved", updated_at: iso(this.simNow) });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: t.agent, kind: "upsert_task", reason_code: "TASK_RESUMED", summary: `${AGENT_META[t.agent].name} resumed with delegated results and completed` });
    }
    this.maybeCloseCase(ctx, this.simNow);
  }

  private maybeCloseCase(ctx: CaseCtx, now: number) {
    if ((this.pendingRetries.get(ctx.caseId) ?? 0) > 0) return;
    const open = this.tables.task.some((r) => r.case_id === ctx.caseId && !["completed", "failed", "canceled", "rejected"].includes(String(r.a2a_state)));
    const pendingApproval = this.tables.approval.some((r) => r.case_id === ctx.caseId && (!r.decision || r.decision === "pending"));
    if (open || pendingApproval) return;
    const c = this.get("case", ctx.caseId);
    if (!c || c.status === "resolved") return;
    this.upsert("case", { case_id: ctx.caseId, status: "resolved", resolved_at: iso(now) });
    // Settlement Quality Review looks at every financial case after the fact (once).
    if (!this.reviewed.has(ctx.caseId) && (ctx.scenario.kind !== "receivable" || this.chance(0.6))) {
      this.reviewed.add(ctx.caseId);
      const { taskId, displayId } = this.nextTaskId(ctx);
      this.schedule(now + this.between(1_500, 5_000), () => {
        this.audit({ case_id: ctx.caseId, task_id: taskId, agent: "validator", kind: "record_delegation", reason_code: "REVIEW_OUTBOX", summary: "Review outbox delivered the financial output for quality review" });
        this.startTask(ctx, { taskId, caseId: ctx.caseId, agent: "validator", skill: "review_output", parentTaskId: "", depth: 0, ancestors: [], displayId });
      });
    }
  }

  private needsHumanApproval(ctx: CaseCtx, t: TaskCtx) {
    if (t.agent !== "controller") return false;
    if (ctx.scenario.kind === "bank_change") return true;
    return ctx.amount > 5_000 && ["review_hold", "approve_entry"].includes(t.skill);
  }

  private requestApproval(ctx: CaseCtx, t: TaskCtx, now: number) {
    const id = `APR-${++this.approvalCounter}`;
    const action = ctx.scenario.kind === "bank_change" ? "release_hold" : "propose_payment";
    this.upsert("approval", {
      id,
      case_id: ctx.caseId,
      task_id: t.taskId,
      action,
      target_id: ctx.invoiceId,
      amount: ctx.amount,
      requested_by: "controller",
      approver: "",
      approver_name: "Human controller",
      decision: "pending",
      code_ok: false,
      decided_at: "",
      expires_at: iso(now + 3_600_000),
      invoice_id: ctx.invoiceId,
      requested_at: iso(now),
    });
    this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "controller", kind: "request_approval", reason_code: ctx.scenario.kind === "bank_change" ? "P-004" : "P-003", summary: `Human approval requested: ${action.replaceAll("_", " ")} ${money(ctx.amount)}`, policy_ids: [ctx.scenario.kind === "bank_change" ? "P-004" : "P-003", "P-005"] });
    // A human decides within a few minutes in the demo world; the spread keeps a
    // handful pending at any time without stranding cases forever.
    this.schedule(now + this.between(45, 420) * 1_000, () => {
        const approved = this.chance(0.85);
        this.upsert("approval", { id, decision: approved ? "approved" : "rejected", code_ok: true, decided_at: iso(this.simNow), approver: "human", approver_name: "Human controller" });
        this.upsert("task", { task_id: t.taskId, a2a_state: "completed", business_status: approved ? "resolved" : "rejected", updated_at: iso(this.simNow) });
        this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "human", kind: "decide_approval", reason_code: approved ? "APPROVED" : "REJECTED", summary: `Human ${approved ? "approved" : "rejected"} ${action.replaceAll("_", " ")} ${money(ctx.amount)}`, policy_ids: ["P-005"] });
        if (approved && action === "propose_payment") this.postPayment(ctx, this.simNow);
        this.maybeCloseCase(ctx, this.simNow);
      });
  }

  private outcomeFor(ctx: CaseCtx, t: TaskCtx): string {
    const k = ctx.scenario.key;
    if (t.agent === "validator") return this.chance(0.9) ? "pass" : "issue";
    if (t.agent === "voice_desk") return "call_completed";
    if (k === "duplicate_invoice" && t.agent === "controller") return "rejected";
    if (["detention_unsupported", "lumper_no_receipt", "rate_mismatch"].includes(k) && t.agent === "controller") return "hold_placed";
    if (k === "bank_change" && t.agent === "verification") return "not_verified";
    if (t.agent === "carrier_ap" && k === "clean_invoice") return "payment_proposed";
    if (t.agent === "cfo") return "impact_assessed";
    return "completed";
  }

  private artifactFor(ctx: CaseCtx, t: TaskCtx, outcome: string): Row {
    const meta = AGENT_META[t.agent];
    const base = { agent: t.agent, skill: t.skill, outcome, case_id: ctx.caseId, load_id: ctx.loadId };
    switch (t.agent) {
      case "carrier_ap":
        return { ...base, summary: outcome === "payment_proposed" ? `Invoice ${ctx.invoiceId} matches rate con; ${money(ctx.amount)} proposed for payment` : `Invoice ${ctx.invoiceId} is ${money(ctx.amount - ctx.contract)} over contract; routed for investigation`, findings: { invoice_amount: ctx.amount, contract_amount: ctx.contract, delta: ctx.amount - ctx.contract }, policy_ids: ctx.amount > ctx.contract * 1.02 ? ["P-001"] : [] };
      case "risk":
        return { ...base, summary: ctx.scenario.key === "bank_change" ? "Bank change arrived by email the day before payment; flagged as high-risk (P-004)" : `GPS shows 47 min on site vs 2 h claimed; ${money(ctx.amount - ctx.contract)} unsupported`, findings: { claimed_minutes: 120, supported_minutes: 47, unsupported_usd: ctx.amount - ctx.contract }, policy_ids: ctx.scenario.key === "bank_change" ? ["P-004", "P-010"] : ["P-001"] };
      case "documents":
        return { ...base, summary: ctx.scenario.key === "missing_pod" ? "Packet incomplete: signed POD missing; requested from carrier dispatcher" : "Packet checked: rate con and invoice present, receipt requested", findings: { packet: { ratecon: true, invoice: true, pod: ctx.scenario.key !== "missing_pod", receipt: false } } };
      case "verification":
        return { ...base, summary: outcome === "not_verified" ? "Bank change could not be verified on the FMCSA-registered number; hold recommended" : `${ctx.carrierName}: authority ACTIVE, insurance on file, dispatcher reachable on directory number`, findings: { authority_status: "ACTIVE", insurance_on_file: true, verified: outcome !== "not_verified" }, policy_ids: ["P-008"] };
      case "controller":
        return { ...base, summary: outcome === "rejected" ? `Duplicate of a paid invoice within 14 days; rejected (P-007)` : outcome === "hold_placed" ? `Hold placed on ${money(ctx.amount - ctx.contract)}; ${money(ctx.contract)} blocked while the hold is active (P-002)` : `Reconciled and posted a balanced journal for ${money(ctx.amount)}`, policy_ids: outcome === "rejected" ? ["P-007"] : outcome === "hold_placed" ? ["P-001", "P-002"] : ["P-006", "P-005"] };
      case "cfo":
        return { ...base, summary: `Margin ${round2(((ctx.contract * 1.18 - ctx.amount) / (ctx.contract * 1.18)) * 100)}% on ${ctx.loadId}; cash impact ${money(-ctx.amount)} in ${this.pick(["7", "14", "21"])} days`, findings: { invoiced_margin: round2(ctx.contract * 1.18 - ctx.amount), expected_collections: Math.round(ctx.contract * 1.18), net_cash: Math.round(ctx.contract * 0.18) } };
      case "voice_desk":
        return { ...base, summary: "Call completed with AI disclosure; dispatcher promised the receipt by end of day", findings: { reached: true, promised_document: "receipt" }, policy_ids: ["P-008", "P-011"] };
      case "validator":
        return { ...base, summary: outcome === "pass" ? "All deterministic checks passed; no friction recorded" : "Issue: AP re-requested the rate con twice; memory note written for Carrier Settlement", checks: [{ name: "debits_equal_credits", ok: true }, { name: "hold_matches_delta", ok: outcome === "pass" }], friction: outcome === "pass" ? [] : [{ kind: "retry", detail: "rate con requested twice", cost_s: 42 }] };
      case "shipper_ar":
        return { ...base, summary: ctx.scenario.key === "shipper_short_pay" ? `Deposit short by ${money(Math.round(ctx.amount * 0.18))}; dispute opened with ${ctx.shipperName}` : ctx.scenario.key === "overdue_collections" ? `${ctx.invoiceId} is ${ctx.daysOverdue} days overdue; dunning drafted and call queued` : `Deposit matched to ${ctx.invoiceId}; journal requested`, findings: { invoice_amount: ctx.amount, matched_amount: ctx.scenario.key === "shipper_short_pay" ? Math.round(ctx.amount * 0.82) : ctx.amount, balance: ctx.scenario.key === "shipper_short_pay" ? Math.round(ctx.amount * 0.18) : 0 } };
      default:
        return { ...base, summary: `${meta.name} completed ${t.skill}` };
    }
  }

  private financialEffects(ctx: CaseCtx, t: TaskCtx, outcome: string, now: number) {
    if (t.agent === "controller" && outcome === "hold_placed") {
      const amount = Math.max(50, ctx.amount - ctx.contract);
      this.upsert("hold", { id: `HOLD-${t.taskId}`, case_id: ctx.caseId, invoice_id: ctx.invoiceId, amount, reason_code: ctx.scenario.key.toUpperCase(), status: "active", placed_by: "controller", released_by: "", released_amount: 0, placed_at: iso(now), released_at: "" });
      this.upsert("invoice", { id: ctx.invoiceId, status: "held" });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "controller", kind: "place_hold", reason_code: "P-001", summary: `Hold placed: ${money(amount)} on ${ctx.invoiceId}`, policy_ids: ["P-001", "P-002"] });
      // Many holds release once evidence arrives.
      if (this.chance(0.5))
        this.schedule(now + this.between(90, 300) * 1_000, () => {
          this.upsert("hold", { id: `HOLD-${t.taskId}`, status: "released", released_by: "controller", released_amount: amount, released_at: iso(this.simNow) });
          this.upsert("invoice", { id: ctx.invoiceId, status: "open" });
          this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "controller", kind: "release_hold", reason_code: "EVIDENCE_RECEIVED", summary: `Hold released: receipt received for ${ctx.invoiceId}`, policy_ids: ["P-003"] });
        });
    }
    if (t.agent === "controller" && outcome === "rejected") {
      this.upsert("invoice", { id: ctx.invoiceId, status: "rejected" });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "controller", kind: "policy_deny", reason_code: "P-007", summary: `Duplicate invoice rejected: same carrier, load and amount within 14 days`, policy_ids: ["P-007"] });
    }
    if (t.agent === "carrier_ap" && outcome === "payment_proposed") {
      this.upsert("payment_proposal", { id: `PP-${t.taskId}`, case_id: ctx.caseId, carrier_id: ctx.carrierId, invoice_id: ctx.invoiceId, load_id: ctx.loadId, amount: ctx.amount, status: ctx.amount > 5_000 ? "awaiting_approval" : "proposed", proposed_by: "carrier_ap", proposed_at: iso(now) });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "carrier_ap", kind: "propose_payment", reason_code: ctx.amount > 5_000 ? "P-003" : "OK", summary: `Payment proposed: ${money(ctx.amount)} to ${ctx.carrierName}`, policy_ids: ctx.amount > 5_000 ? ["P-003"] : ["P-002"] });
      if (ctx.amount <= 5_000) this.schedule(now + this.between(20, 90) * 1_000, () => this.postPayment(ctx, this.simNow));
    }
    if (t.agent === "controller" && ["completed"].includes(outcome) && ctx.scenario.kind === "receivable") {
      const amount = ctx.scenario.key === "shipper_short_pay" ? Math.round(ctx.amount * 0.82) : ctx.amount;
      this.upsert("ledger_entry", { id: `JE-${t.taskId}`, case_id: ctx.caseId, period: iso(now).slice(0, 7), lines: [{ account: "cash", debit: amount, credit: 0 }, { account: "accounts_receivable", debit: 0, credit: amount }], memo: `Deposit matched to ${ctx.invoiceId}`, requested_by: "shipper_ar", approved_by: "controller", posted_at: iso(now) });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "controller", kind: "post_journal_entry", reason_code: "P-006", summary: `Posted deposit ${money(amount)} against ${ctx.invoiceId}`, policy_ids: ["P-006", "P-005"] });
    }
    if (t.agent === "shipper_ar" && ctx.scenario.key === "customer_payment") {
      // Billing already happened; nothing else.
    }
    if (t.agent === "cfo") {
      const period = iso(now + 7 * 86_400_000).slice(0, 10);
      this.upsert("cash_forecast", { id: `CF-${t.taskId}`, case_id: ctx.caseId, period, category: ctx.scenario.kind === "receivable" ? "collections" : "carrier_payables", amount: ctx.scenario.kind === "receivable" ? ctx.amount : -ctx.amount, confidence: round2(this.between(0.6, 0.95)), assumptions_json: JSON.stringify({ basis: "task artifact", load: ctx.loadId }), updated_at: iso(now) });
    }
    if (t.agent === "validator") {
      this.upsert("review", { id: `REV-${t.taskId}`, target_task_id: ctx.rootTaskId ?? "", case_id: ctx.caseId, verdict: outcome, checks_json: JSON.stringify([{ name: "debits_equal_credits", ok: true, expected: "balanced", actual: "balanced" }, { name: "hold_matches_delta", ok: outcome === "pass" }]), friction_json: JSON.stringify(outcome === "pass" ? [] : [{ kind: "retry", detail: "rate con requested twice", cost_s: 42 }]), ts: iso(now) });
      this.audit({ case_id: ctx.caseId, task_id: t.taskId, agent: "validator", kind: "add_review", reason_code: outcome.toUpperCase(), summary: outcome === "pass" ? "Quality review passed" : "Quality review found friction; lesson written to agent memory" });
      if (outcome === "issue")
        this.upsert("agent_memory", { id: `MEM-${t.taskId}`, agent: ctx.scenario.root, scope: JSON.stringify({ carrier_id: ctx.carrierId, claim_type: ctx.scenario.key }), text: `When ${ctx.carrierName} bills accessorials, request the rate con and receipt in the first message.`, source: "validator", source_ref: t.taskId, version: 1, active: true, created_at: iso(now) });
    }
  }

  private postPayment(ctx: CaseCtx, now: number) {
    const inv = this.get("invoice", ctx.invoiceId);
    if (!inv || inv.status === "paid" || inv.status === "held") return;
    this.upsert("invoice", { id: ctx.invoiceId, status: "paid", paid_amount: ctx.amount });
    this.upsert("payment_proposal", { id: `PP-${ctx.rootTaskId}`, status: "sent" });
    this.upsert("ledger_entry", { id: `JE-${ctx.caseId}-exp`, case_id: ctx.caseId, period: iso(now).slice(0, 7), lines: [{ account: "freight_expense", debit: ctx.amount, credit: 0 }, { account: "accounts_payable", debit: 0, credit: ctx.amount }], memo: `Carrier settlement ${ctx.invoiceId}`, requested_by: "carrier_ap", approved_by: "controller", posted_at: iso(now) });
    this.upsert("ledger_entry", { id: `JE-${ctx.caseId}-pay`, case_id: ctx.caseId, period: iso(now).slice(0, 7), lines: [{ account: "accounts_payable", debit: ctx.amount, credit: 0 }, { account: "cash", debit: 0, credit: ctx.amount }], memo: `Payment released ${ctx.invoiceId}`, requested_by: "carrier_ap", approved_by: "controller", posted_at: iso(now + 1_000) });
    this.upsert("ledger_entry", { id: `JE-${ctx.caseId}-rev`, case_id: ctx.caseId, period: iso(now).slice(0, 7), lines: [{ account: "accounts_receivable", debit: Math.round(ctx.contract * 1.18), credit: 0 }, { account: "freight_revenue", debit: 0, credit: Math.round(ctx.contract * 1.18) }], memo: `Customer billing ${ctx.loadId}`, requested_by: "shipper_ar", approved_by: "controller", posted_at: iso(now + 2_000) });
    this.audit({ case_id: ctx.caseId, task_id: ctx.rootTaskId ?? "", agent: "controller", kind: "post_journal_entry", reason_code: "P-006", summary: `Posted settlement and billing journals for ${ctx.loadId} (${money(ctx.amount)})`, policy_ids: ["P-006", "P-005"] });
    // The shipper pays the billing a little later (most of the time), so cash
    // nets out positive instead of only ever flowing to carriers.
    if (this.chance(0.85)) {
      const revenue = Math.round(ctx.contract * 1.18);
      this.schedule(now + this.between(2, 15) * 60_000, () => {
        this.upsert("ledger_entry", { id: `JE-${ctx.caseId}-col`, case_id: ctx.caseId, period: iso(this.simNow).slice(0, 7), lines: [{ account: "cash", debit: revenue, credit: 0 }, { account: "accounts_receivable", debit: 0, credit: revenue }], memo: `Customer deposit matched ${ctx.loadId}`, requested_by: "shipper_ar", approved_by: "controller", posted_at: iso(this.simNow) });
      });
    }
  }

  /** Dynamic delegation: who does this agent need next? */
  private childrenFor(ctx: CaseCtx, t: TaskCtx): { to: AgentId; skill: string }[] {
    if (t.depth >= 3) return [];
    const k = ctx.scenario.key;
    const wanted: { to: AgentId; skill: string; p: number }[] = [];
    const add = (to: AgentId, skill: string, p = 1) => wanted.push({ to, skill, p });
    switch (t.agent) {
      case "carrier_ap":
        if (k === "detention_unsupported" || k === "lumper_no_receipt" || k === "rate_mismatch") {
          add("risk", "investigate_charge");
          add("controller", "review_hold");
        } else if (k === "missing_pod") {
          add("documents", "check_document_packet");
          add("controller", "review_hold", 0.5);
        } else if (k === "duplicate_invoice") add("controller", "check_already_paid");
        else if (k === "clean_invoice") {
          add("documents", "check_document_packet", 0.2);
          add("controller", "approve_entry", ctx.amount > 5_000 ? 1 : 0.35);
          add("verification", "verify_claim", 0.08);
        }
        break;
      case "risk":
        if (k === "bank_change") {
          add("verification", "verify_bank_change");
          add("controller", "review_hold");
        } else if (k === "detention_unsupported" || k === "lumper_no_receipt") add("documents", "collect_documents", 0.85);
        else if (k === "rate_mismatch") add("verification", "verify_claim", 0.4);
        break;
      case "documents":
        add("voice_desk", "place_call", k === "missing_pod" ? 0.7 : 0.5);
        break;
      case "verification":
        add("voice_desk", "place_call", k === "bank_change" ? 0.9 : 0.35);
        break;
      case "controller":
        add("cfo", "assess_financial_impact", k === "duplicate_invoice" ? 0.1 : 0.7);
        break;
      case "shipper_ar":
        if (k === "shipper_short_pay") {
          add("documents", "check_document_packet", 0.6);
          add("verification", "verify_dispute", 0.5);
          add("cfo", "assess_financial_impact", 0.6);
        } else if (k === "overdue_collections") {
          add("voice_desk", "place_call", 0.8);
          add("cfo", "cash_forecast", 0.5);
        } else add("controller", "reconcile");
        break;
      case "cfo":
        break;
    }
    const allowed = AGENT_META[t.agent].delegatesTo;
    const out: { to: AgentId; skill: string }[] = [];
    for (const w of wanted) {
      if (!allowed.includes(w.to)) continue;
      if (t.ancestors.includes(w.to) || w.to === t.agent) continue;
      if (!this.chance(w.p)) continue;
      out.push({ to: w.to, skill: w.skill });
      if (out.length >= 3) break;
    }
    return out;
  }

  /* -------- derived tables -------- */

  private refreshAgents(force: boolean) {
    if (!force && this.simNow - this.lastAgentRefresh < 1_000) return;
    this.lastAgentRefresh = this.simNow;
    const active: Record<string, number> = {};
    const queued: Record<string, number> = {};
    for (const r of this.tables.task) {
      const s = String(r.a2a_state);
      if (s === "working") active[String(r.agent)] = (active[String(r.agent)] ?? 0) + 1;
      else if (s === "submitted" || s === "input-required") queued[String(r.agent)] = (queued[String(r.agent)] ?? 0) + 1;
    }
    for (const id of AGENT_IDS) {
      const a = active[id] ?? 0;
      const prev = this.get("agent", id);
      const status = a > 0 ? "busy" : "online";
      if (prev && prev.status === status && prev.active === a && prev.queued === (queued[id] ?? 0) && prev.done === this.agentDone[id] && this.simNow - Date.parse(String(prev.last_seen)) < 15_000) continue;
      this.upsert("agent", {
        name: id,
        status,
        active: a,
        queued: queued[id] ?? 0,
        done: this.agentDone[id],
        errors: this.agentErrors[id],
        p50_ms: Math.round(this.between(1_800, 6_500)),
        card_url: `http://localhost:${AGENT_META[id].port}/.well-known/agent-card.json`,
        last_seen: iso(this.simNow),
      });
    }
  }

  private refreshMetrics(force: boolean) {
    if (!force && this.simNow - this.lastMetricRefresh < 5_000) return;
    this.lastMetricRefresh = this.simNow;
    const holds = this.tables.hold.filter((h) => h.status === "active").reduce((s, h) => s + Number(h.amount), 0);
    const caught = this.tables.hold.reduce((s, h) => s + Number(h.amount), 0) + this.tables.invoice.filter((i) => i.status === "rejected").reduce((s, i) => s + Number(i.amount), 0);
    const resolved = this.tables.case.filter((c) => c.status === "resolved" && c.resolved_at);
    const durations = resolved.map((c) => Date.parse(String(c.resolved_at)) - Date.parse(String(c.opened_at))).sort((a, b) => a - b);
    const median = durations.length ? durations[Math.floor(durations.length / 2)] / 1_000 : 0;
    const set = (name: string, value: number) => this.upsert("metric", { name, value: round2(value), updated_at: iso(this.simNow) });
    set("precision", 0.94 + this.rng() * 0.03);
    set("recall", 0.88 + this.rng() * 0.04);
    set("dollars_caught", caught);
    set("funds_on_hold", holds);
    set("false_holds", Math.round(this.tables.hold.length * 0.04));
    set("median_resolution_s", median);
    set("calls_placed", this.tables.call.length);
    set("automation_rate", 1 - this.tables.approval.length / Math.max(1, this.tables.case.length));
  }
}
