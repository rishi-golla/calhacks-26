/**
 * Case file: what a case is made of, read from its `source_record` row
 * (`case:<CASE_ID>`, the record agents read in place of source.json).
 *
 * Pure: `buildCaseFile` turns the row's `record_json` into the panel model,
 * documents, emails, bank lines and the facts worth comparing. The record is
 * ingest's export, so every field is optional and unknown ones are left to the
 * raw view; nothing here throws on a malformed record.
 */
import { json, type Row } from "@/lib/data";
import { documentKindLabel, parseEvidenceRef } from "@/lib/evidence";

export type CaseFileDoc = {
  ref: string;
  /** Document kind as the record names it (`rate_confirmation`), or inferred from the ref. */
  kind: string;
  label: string;
  /** PODs only: true signed, false unsigned, undefined not stated. */
  signed?: boolean;
  /** Shipper invoice only: what the shipper was billed. */
  amount?: number;
};

export type CaseFileBankLine = {
  id: string;
  amount: number | null;
  postedAt: string;
  /** The invoice the deposit was applied to, or the candidate for an unapplied one. */
  invoiceId: string;
  shipperId: string;
  /** Sitting unapplied: the remittance did not name an invoice the brokerage holds. */
  unapplied: boolean;
  /** Remittance reference, when it differs from the invoice. */
  remittanceRef: string;
};

export type CaseFile = {
  recordId: string;
  updatedAt: string;
  documents: CaseFileDoc[];
  /** Required document kinds the record has no document for. */
  missing: { kind: string; label: string }[];
  emails: string[];
  /** Cited sources the viewer cannot open (fixture:, call://, ...). */
  otherRefs: string[];
  bank: CaseFileBankLine[];
  billed: number | null;
  /** Deposits applied to this case's invoice from this case's shipper; `deposited` is their sum. */
  applied: CaseFileBankLine[];
  deposited: number;
  carrier: { name: string; id: string; usdot: string; mc: string } | null;
  /** The carrier the invoice names, when it is not the tendered one. */
  invoiceCarrier: { name: string; usdot: string } | null;
  shipper: string;
  bol: string;
  loadId: string;
  lane: string;
  rate: { contract: number | null; invoice: number | null; delta: number | null } | null;
  detention: { claimedHours: number | null; dwellMin: number | null; freeTimeMin: number | null; billableHours: number | null; overClaimed: boolean } | null;
  claimType: string;
  deductionReason: string;
  bankChange: { payee: string; accountLast4: string; via: string; urgency: string; senderMatches: boolean | null } | null;
  flags: string[];
  /** The parsed record, for the raw view. */
  record: Record<string, unknown>;
};

export type CaseFileResult = { state: "none" } | { state: "invalid"; recordId: string } | { state: "ready"; file: CaseFile };

const KIND_LABEL: Record<string, string> = {
  rate_confirmation: "Rate confirmation",
  bill_of_lading: "Bill of lading",
  pod: "Proof of delivery",
  carrier_invoice: "Carrier invoice",
  shipper_invoice: "Shipper invoice",
  detention_log: "Detention log",
  lumper_receipt: "Lumper receipt",
  receipt: "Receipt",
};

/** Ref prefixes data/docs uses, for a cited document the record does not list. */
const PREFIX_KIND: Record<string, string> = {
  RC: "rate_confirmation",
  BOL: "bill_of_lading",
  POD: "pod",
  INV: "carrier_invoice",
  SINV: "shipper_invoice",
  DET: "detention_log",
};

/** Display order: the paperwork of a load in the order it is produced. */
const KIND_ORDER = ["rate_confirmation", "bill_of_lading", "pod", "detention_log", "lumper_receipt", "receipt", "carrier_invoice", "shipper_invoice"];

export function docKindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? documentKindLabel(kind, { kind: "doc", id: "", previewable: true });
}

const text = (value: unknown): string => (typeof value === "string" ? value.trim() : typeof value === "number" && Number.isFinite(value) ? String(value) : "");

function amount(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
}

const objects = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value) ? value.filter((v): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v)) : [];

const object = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;

/** The non-empty strings of a list; refs and kinds are never numbers. */
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((v): v is string => typeof v === "string").map((v) => v.trim()).filter(Boolean) : [];

function inferKind(ref: string): string {
  const { id } = parseEvidenceRef(ref);
  const prefix = /^([A-Z]+)-/.exec(id)?.[1] ?? "";
  return PREFIX_KIND[prefix] ?? "document";
}

const round2 = (n: number) => Math.round(n * 100) / 100;

/** The case's row among `rows` (a `source_record` query for one case), parsed into the panel model. */
export function buildCaseFile(rows: readonly Row[], caseId: string): CaseFileResult {
  const row = rows.find((r) => r.id === `case:${caseId}`) ?? rows.find((r) => r.kind === "case" && r.case_id === caseId);
  if (!row) return { state: "none" };
  const recordId = text(row.id);
  const record = object(json<unknown>(row.record_json, null));
  if (!record) return { state: "invalid", recordId };

  /* -------- documents -------- */
  const documents: CaseFileDoc[] = [];
  const seen = new Map<string, CaseFileDoc>();
  const addDoc = (ref: string, kind: string, extra: Partial<CaseFileDoc> = {}) => {
    const parsed = parseEvidenceRef(ref);
    if (parsed.kind !== "doc") return;
    // `doc:X#p2` and `doc:X` are one document.
    const key = parsed.previewable ? `doc:${parsed.id}` : ref;
    const existing = seen.get(key);
    if (existing) {
      // Already listed: keep it, but take what this mention adds (the shipper
      // invoice's amount, a kind the ref prefix could not tell).
      if (existing.kind === "document" && kind !== "document") Object.assign(existing, { kind, label: docKindLabel(kind) });
      if (existing.signed === undefined && extra.signed !== undefined) existing.signed = extra.signed;
      if (existing.amount === undefined && extra.amount !== undefined) existing.amount = extra.amount;
      return;
    }
    const doc: CaseFileDoc = { ref, kind, label: docKindLabel(kind), ...extra };
    seen.set(key, doc);
    documents.push(doc);
  };
  for (const d of objects(record.documents)) {
    const ref = text(d.ref);
    if (!ref) continue;
    const kind = text(d.kind) || inferKind(ref);
    addDoc(ref, kind, typeof d.signed === "boolean" ? { signed: d.signed } : {});
  }
  // A cited document the list leaves out still belongs to the file.
  for (const ref of [...strings(record.evidence_refs), ...strings(record.resubmission_refs)]) addDoc(ref, inferKind(ref));
  const shipperInvoiceId = text(record.shipper_invoice_id);
  const billed = amount(record.shipper_invoice_amount);
  if (shipperInvoiceId) addDoc(`doc:${shipperInvoiceId}`, "shipper_invoice", billed !== null ? { amount: billed } : {});
  // A POD's signature can live on the record rather than the document entry.
  const podSigned = typeof record.pod_signed === "boolean" ? record.pod_signed : undefined;
  for (const d of documents) if (d.kind === "pod" && d.signed === undefined && podSigned !== undefined) d.signed = podSigned;
  const rank = (kind: string) => {
    const i = KIND_ORDER.indexOf(kind);
    return i < 0 ? KIND_ORDER.length : i;
  };
  documents.sort((a, b) => rank(a.kind) - rank(b.kind));
  const have = new Set(documents.map((d) => d.kind));
  const missing = [...new Set(strings(record.required_documents))].filter((k) => !have.has(k)).map((kind) => ({ kind, label: docKindLabel(kind) }));

  /* -------- emails and other sources -------- */
  const bankChange = object(record.bank_change);
  const refs = [...strings(record.evidence_refs), ...strings(record.resubmission_refs)];
  const sourceEmail = text(bankChange?.source_email);
  if (sourceEmail) refs.push(sourceEmail.startsWith("mail:") ? sourceEmail : `mail:${sourceEmail}`);
  const emails = [...new Set(refs.filter((r) => parseEvidenceRef(r).kind === "mail"))];
  const otherRefs = [...new Set(refs.filter((r) => parseEvidenceRef(r).kind === "other"))];

  /* -------- bank lines -------- */
  const bank: CaseFileBankLine[] = [];
  for (const d of objects(record.deposits))
    bank.push({ id: text(d.id), amount: amount(d.amount), postedAt: text(d.posted_at), invoiceId: text(d.invoice_id), shipperId: text(d.shipper_id), unapplied: false, remittanceRef: "" });
  for (const d of objects(record.unapplied_deposits)) {
    const invoiceId = text(d.candidate_invoice_id) || text(d.invoice_id);
    const remittance = text(d.remittance_ref);
    bank.push({ id: text(d.id), amount: amount(d.amount), postedAt: text(d.posted_at), invoiceId, shipperId: text(d.shipper_id), unapplied: true, remittanceRef: remittance !== invoiceId ? remittance : "" });
  }
  // Received: only cash applied to this case's invoice from this case's shipper,
  // the same match Shipper AR makes before it computes the balance.
  const shipperId = text(record.shipper_id) || text(object(record.shipper)?.id);
  const applied = bank.filter((b) => !b.unapplied && !!shipperInvoiceId && b.invoiceId === shipperInvoiceId && b.shipperId === shipperId);
  const deposited = round2(applied.reduce((sum, b) => sum + (b.amount ?? 0), 0));

  /* -------- facts -------- */
  const c = object(record.carrier);
  const carrier = c || text(record.carrier_id) ? { name: text(c?.name), id: text(c?.id) || text(record.carrier_id), usdot: text(c?.usdot), mc: text(c?.mc) } : null;
  const identity = object(record.carrier_identity);
  const invoiceUsdot = text(identity?.invoice_usdot) || text(record.invoice_carrier_usdot);
  const invoiceCarrierName = text(identity?.invoice_carrier_name);
  const invoiceCarrier =
    (invoiceUsdot && carrier?.usdot && invoiceUsdot !== carrier.usdot) || (invoiceCarrierName && carrier?.name && invoiceCarrierName !== carrier.name)
      ? { name: invoiceCarrierName, usdot: invoiceUsdot }
      : null;

  const contract = amount(record.contract_rate);
  const invoice = amount(record.invoice_amount);
  const rate = contract !== null || invoice !== null ? { contract, invoice, delta: contract !== null && invoice !== null ? round2(invoice - contract) : null } : null;

  const gps = object(record.gps);
  const claimedHours = amount(record.detention_hours_claimed);
  const dwellMin = amount(record.gps_dwell_min) ?? amount(gps?.delivery_dwell_min);
  const freeTimeMin = amount(record.free_time_min) ?? amount(gps?.free_time_min);
  const billableHours = dwellMin !== null && freeTimeMin !== null ? round2(Math.max(0, dwellMin - freeTimeMin) / 60) : null;
  const detention =
    claimedHours !== null || dwellMin !== null || freeTimeMin !== null
      ? { claimedHours, dwellMin, freeTimeMin, billableHours, overClaimed: claimedHours !== null && billableHours !== null && claimedHours > billableHours + 0.01 }
      : null;

  const shipper = object(record.shipper);
  const flags = [...new Set([...strings(record.ingest_flags), ...strings(identity?.flags)])];

  return {
    state: "ready",
    file: {
      recordId,
      updatedAt: text(row.updated_at),
      documents,
      missing,
      emails,
      otherRefs,
      bank,
      billed,
      applied,
      deposited,
      carrier,
      invoiceCarrier,
      shipper: text(shipper?.name) || text(record.shipper_id),
      bol: text(record.bol),
      loadId: text(record.load_id),
      lane: text(record.lane),
      rate,
      detention,
      claimType: text(record.claim_type),
      deductionReason: text(record.deduction_reason),
      bankChange: bankChange
        ? {
            payee: text(bankChange.new_payee),
            accountLast4: text(bankChange.new_account_last4),
            via: text(bankChange.channel) || text(bankChange.requested_via),
            urgency: text(bankChange.urgency_language),
            senderMatches: typeof bankChange.sender_matches_registered === "boolean" ? bankChange.sender_matches_registered : null,
          }
        : null,
      flags,
      record,
    },
  };
}

/** "4 docs · 1 email · 1 bank line": the collapsed panel's summary. */
export function caseFileSummary(file: CaseFile): string {
  const part = (n: number, one: string, many = `${one}s`) => (n ? `${n} ${n === 1 ? one : many}` : "");
  return [part(file.documents.length, "doc"), part(file.emails.length, "email"), part(file.bank.length, "bank line")].filter(Boolean).join(" · ") || "Empty";
}
