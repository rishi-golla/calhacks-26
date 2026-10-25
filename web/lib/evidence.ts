/**
 * Evidence references: what an agent cites for a number, and how the
 * dashboard opens the cited source.
 *
 *   doc:<REF>      PDF at <DOCS_DIR>/<REF>.pdf, ground truth at <DOCS_DIR>/<REF>.json
 *   mail:<MAIL-ID> RFC 822 message at <MAIL_DIR>/<MAIL-ID>.eml
 *   anything else  (fixture:, call://, audit://, source:, ...) has no preview
 *
 * Pure helpers only (no fs, no node:path) so the browser bundle can use them;
 * app/api/evidence/[ref]/route.ts does the file work on the server.
 */

export type EvidenceKind = "doc" | "mail" | "other";
export type EvidenceView = "pdf" | "json" | "eml";

export type ParsedEvidence = {
  kind: EvidenceKind;
  /** File stem for doc/mail refs; the whole ref for anything else. */
  id: string;
  /** True when the dashboard can try to open the source (the file may still be missing on this host). */
  previewable: boolean;
  /** Page from an optional `#p<N>` suffix (doc refs only). */
  page?: number;
};

/** REF and MAIL-ID: one safe file stem, never a path. */
export const EVIDENCE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;

/** A file stem that is safe to join onto a directory: matches EVIDENCE_ID, no `..`, no separators. */
export function isSafeEvidenceId(id: string): boolean {
  return EVIDENCE_ID.test(id) && !id.includes("..") && !id.includes("/") && !id.includes("\\");
}

export function parseEvidenceRef(ref: unknown): ParsedEvidence {
  const raw = typeof ref === "string" ? ref.trim() : "";
  const other: ParsedEvidence = { kind: "other", id: raw, previewable: false };
  const match = /^(doc|mail):(.*)$/.exec(raw);
  if (!match) return other;
  const kind = match[1] as "doc" | "mail";
  let id = match[2];
  let page: number | undefined;
  if (kind === "doc") {
    const anchor = /^(.*)#p(\d{1,4})$/.exec(id);
    if (anchor) {
      id = anchor[1];
      page = Number(anchor[2]) || undefined;
    }
  }
  if (!isSafeEvidenceId(id)) return other;
  return page ? { kind, id, previewable: true, page } : { kind, id, previewable: true };
}

/** Same-origin URL for one view of a previewable ref. */
export function evidenceUrl(ref: string, view: EvidenceView = "pdf"): string {
  const parsed = parseEvidenceRef(ref);
  const key = parsed.previewable ? `${parsed.kind}:${parsed.id}` : ref;
  return `/api/evidence/${encodeURIComponent(key)}?view=${view}`;
}

export type EvidenceFile = {
  /** Which configured directory the file lives in. */
  dir: "docs" | "mail";
  /** File name inside that directory (stem + extension). */
  file: string;
  contentType: string;
  /** Content-Disposition value. */
  disposition: string;
};

export type EvidenceResolution = { ok: true; file: EvidenceFile } | { ok: false; status: 400 | 404; error: string };

/**
 * Validate a requested ref and view and name the file that would answer it.
 * 400: malformed ref or view. 404: a well-formed ref with no previewable source.
 * Error strings never contain paths.
 */
export function resolveEvidenceRequest(ref: string, view: string | null): EvidenceResolution {
  const bad = (error: string) => ({ ok: false as const, status: 400 as const, error });
  if (typeof ref !== "string" || !ref || ref.length > 200 || [...ref].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127))
    return bad("invalid evidence reference");
  if (ref.includes("/") || ref.includes("\\") || ref.includes("..")) return bad("invalid evidence reference");
  const match = /^(doc|mail):(.*)$/.exec(ref);
  if (!match) return { ok: false, status: 404, error: "no preview for this source" };
  const [, kind, id] = match;
  if (!isSafeEvidenceId(id)) return bad("invalid evidence reference");
  if (kind === "doc") {
    const v = view || "pdf";
    if (v === "pdf")
      return { ok: true, file: { dir: "docs", file: `${id}.pdf`, contentType: "application/pdf", disposition: `inline; filename="${id}.pdf"` } };
    if (v === "json")
      return { ok: true, file: { dir: "docs", file: `${id}.json`, contentType: "application/json; charset=utf-8", disposition: `inline; filename="${id}.json"` } };
    return bad("invalid view");
  }
  const v = view || "eml";
  if (v !== "eml" && v !== "text") return bad("invalid view");
  return { ok: true, file: { dir: "mail", file: `${id}.eml`, contentType: "text/plain; charset=utf-8", disposition: `inline; filename="${id}.eml"` } };
}

/**
 * True when `candidate` is `root` or inside it. Both must already be absolute
 * and normalized (path.resolve / realpath); `sep` is the platform separator.
 * Guards against prefix tricks such as /data/docs-evil next to /data/docs.
 */
export function isWithinDir(root: string, candidate: string, sep = "/"): boolean {
  if (!root || !candidate) return false;
  const base = root.endsWith(sep) ? root : root + sep;
  return candidate === root || candidate.startsWith(base);
}

/**
 * The evidence route answers only the dashboard's own pages. Browsers send `Sec-Fetch-Site`
 * on every fetch, iframe and link navigation; `same-origin` is the dashboard, `none` is the
 * address bar (a person opening a PDF they were shown). Anything else, or a client that sends
 * no header at all (scripts, curl), is refused. This is hotlink and scripted-scrape protection,
 * not authentication: the dashboard itself has no login, so deploy it on a trusted network.
 */
export function isAllowedFetchSite(secFetchSite: string | null): boolean {
  return secFetchSite === "same-origin" || secFetchSite === "none";
}

/** Human label for the kind of a ground-truth document (`rate_confirmation` -> "Rate confirmation"). */
export function documentKindLabel(type: unknown, parsed: ParsedEvidence): string {
  if (typeof type === "string" && type.trim()) {
    const t = type.trim().replaceAll("_", " ");
    return t.charAt(0).toUpperCase() + t.slice(1);
  }
  return parsed.kind === "mail" ? "Email (RFC 822)" : parsed.kind === "doc" ? "Document" : "Source";
}

/** Ground-truth fields worth listing: skip the bookkeeping keys. */
export function extractedFields(record: unknown): [string, unknown][] {
  if (!record || typeof record !== "object" || Array.isArray(record)) return [];
  return Object.entries(record as Record<string, unknown>).filter(([key]) => !["type", "ref", "pdf"].includes(key));
}

/** One-line rendering of a nested ground-truth value: `{name: Test Carrier, mc: 123}`. */
export function compactValue(value: unknown, depth = 0): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString("en-US", { maximumFractionDigits: 4 }) : "—";
  if (typeof value === "string") return value;
  if (depth > 3) return "…";
  if (Array.isArray(value)) return value.length ? `[${value.map((v) => compactValue(v, depth + 1)).join(", ")}]` : "[]";
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== null && v !== undefined && v !== "");
    return entries.length ? `{${entries.map(([k, v]) => `${k}: ${compactValue(v, depth + 1)}`).join(", ")}}` : "{}";
  }
  return String(value);
}
