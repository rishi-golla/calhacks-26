import { createHash } from "node:crypto";
import type { Row, Snapshot, TableName } from "./data";

const TABLE_FIELDS = {
  invoice: [
    "id",
    "case_id",
    "carrier_id",
    "load_id",
    "amount",
    "contract_amount",
    "paid_amount",
    "issued_at",
    "status",
  ],
  hold: [
    "id",
    "case_id",
    "invoice_id",
    "amount",
    "reason_code",
    "status",
    "placed_by",
    "released_by",
    "released_amount",
    "placed_at",
    "released_at",
  ],
  approval: [
    "id",
    "case_id",
    "task_id",
    "action",
    "target_id",
    "amount",
    "requested_by",
    "approver_name",
    "decision",
    "decided_at",
    "expires_at",
    "invoice_id",
  ],
  payment_proposal: [
    "id",
    "case_id",
    "carrier_id",
    "invoice_id",
    "load_id",
    "amount",
    "status",
    "proposed_by",
    "proposed_at",
    "approval_id",
  ],
  ledger_entry: [
    "id",
    "case_id",
    "period",
    "lines",
    "memo",
    "requested_by",
    "approved_by",
    "posted_at",
    "request_id",
  ],
  audit_event: [
    "id",
    "ts",
    "case_id",
    "task_id",
    "agent",
    "kind",
    "reason_code",
    "summary",
    "evidence_refs",
    "policy_ids",
    "model_used",
    "change_set_id",
  ],
  change_set: [
    "id",
    "case_id",
    "task_id",
    "agent",
    "action",
    "summary",
    "policy_ids",
    "instr_version",
    "ts",
    "sequence",
  ],
  call: [
    "id",
    "case_id",
    "task_id",
    "persona",
    "direction",
    "intended_to",
    "routed_to",
    "company_id",
    "contact_id",
    "status",
    "started_at",
    "ended_at",
    "findings_json",
    "transcript_redacted",
    "disclosure_given",
  ],
  doc_read: [
    "id",
    "change_set_id",
    "task_id",
    "agent",
    "doc_ref",
    "page",
    "span",
    "extracted_json",
  ],
  review: [
    "id",
    "target_task_id",
    "case_id",
    "verdict",
    "checks_json",
    "friction_json",
    "ts",
    "actions_json",
  ],
  flag: [
    "id",
    "target_ref",
    "case_id",
    "kind",
    "note",
    "raised_by",
    "status",
    "resolution_ref",
    "ts",
  ],
  cash_forecast: [
    "id",
    "case_id",
    "period",
    "category",
    "amount",
    "confidence",
    "assumptions_json",
    "updated_at",
  ],
  instruction_version: [
    "id",
    "agent",
    "version",
    "reason",
    "author",
    "source_ref",
    "created_at",
    "active",
    "diff",
  ],
  agent_memory: [
    "id",
    "agent",
    "scope",
    "text",
    "source",
    "source_ref",
    "version",
    "active",
    "created_at",
  ],
  company: ["id", "kind", "name", "usdot", "mc", "phone", "website"],
  contact: [
    "id",
    "company_id",
    "name",
    "title",
    "phone",
    "email",
    "linkedin_url",
    "source",
    "confidence",
  ],
  case: [
    "case_id",
    "load_id",
    "opened_at",
    "status",
    "priority",
    "title",
    "exposure_usd",
    "resolved_at",
  ],
  task: [
    "task_id",
    "case_id",
    "parent_task_id",
    "agent",
    "skill",
    "display_id",
    "a2a_state",
    "business_status",
    "outcome",
    "confidence",
    "model_used",
    "started_at",
    "updated_at",
    "evidence_refs",
    "policy_ids",
    "instr_version",
    "depth",
  ],
} satisfies Partial<Record<TableName, string[]>>;

const PRIMARY_KEYS: Partial<Record<TableName, string>> = {
  case: "case_id",
  task: "task_id",
};
const SECRET_KEY = /(secret|token|password|credential|api[_-]?key)/i;
const TITLE_FIELDS: Partial<Record<TableName, string[]>> = {
  invoice: ["id"],
  hold: ["id"],
  approval: ["action", "id"],
  payment_proposal: ["id"],
  ledger_entry: ["memo", "id"],
  audit_event: ["summary", "kind"],
  change_set: ["summary", "action"],
  call: ["persona", "status", "id"],
  doc_read: ["doc_ref", "id"],
  review: ["verdict", "id"],
  flag: ["kind", "id"],
  cash_forecast: ["period", "category"],
  instruction_version: ["agent", "version"],
  agent_memory: ["agent", "scope"],
  company: ["name", "id"],
  contact: ["name", "id"],
  case: ["title", "case_id"],
  task: ["skill", "agent", "task_id"],
};

export type SearchChunk = {
  ref: string;
  table: keyof typeof TABLE_FIELDS;
  id: string;
  case_id: string | null;
  title: string;
  text: string;
};

export type RankedChunk = { chunk: SearchChunk; score: number };

function safeString(value: unknown): string | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return JSON.stringify(value);
}

function redactText(value: string): string {
  return value
    .replace(
      /\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._~+/-]+)\b/g,
      "[REDACTED]",
    )
    .replace(
      /\b([\w.-]*(?:secret|token|password|credential|api[_-]?key)[\w.-]*\s*[:=]\s*)[^,\s;]+/gi,
      "$1[REDACTED]",
    );
}

function sanitize(value: unknown, depth = 0): unknown {
  if (depth > 6) return "[truncated]";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        return sanitize(JSON.parse(trimmed), depth + 1);
      } catch {
        return redactText(value).slice(0, 2500);
      }
    }
    return redactText(value).slice(0, 2500);
  }
  if (Array.isArray(value))
    return value.slice(0, 100).map((item) => sanitize(item, depth + 1));
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !SECRET_KEY.test(key))
        .map(([key, item]) => [key, sanitize(item, depth + 1)]),
    );
  return value;
}

function chunkValue(value: unknown): string | undefined {
  const safe = sanitize(value);
  if (safe === undefined || safe === null || safe === "") return undefined;
  const rendered =
    typeof safe === "string"
      ? safe
      : typeof safe === "number" || typeof safe === "boolean"
        ? String(safe)
        : JSON.stringify(safe);
  return rendered.slice(0, 2500);
}

function rowId(table: keyof typeof TABLE_FIELDS, row: Row): string {
  const primary = PRIMARY_KEYS[table] ?? "id";
  const value = chunkValue(row[primary]);
  return value || chunkValue(row.task_id) || chunkValue(row.name) || "unknown";
}

function buildCompanyCaseMap(snapshot: Snapshot): Map<string, string | null> {
  const related = new Map<string, Set<string>>();
  const attach = (companyId: unknown, caseId: unknown) => {
    const company = safeString(companyId);
    const caseValue = safeString(caseId);
    if (!company || !caseValue) return;
    const cases = related.get(company) ?? new Set<string>();
    cases.add(caseValue);
    related.set(company, cases);
  };
  for (const row of snapshot.invoice) attach(row.carrier_id, row.case_id);
  for (const row of snapshot.payment_proposal)
    attach(row.carrier_id, row.case_id);
  for (const row of snapshot.call) attach(row.company_id, row.case_id);
  return new Map(
    [...related].map(
      ([company, cases]) =>
        [company, cases.size === 1 ? [...cases][0] : null] as const,
    ),
  );
}

export function buildSearchChunks(snapshot: Snapshot): SearchChunk[] {
  const taskCases = new Map<string, string>();
  for (const row of snapshot.task) {
    const taskId = safeString(row.task_id);
    const caseId = safeString(row.case_id);
    if (taskId && caseId) taskCases.set(taskId, caseId);
  }
  const companyCases = buildCompanyCaseMap(snapshot);
  const chunks: SearchChunk[] = [];
  for (const [table, fields] of Object.entries(TABLE_FIELDS) as [
    keyof typeof TABLE_FIELDS,
    string[],
  ][]) {
    const rows = snapshot[table] as Row[];
    for (const row of rows) {
      const id = rowId(table, row);
      const title =
        TITLE_FIELDS[table]
          ?.map((field) => chunkValue(row[field]))
          .find(Boolean) ?? id;
      const taskId = safeString(row.task_id);
      const companyId =
        table === "contact"
          ? safeString(row.company_id)
          : table === "company"
            ? id
            : undefined;
      const caseId =
        safeString(row.case_id) ??
        (taskId ? taskCases.get(taskId) : undefined) ??
        (companyId ? (companyCases.get(companyId) ?? undefined) : undefined) ??
        null;
      const lines = [`table: ${table}`, `id: ${id}`];
      if (caseId) lines.push(`case_id: ${caseId}`);
      for (const field of fields) {
        if (field === "id" || field === "task_id" || field === "case_id")
          continue;
        if (SECRET_KEY.test(field)) continue;
        const value = chunkValue(row[field]);
        if (value) lines.push(`${field}: ${value}`);
      }
      const text = lines.join("\n").slice(0, 8000);
      chunks.push({
        ref: `${table}:${id}`,
        table,
        id,
        case_id: caseId,
        title,
        text,
      });
    }
  }
  return chunks;
}

export function tokenize(text: string): string[] {
  return (text.toLowerCase().match(/[a-z0-9]+/g) ?? []).filter(
    (token) => token.length > 1,
  );
}

export function rankBm25(chunks: SearchChunk[], query: string): RankedChunk[] {
  const terms = tokenize(query);
  if (!terms.length || !chunks.length) return [];
  const documents = chunks.map((chunk) => {
    const tokens = tokenize(chunk.text);
    const frequencies = new Map<string, number>();
    for (const token of tokens)
      frequencies.set(token, (frequencies.get(token) ?? 0) + 1);
    return { tokens, frequencies };
  });
  const documentFrequency = new Map<string, number>();
  for (const document of documents)
    for (const term of new Set(document.tokens))
      documentFrequency.set(term, (documentFrequency.get(term) ?? 0) + 1);
  const averageLength =
    documents.reduce((total, document) => total + document.tokens.length, 0) /
    documents.length;
  const k1 = 1.2;
  const b = 0.75;
  return chunks
    .map((chunk, index) => {
      const document = documents[index];
      const length = document.tokens.length;
      let score = 0;
      for (const term of terms) {
        const frequency = document.frequencies.get(term) ?? 0;
        if (!frequency) continue;
        const df = documentFrequency.get(term) ?? 0;
        const idf = Math.log(1 + (chunks.length - df + 0.5) / (df + 0.5));
        score +=
          idf *
          ((frequency * (k1 + 1)) /
            (frequency + k1 * (1 - b + b * (length / averageLength))));
      }
      return { chunk, score };
    })
    .filter((result) => result.score > 0)
    .sort(
      (left, right) =>
        right.score - left.score ||
        left.chunk.ref.localeCompare(right.chunk.ref),
    );
}

export function cosineSimilarity(left: number[], right: number[]): number {
  if (left.length !== right.length || !left.length) return 0;
  let dot = 0;
  let leftMagnitude = 0;
  let rightMagnitude = 0;
  for (let index = 0; index < left.length; index += 1) {
    dot += left[index] * right[index];
    leftMagnitude += left[index] * left[index];
    rightMagnitude += right[index] * right[index];
  }
  if (!leftMagnitude || !rightMagnitude) return 0;
  return dot / Math.sqrt(leftMagnitude * rightMagnitude);
}

export function rankSemantic(
  chunks: SearchChunk[],
  queryVector: number[],
  vectors: Map<string, number[]>,
): RankedChunk[] {
  return chunks
    .map((chunk) => ({
      chunk,
      score: cosineSimilarity(
        queryVector,
        vectors.get(chunkHash(chunk.text)) ?? [],
      ),
    }))
    .sort(
      (left, right) =>
        right.score - left.score ||
        left.chunk.ref.localeCompare(right.chunk.ref),
    );
}

export function reciprocalRankFuse(
  keyword: RankedChunk[],
  semantic: RankedChunk[],
  caseId?: string,
  limit = 12,
): RankedChunk[] {
  const fused = new Map<string, RankedChunk>();
  const add = (ranking: RankedChunk[]) => {
    ranking.forEach(({ chunk }, index) => {
      const current = fused.get(chunk.ref) ?? { chunk, score: 0 };
      current.score += 1 / (60 + index + 1);
      fused.set(chunk.ref, current);
    });
  };
  add(keyword);
  add(semantic);
  return [...fused.values()]
    .map((result) => ({
      ...result,
      score:
        result.score * (caseId && result.chunk.case_id === caseId ? 1.25 : 1),
    }))
    .sort(
      (left, right) =>
        right.score - left.score ||
        left.chunk.ref.localeCompare(right.chunk.ref),
    )
    .slice(0, limit);
}

export function chunkHash(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}
