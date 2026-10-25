/**
 * What an agent learned from Apify, read from the tool call it already published.
 *
 * Agents reach Apify through two tools (`integrations/apify.py`): an FMCSA
 * carrier-authority check and a scrape of the company's public website. Each
 * call is an `audit_event` of kind `tool_call` whose result names the provider,
 * the Apify run, and the public record it returned; its `evidence_refs` carry
 * `apify://runs/<id>` even when a large result was trimmed from the summary.
 */
import { str } from "@/lib/data";

export const APIFY_TOOLS: Record<string, string> = {
  lookup_carrier_authority: "FMCSA carrier check",
  inspect_company_website: "Website contact scrape",
};

const RUN_REF = /^apify:\/\/runs\/([A-Za-z0-9]{5,40})$/;

/** Run id of an `apify://runs/<id>` evidence ref, or null for anything else. */
export function apifyRunId(ref: unknown): string | null {
  return typeof ref === "string" ? (RUN_REF.exec(ref.trim())?.[1] ?? null) : null;
}

/** Apify Console page for a run (opens for members of the team's Apify account). */
export const apifyRunUrl = (runId: string) => `https://console.apify.com/view/runs/${encodeURIComponent(runId)}`;

export type ApifyOutcome = "running" | "confirmed" | "mismatch" | "not_found" | "unavailable" | "recorded";

export const APIFY_OUTCOME: Record<ApifyOutcome, { label: string; tone: "ok" | "bad" | "warn" | "" }> = {
  running: { label: "Calling Apify…", tone: "" },
  confirmed: { label: "Confirmed", tone: "ok" },
  mismatch: { label: "Different company", tone: "bad" },
  not_found: { label: "No matching registration", tone: "warn" },
  unavailable: { label: "Check unavailable", tone: "warn" },
  recorded: { label: "Run recorded", tone: "" },
};

export type ApifyCarrier = {
  legalName: string;
  dbaName: string;
  usdot: string;
  dockets: string[];
  authority: string;
  authorityActive: boolean | null;
  outOfService: boolean | null;
  nameMatch: boolean | null;
  sourceUrl: string;
};

export type ApifyEvidence = {
  tool: string;
  check: string;
  outcome: ApifyOutcome;
  runId: string | null;
  cached: boolean;
  /** Short reason when the check could not answer (`no_matching_source_records`, `apify_not_configured`, …). */
  error: string;
  carriers: ApifyCarrier[];
  /** Names the case expected and the registry returned, when they differ. */
  expectedNames: string[];
  registeredNames: string[];
  /** Website scrape: public contact candidates found (counts only; values stay unverified). */
  contacts: { url: string; emails: number; phones: number }[];
  sourceUrl: string;
};

type CallLike = { name: string; status?: string; result?: unknown; evidenceRefs?: readonly string[] };

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const text = (v: unknown) => str(v, "").trim();
const flag = (v: unknown) => (typeof v === "boolean" ? v : null);
const strings = (v: unknown) => list(v).map(text).filter(Boolean);

/**
 * True when this tool call itself went to Apify. An `apify://runs/` evidence ref is not enough: a task's
 * result and a reviewer's inspection carry the case's Apify refs too, without calling Apify.
 */
export function isApifyCall(call: CallLike): boolean {
  return call.name in APIFY_TOOLS || obj(call.result).provider === "apify";
}

/** The Apify finding a tool call carries, or null when the call did not go to Apify. */
export function apifyEvidence(call: CallLike): ApifyEvidence | null {
  if (!isApifyCall(call)) return null;
  const result = obj(call.result);
  const runId = text(result.run_id) || (call.evidenceRefs ?? []).map(apifyRunId).find((id) => id) || null;
  const carriers: ApifyCarrier[] = list(result.records)
    .map(obj)
    .filter((r) => text(r.usdot) || text(r.legal_name))
    .map((r) => ({
      legalName: text(r.legal_name),
      dbaName: text(r.dba_name),
      usdot: text(r.usdot),
      dockets: strings(r.docket_numbers),
      authority: text(r.authority_status),
      authorityActive: flag(r.authority_confirmed_active),
      outOfService: flag(r.out_of_service),
      nameMatch: flag(r.name_match),
      sourceUrl: text(r.source_url),
    }));
  const contacts = call.name === "inspect_company_website"
    ? list(result.records).map(obj).filter((r) => text(r.url)).map((r) => ({
        url: text(r.url),
        emails: list(r.emails).length,
        phones: list(r.phones).length,
      }))
    : [];
  const mismatch = obj(result.identity_mismatch);
  const settled = ["completed", "failed", "blocked"].includes(call.status ?? "completed");
  const hasResult = Object.keys(result).length > 0 && !("summary" in result && Object.keys(result).length === 1);
  const outcome: ApifyOutcome = !settled
    ? "running"
    : !hasResult
      ? "recorded"
      : result.available === true
        ? result.identity_match === false
          ? "mismatch"
          : "confirmed"
        : text(result.error) === "no_matching_source_records"
          ? "not_found"
          : "unavailable";
  return {
    tool: call.name,
    check: APIFY_TOOLS[call.name] ?? "Apify lookup",
    outcome,
    runId,
    cached: result.cached === true,
    error: text(result.error),
    carriers,
    expectedNames: strings(mismatch.expected_names),
    registeredNames: strings(mismatch.registered_names),
    contacts,
    sourceUrl: text(result.source_url),
  };
}

/** One-line description of the finding, e.g. "TN LOGISTICS INC · USDOT 3141592 · MC99574 · not authorized". */
export function apifyHeadline(e: ApifyEvidence): string {
  const c = e.carriers[0];
  if (c) {
    const name = c.dbaName && c.dbaName !== c.legalName ? `${c.legalName} (${c.dbaName})` : c.legalName;
    return [name, c.usdot && `USDOT ${c.usdot}`, c.dockets[0], c.authority].filter(Boolean).join(" · ");
  }
  if (e.contacts.length) {
    const emails = e.contacts.reduce((n, x) => n + x.emails, 0);
    const phones = e.contacts.reduce((n, x) => n + x.phones, 0);
    return `${e.contacts[0].url.replace(/^https:\/\//, "").replace(/\/$/, "")} · ${emails} email${emails === 1 ? "" : "s"}, ${phones} phone${phones === 1 ? "" : "s"}`;
  }
  return e.error ? e.error.replaceAll("_", " ") : APIFY_OUTCOME[e.outcome].label;
}

const TOOL_NAME_IN_SUMMARY = new RegExp(`"tool_name":\\s*"(${Object.keys(APIFY_TOOLS).join("|")})"`);

/** Cases with at least one Apify tool call, scanning raw audit rows cheaply (no JSON parse per row). */
export function apifyCaseIds(audit: readonly Record<string, unknown>[]): Set<string> {
  const ids = new Set<string>();
  for (const row of audit) {
    if (row.kind !== "tool_call") continue;
    const caseId = text(row.case_id);
    if (caseId && !ids.has(caseId) && TOOL_NAME_IN_SUMMARY.test(text(row.summary))) ids.add(caseId);
  }
  return ids;
}
