import { sha256 } from '@noble/hashes/sha256';
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Payload = Record<string, Json>;

/** Canonical UTF-8 JSON: sorted object keys, preserved array order, no undefined. */
export function canonical(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'bigint') return JSON.stringify(value.toString());
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Non-finite JSON number');
  if (typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical((value as Record<string, unknown>)[k])}`).join(',')}}`;
}

export const hash = (value: string): string => bytesToHex(sha256(utf8ToBytes(value)));

export function payloadFrom(raw: string): Payload {
  if (raw.length > 262144) throw new Error('payload exceeds 256 KiB');
  const parsed: unknown = JSON.parse(raw);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('payload must be a JSON object');
  return parsed as Payload;
}

export function text(p: Payload, key: string, fallback?: string, max = 4096): string {
  const value = p[key] ?? fallback;
  if (typeof value !== 'string' || value.length > max || (fallback === undefined && !value.trim())) {
    throw new Error(`${key} must be a ${fallback === undefined ? 'nonempty ' : ''}string of at most ${max} characters`);
  }
  return value;
}

export function num(p: Payload, key: string, fallback?: number): number {
  const value = p[key] ?? fallback;
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${key} must be a finite number`);
  return value;
}

export function uint(p: Payload, key: string, fallback = 0): number {
  const value = num(p, key, fallback);
  if (!Number.isInteger(value) || value < 0 || value > 4294967295) throw new Error(`${key} must be a u32`);
  return value;
}

export function bool(p: Payload, key: string, fallback = true): boolean {
  const value = p[key] ?? fallback;
  if (typeof value !== 'boolean') throw new Error(`${key} must be boolean`);
  return value;
}

export function choice(p: Payload, key: string, values: readonly string[], fallback?: string): string {
  const value = text(p, key, fallback);
  if (!values.includes(value)) throw new Error(`${key} must be one of ${values.join(', ')}`);
  return value;
}

export function list(p: Payload, key: string): string[] {
  const value = p[key] ?? [];
  if (!Array.isArray(value) || value.length > 200 || value.some(x => typeof x !== 'string' || x.length > 4096)) {
    throw new Error(`${key} must be an array of at most 200 strings`);
  }
  return value as string[];
}

export function json(p: Payload, key: string, fallback: Json = {}): string {
  const value = p[key] ?? fallback;
  // *_json accepts either an object or valid encoded JSON for existing callers.
  return canonical(typeof value === 'string' ? JSON.parse(value) : value);
}

export function probability(p: Payload, key = 'confidence', fallback = 0): number {
  const value = num(p, key, fallback);
  if (value < 0 || value > 1) throw new Error(`${key} must be between 0 and 1`);
  return value;
}

/** Public amounts use USD. All controls and addition use safe integer cents. */
export function cents(value: number, signed = false): number {
  if (!Number.isFinite(value) || (!signed && value < 0)) throw new Error('amount must be finite and nonnegative');
  const scaled = value * 100;
  const rounded = Math.round(scaled);
  // Small binary representation noise is permitted, fractional cents are not.
  if (!Number.isSafeInteger(rounded) || Math.abs(rounded) > 1_000_000_000_000 || Math.abs(scaled - rounded) > 0.00001) {
    throw new Error('amount must contain at most two decimals and fit the supported cents range');
  }
  return rounded;
}

export function amount(p: Payload, key = 'amount', fallback?: number, signed = false): number {
  return cents(num(p, key, fallback), signed) / 100;
}

export function positiveAmount(p: Payload, key = 'amount'): number {
  const result = amount(p, key);
  if (result <= 0) throw new Error(`${key} must be positive`);
  return result;
}

export function sumCents(values: number[]): number {
  const total = values.reduce((sum, value) => sum + cents(value), 0);
  if (!Number.isSafeInteger(total) || total > 1_000_000_000_000) throw new Error('aggregate amount exceeds supported range');
  return total;
}

export type JournalLine = { account: string; debit: number; credit: number };
export function journalLines(value: Json | undefined): JournalLine[] {
  if (!Array.isArray(value) || value.length < 2 || value.length > 200) throw new Error('journal must contain 2–200 lines');
  const lines = value.map(item => {
    if (!item || Array.isArray(item) || typeof item !== 'object') throw new Error('invalid journal line');
    const line = { account: text(item, 'account'), debit: amount(item, 'debit', 0), credit: amount(item, 'credit', 0) };
    if ((line.debit > 0) === (line.credit > 0)) throw new Error('each journal line must have exactly one positive debit or credit');
    return line;
  });
  if (sumCents(lines.map(x => x.debit)) !== sumCents(lines.map(x => x.credit))) throw new Error('P-006: journal entry is not balanced');
  return lines;
}

export const AGENTS = ['carrier_ap', 'shipper_ar', 'cfo', 'risk', 'controller', 'verification', 'voice_desk', 'validator', 'documents'] as const;
export const ROLES: readonly string[] = [...AGENTS, 'admin', 'human', 'ingest', 'harness', 'evaluator', 'router'];
export const DELEGATIONS: Record<string, readonly string[]> = {
  carrier_ap: ['risk', 'controller', 'verification', 'documents'], shipper_ar: ['controller', 'verification', 'voice_desk', 'cfo', 'documents'],
  cfo: ['shipper_ar', 'carrier_ap', 'controller'], risk: ['verification', 'controller', 'documents'],
  controller: ['cfo', 'verification', 'voice_desk', 'documents'], verification: ['voice_desk'], voice_desk: [], validator: [], documents: ['voice_desk'],
};
export const SKILLS: Record<string, readonly string[]> = {
  carrier_ap: ['process_carrier_invoice', 'propose_payment', 'answer_payment_status'],
  shipper_ar: ['reconcile_customer_payment', 'chase_overdue', 'set_credit_limit'],
  cfo: ['assess_financial_impact', 'cash_forecast'], risk: ['investigate_charge', 'screen_bank_change', 'screen_counterparty'],
  controller: ['review_hold', 'check_already_paid', 'reconcile', 'approve_entry'],
  verification: ['verify_claim', 'verify_bank_change', 'verify_dispute'],
  voice_desk: ['place_call', 'handle_inbound_call'], validator: ['review_output', 'review_flag'],
  documents: ['collect_documents', 'check_document_packet'],
};
export const TERMINAL = ['completed', 'failed', 'canceled', 'rejected'];
export const A2A_STATES = ['submitted', 'working', 'input-required', 'auth-required', ...TERMINAL];
export const BUSINESS_STATES = ['queued', 'investigating', 'awaiting_other_agent', 'awaiting_external_verification', 'awaiting_call', 'awaiting_human_approval', 'resolved', 'rejected', 'failed'];

export function taskTransition(before: string, after: string): void {
  if (!A2A_STATES.includes(after)) throw new Error('invalid A2A state');
  if (TERMINAL.includes(before) && before !== after) throw new Error('terminal task cannot be reopened; create a new task');
  if (before !== 'submitted' && after === 'submitted') throw new Error('task cannot return to submitted');
}

export function phone(value: string): string {
  if (!/^\+[1-9]\d{7,14}$/.test(value)) throw new Error('phone must use E.164 format');
  return value;
}

export function iso(value: string): string {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) throw new Error('invalid ISO timestamp');
  return new Date(parsed).toISOString();
}
