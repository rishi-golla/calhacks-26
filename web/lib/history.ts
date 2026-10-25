/**
 * Imported history: the brokerage's books from before FreightLine ran.
 *
 * `scripts/seed_history.py` writes them through the admin-only
 * `import_history_batch` reducer (spacetime/module/CONTRACT.md, "Imported
 * history"): resolved cases, carrier invoices, posted journals and case files,
 * dated in the past. Case and journal ids start with `HIST-`; invoices and case
 * files belong to a `HIST-` case. There is never agent work on them (no task,
 * dispatch or review rows), so pages treat them as the company's past:
 *
 * - "Start fresh" keeps them visible (lib/view-epoch), so Finance shows the
 *   imported books plus live activity since the epoch;
 * - agent metrics (automation, median resolution, time saved) leave them out;
 * - the case page labels them "Imported history" and shows no trace graph.
 */
import { str, type Row } from "./data";

export const HISTORY_PREFIX = "HIST-";

export const isImportedId = (id: unknown): boolean => typeof id === "string" && id.startsWith(HISTORY_PREFIX);

/** A row of imported history: a `HIST-` id (case, journal) or a `HIST-` case. */
export const isImportedRow = (row: Row): boolean => isImportedId(row.case_id) || isImportedId(row.id);

/** Audit kinds the import and its removal write: bookkeeping about the import, not activity. */
export const HISTORY_AUDIT_KINDS = new Set(["import_history_batch", "remove_imported_history"]);

export const isHistoryAudit = (row: Row): boolean => HISTORY_AUDIT_KINDS.has(str(row.kind, ""));
