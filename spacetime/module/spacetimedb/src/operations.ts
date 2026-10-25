import type { Ctx } from './schema';
import {
  AGENTS, ROLES, SKILLS, DELEGATIONS, TERMINAL, BUSINESS_STATES, A2A_STATES,
  amount, bool, canonical, cents, choice, hash, iso, journalLines, json, list, num,
  payloadFrom, phone, positiveAmount, probability, sumCents, taskTransition, text, uint,
  type Payload,
} from './policy';

type Principal = NonNullable<ReturnType<Ctx['db']['principal']['identity']['find']>>;
type Cell = { sheet: string; row_key: string; column: string; before: string; after: string };
const now = (ctx: Ctx) => ctx.timestamp.toISOString();
const identity = (ctx: Ctx) => ctx.sender.toHexString();
const timestampAfter = (ctx: Ctx, seconds: number) => new Date(Number(ctx.timestamp.microsSinceUnixEpoch / 1000n) + seconds * 1000).toISOString();
const must = <T>(value: T | undefined | null, message: string): T => {
  if (value === undefined || value === null) throw new Error(message);
  return value;
};

function principal(ctx: Ctx, roles: readonly string[]): Principal {
  const p = must(ctx.db.principal.identity.find(identity(ctx)), 'identity is not provisioned');
  if (!p.active || !roles.includes(p.role)) throw new Error(`role ${p.role} may not perform this action`);
  if (AGENTS.includes(p.role as typeof AGENTS[number])) {
    const config = ctx.db.agent_config.agent.find(p.role);
    if (config && !config.enabled) throw new Error('agent is disabled by a human');
  }
  return p;
}

function caseFor(ctx: Ctx, caseId: string, actor: Principal, assist = false) {
  const row = must(ctx.db.case.case_id.find(caseId), 'case does not exist');
  if (row.status === 'human_owned' && actor.role !== 'human' && !assist) {
    throw new Error('case is human_owned; agents may only record assistance or evidence');
  }
  return row;
}

function ownedTask(ctx: Ctx, taskId: string, actor: Principal) {
  const task = must(ctx.db.task.task_id.find(taskId), 'task does not exist');
  if (task.agent !== actor.role) throw new Error('task belongs to another agent');
  return task;
}

class Mutation {
  readonly cells: Cell[] = [];
  readonly payloadHash: string;
  // Set by a handler whose request already matches stored state: no change set or audit event is
  // written, only the receipt, so the write ID still rejects different contents later.
  unchanged = false;
  constructor(readonly ctx: Ctx, readonly p: Payload, readonly actor: Principal, readonly action: string) {
    // Handlers enrich audit context with authoritative case IDs. Retry identity
    // must continue to hash the original caller payload, not that enrichment.
    this.payloadHash = hash(canonical({ action, payload: p }));
  }
  capture(sheet: string, key: string, before: object | null, after: object): void {
    const old = (before ?? {}) as Record<string, unknown>;
    const next = after as Record<string, unknown>;
    for (const column of [...new Set([...Object.keys(old), ...Object.keys(next)])].sort()) {
      const previous = canonical(old[column]);
      const current = canonical(next[column]);
      if (previous !== current) {
        const existing = this.cells.find(cell => cell.sheet === sheet && cell.row_key === key && cell.column === column);
        if (existing) existing.after = current;
        else this.cells.push({ sheet, row_key: key, column, before: previous, after: current });
      }
    }
  }
  finish(): void {
    const { ctx, p, actor, action } = this;
    if (this.unchanged) {
      if (this.cells.length) throw new Error('an unchanged mutation cannot capture changes');
      this.receipt('', now(ctx));
      return;
    }
    // Dispatch references a remote first-hop task, which may not yet be mirrored
    // here. Its ID lives on the dispatch row, not as a router-owned audit task.
    const taskId = action === 'record_dispatch' ? '' : text(p, 'task_id', '');
    if (taskId) {
      const task = must(ctx.db.task.task_id.find(taskId), 'audit task does not exist');
      if (action === 'record_delegation') {
        const parent = ownedTask(ctx, text(p, 'parent_task_id'), actor);
        if (task.parent_task_id !== parent.task_id || task.case_id !== parent.case_id) throw new Error('delegation audit task does not match its parent');
      } else if (task.agent !== actor.role) {
        throw new Error('audit task must belong to the authenticated actor');
      }
      if (p.case_id && p.case_id !== task.case_id) throw new Error('audit task belongs to another case');
      p.case_id = task.case_id;
    }
    const caseId = text(p, 'case_id', '');
    const chainScope = caseId ? `case:${caseId}` : 'system';
    const changeHeadName = `changes:${chainScope}`;
    const auditHeadName = `audit:${chainScope}`;
    const head = ctx.db.chain_head.name.find(changeHeadName);
    const policy_ids = [...new Set([...policyIds(action), ...list(p, 'policy_ids')])].sort();
    if (policy_ids.some(id => !/^P-0(?:0[1-9]|1[0-2])$/.test(id))) throw new Error('unknown policy ID');
    const cells = this.cells.filter(cell => cell.before !== cell.after).sort((a, b) => {
      const left = canonical([a.sheet, a.row_key, a.column]);
      const right = canonical([b.sheet, b.row_key, b.column]);
      return left < right ? -1 : left > right ? 1 : 0;
    });
    const base = {
      parent_id: head?.id ?? '', parent_hash: head?.hash ?? '', sequence: (head?.sequence ?? 0) + 1,
      case_id: caseId, task_id: taskId, agent: actor.actor,
      actor_identity: actor.identity, action, summary: text(p, 'summary', action), policy_ids,
      instr_version: uint(p, 'instr_version'), memory_ids: list(p, 'memory_ids'), ts: now(ctx),
      write_id: text(p, 'write_id', undefined, 200),
    };
    const hash_body = canonical({ ...base, cells });
    const digest = hash(hash_body);
    const id = digest.slice(0, 24);
    ctx.db.change_set.insert({ ...base, id, hash: digest, hash_body });
    cells.forEach((cell, index) => ctx.db.cell_change.insert({ ...cell, id: `${id}:${index}`, change_set_id: id }));
    const next = { name: changeHeadName, id, hash: digest, sequence: base.sequence };
    if (head) ctx.db.chain_head.name.update(next); else ctx.db.chain_head.insert(next);

    const auditHead = ctx.db.chain_head.name.find(auditHeadName);
    const audit = {
      id: `audit:${id}`, ts: base.ts, case_id: base.case_id, task_id: base.task_id,
      agent: actor.actor, actor_identity: actor.identity, kind: action === 'append_audit' ? text(p, 'kind', action) : action,
      reason_code: text(p, 'reason_code', ''), summary: base.summary,
      evidence_refs: list(p, 'evidence_refs'), policy_ids, model_used: text(p, 'model_used', ''),
      prev_hash: auditHead?.hash ?? '', sequence: (auditHead?.sequence ?? 0) + 1, change_set_id: id,
    };
    const auditBody = canonical(audit);
    const auditHash = hash(auditBody);
    ctx.db.audit_event.insert({ ...audit, hash: auditHash, hash_body: auditBody });
    const nextAudit = { name: auditHeadName, id: audit.id, hash: auditHash, sequence: audit.sequence };
    if (auditHead) ctx.db.chain_head.name.update(nextAudit); else ctx.db.chain_head.insert(nextAudit);
    this.receipt(id, base.ts);
  }
  private receipt(change_set_id: string, ts: string): void {
    const { ctx, p, actor, action } = this;
    ctx.db.write_receipt.insert({
      id: `${actor.identity}:${text(p, 'write_id', undefined, 200)}`, action, actor_identity: actor.identity,
      payload_hash: this.payloadHash, change_set_id, ts,
    });
  }
}

function policyIds(action: string): string[] {
  const controls: Record<string, string[]> = {
    place_hold: ['P-001'], release_hold: ['P-003', 'P-005'],
    propose_payment: ['P-001', 'P-002', 'P-003', 'P-007'],
    request_journal_entry: ['P-006'], post_journal_entry: ['P-005', 'P-006'],
    decide_approval: ['P-005', 'P-010'], request_approval: ['P-005'],
    upsert_call: ['P-008', 'P-011'], upsert_memory: ['P-012'],
    publish_instruction_version: ['P-012'], revert_instruction_version: ['P-012'],
  };
  return controls[action] ?? [];
}

type Handler = (m: Mutation) => void;
export function execute(ctx: Ctx, raw: string, action: string, roles: readonly string[], handle: Handler): void {
  try {
    const p = payloadFrom(raw);
    const actor = principal(ctx, roles);
    const writeId = text(p, 'write_id', undefined, 200);
    const receipt = ctx.db.write_receipt.id.find(`${actor.identity}:${writeId}`);
    if (receipt) {
      if (receipt.payload_hash !== hash(canonical({ action, payload: p }))) throw new Error('write_id was reused with different contents');
      return;
    }
    const m = new Mutation(ctx, p, actor, action);
    handle(m);
    m.finish();
  } catch (error) {
    // Failed reducers cannot append audit rows: SpacetimeDB rolls back their
    // transactions. Preserve sanitized refusals in server logs for W3 review.
    console.warn(`reducer denied action=${action} identity=${identity(ctx)}: ${error instanceof Error ? error.message : 'invalid request'}`);
    throw error instanceof Error ? error : new Error('invalid request');
  }
}

export function initialize(ctx: Ctx): void {
  const actor = { identity: identity(ctx), role: 'admin', actor: 'admin:publisher', active: true, granted_at: now(ctx) };
  ctx.db.principal.insert(actor);
  const m = new Mutation(ctx, { write_id: 'module:init', summary: 'Provision publisher and agent defaults' }, actor, 'initialize');
  m.capture('principal', actor.identity, null, actor);
  for (const agent of AGENTS) {
    const config = { agent, model: 'auto', enabled: true };
    ctx.db.agent_config.insert(config);
    m.capture('agent_config', agent, null, config);
  }
  m.finish();
}

export const grantRole: Handler = m => {
  const { ctx, p } = m;
  const key = text(p, 'identity').toLowerCase().replace(/^0x/, '');
  if (!/^[0-9a-f]{64}$/.test(key)) throw new Error('identity must be 64 hexadecimal digits');
  const role = choice(p, 'role', ROLES);
  let actor = text(p, 'actor', role);
  if (AGENTS.includes(role as typeof AGENTS[number]) && actor !== role) throw new Error('agent actor must match its registered role');
  if (role === 'human' && !actor.startsWith('human:')) actor = `human:${actor}`;
  const previous = ctx.db.principal.identity.find(key);
  const next = { identity: key, role, actor, active: bool(p, 'active'), granted_at: now(ctx) };
  if (previous?.role === 'admin' && previous.active && (role !== 'admin' || !next.active)) {
    if (![...ctx.db.principal.iter()].some(x => x.identity !== key && x.role === 'admin' && x.active)) throw new Error('cannot remove the final active administrator');
  }
  if (next.active && [...ctx.db.principal.actor.filter(actor)].some(x => x.identity !== key && x.active)) throw new Error('actor is already assigned to an active identity');
  if (previous) ctx.db.principal.identity.update(next); else ctx.db.principal.insert(next);
  m.capture('principal', key, previous, next);
};

// Ingest opens a case at its first routable event (often a POD, before the carrier bills) and refreshes
// title, priority and exposure as source documents arrive. Status, timestamps and a set load are not ingest's.
export const openCase: Handler = m => {
  const { ctx, p, actor } = m;
  const case_id = text(p, 'case_id');
  const previous = ctx.db.case.case_id.find(case_id);
  if (previous) caseFor(ctx, case_id, actor);
  const load_id = text(p, 'load_id', previous?.load_id ?? '');
  if (previous?.load_id && load_id !== previous.load_id) throw new Error('case load is immutable');
  const row = {
    case_id, load_id, title: text(p, 'title', previous?.title),
    status: previous?.status ?? 'open', priority: choice(p, 'priority', ['P0', 'P1', 'P2', 'P3'], previous?.priority ?? 'P2'),
    exposure_usd: amount(p, 'exposure_usd', previous?.exposure_usd ?? 0), opened_at: previous?.opened_at ?? now(ctx),
    resolved_at: previous?.resolved_at ?? '',
  };
  if (previous) ctx.db.case.case_id.update(row); else ctx.db.case.insert(row);
  m.capture('case', row.case_id, previous, row);
};

export const updateCase: Handler = m => {
  const { ctx, p, actor } = m;
  const previous = caseFor(ctx, text(p, 'case_id'), actor, true);
  const status = choice(p, 'status', ['open', 'in_progress', 'resolved', 'human_owned']);
  if ((status === 'human_owned' || previous.status === 'human_owned') && actor.role !== 'human') throw new Error('only a human may take or release case ownership');
  const next = { ...previous, status, resolved_at: status === 'resolved' ? now(ctx) : '' };
  ctx.db.case.case_id.update(next);
  m.capture('case', next.case_id, previous, next);
};

export const upsertInvoice: Handler = m => {
  const { ctx, p, actor } = m;
  const id = text(p, 'id');
  const previous = ctx.db.invoice.id.find(id);
  const case_id = text(p, 'case_id');
  const c = caseFor(ctx, case_id, actor);
  const row = {
    id, case_id, carrier_id: text(p, 'carrier_id'), load_id: text(p, 'load_id', c.load_id),
    amount: positiveAmount(p), contract_amount: amount(p, 'contract_amount'), paid_amount: previous?.paid_amount ?? 0,
    issued_at: p.issued_at ? iso(text(p, 'issued_at')) : previous?.issued_at ?? now(ctx), status: 'open',
  };
  if (p.paid_amount !== undefined && amount(p, 'paid_amount') !== row.paid_amount) throw new Error('ingest cannot overwrite paid_amount');
  if (row.load_id !== c.load_id) throw new Error('invoice must belong to the case load');
  if (previous && ['case_id', 'carrier_id', 'load_id'].some(key => previous[key as 'case_id'] !== row[key as 'case_id'])) throw new Error('invoice case, carrier and load are immutable');
  const holds = [...ctx.db.hold.invoice_id.filter(id)].map(x => x.amount - x.released_amount);
  const payments = [...ctx.db.payment_proposal.invoice_id.filter(id)].filter(x => x.status !== 'cancelled').map(x => x.amount);
  if (cents(row.amount) < sumCents([...holds, ...payments, row.paid_amount])) throw new Error('invoice cannot be below committed holds and payments');
  if (previous) ctx.db.invoice.id.update(row); else ctx.db.invoice.insert(row);
  m.capture('ap_subledger', id, previous, row);
};

export const setPeriod: Handler = m => {
  const { ctx, p } = m;
  const period = text(p, 'period');
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(period)) throw new Error('period must be YYYY-MM');
  const previous = ctx.db.accounting_period.period.find(period);
  const row = { period, status: choice(p, 'status', ['open', 'locked']), updated_at: now(ctx) };
  if (previous) ctx.db.accounting_period.period.update(row); else ctx.db.accounting_period.insert(row);
  m.capture('accounting_period', period, previous, row);
};

function requireOpenPeriod(ctx: Ctx, period: string): void {
  if (ctx.db.accounting_period.period.find(period)?.status !== 'open') throw new Error('P-006: accounting period is missing or locked');
}

export const requestJournalEntry: Handler = m => {
  const { ctx, p, actor } = m;
  const case_id = text(p, 'case_id');
  caseFor(ctx, case_id, actor);
  const period = text(p, 'period');
  requireOpenPeriod(ctx, period);
  const row = {
    id: text(p, 'id'), case_id, period, lines: journalLines(p.lines), memo: text(p, 'memo', ''),
    requested_by: actor.actor, requester_identity: actor.identity, created_at: now(ctx), posted_entry_id: '',
  };
  if (p.requested_by !== undefined && p.requested_by !== actor.actor) throw new Error('requester must match the caller');
  ctx.db.journal_request.insert(row);
  m.capture('journal_request', row.id, null, row);
};

export const postJournalEntry: Handler = m => {
  const { ctx, p, actor } = m;
  const request = must(ctx.db.journal_request.id.find(text(p, 'request_id')), 'journal request does not exist; requester must submit it first');
  caseFor(ctx, request.case_id, actor);
  if (request.posted_entry_id) throw new Error('journal request has already been posted');
  if (request.requester_identity === actor.identity || request.requested_by === actor.actor) throw new Error('P-005: requester cannot approve their own journal');
  if (p.requested_by !== undefined && p.requested_by !== request.requested_by) throw new Error('requested_by does not match authenticated journal request');
  if (p.approved_by !== undefined && p.approved_by !== actor.actor) throw new Error('approved_by must match caller');
  if (p.period !== undefined && p.period !== request.period) throw new Error('period does not match requested entry');
  if (p.lines !== undefined && canonical(journalLines(p.lines)) !== canonical(request.lines)) throw new Error('lines do not match requested entry');
  requireOpenPeriod(ctx, request.period);
  journalLines(request.lines);
  const row = {
    id: text(p, 'id'), case_id: request.case_id, period: request.period, lines: request.lines, memo: request.memo,
    requested_by: request.requested_by, approved_by: actor.actor, posted_at: now(ctx), request_id: request.id,
    requester_identity: request.requester_identity, approver_identity: actor.identity,
  };
  ctx.db.ledger_entry.insert(row);
  const posted = { ...request, posted_entry_id: row.id };
  ctx.db.journal_request.id.update(posted);
  m.capture('ledger_entry', row.id, null, row);
  row.lines.forEach((line, index) => {
    const cellRow = { entry_id: row.id, line: index + 1, case_id: row.case_id, period: row.period,
      account: line.account, debit: line.debit, credit: line.credit, memo: row.memo,
      requested_by: row.requested_by, approved_by: row.approved_by };
    const key = `${row.id}:${index}`;
    m.capture('gl', key, null, cellRow);
    const account = line.account.trim().toLowerCase().replace(/[ -]+/g, '_');
    if (['accounts_receivable', 'account_receivable', 'ar', 'a/r', 'receivables', 'trade_receivables'].includes(account)) m.capture('ar_subledger', key, null, cellRow);
    if (['accounts_payable', 'account_payable', 'ap', 'a/p', 'payables', 'trade_payables'].includes(account)) m.capture('ap_subledger', key, null, cellRow);
  });
  m.capture('journal_request', request.id, request, posted);
  p.case_id = row.case_id;
};

function invoiceFor(m: Mutation, invoiceId: string, caseId?: string) {
  const invoice = must(m.ctx.db.invoice.id.find(invoiceId), 'invoice does not exist; ingest it first');
  if (caseId && invoice.case_id !== caseId) throw new Error('invoice belongs to another case');
  caseFor(m.ctx, invoice.case_id, m.actor);
  return invoice;
}

export const placeHold: Handler = m => {
  const { ctx, p, actor } = m;
  const invoice = invoiceFor(m, text(p, 'invoice_id'), text(p, 'case_id'));
  const value = positiveAmount(p);
  const existing = [...ctx.db.hold.invoice_id.filter(invoice.id)];
  const held = sumCents(existing.map(x => (cents(x.amount) - cents(x.released_amount)) / 100));
  const reserved = sumCents([...ctx.db.payment_proposal.invoice_id.filter(invoice.id)].filter(x => x.status !== 'cancelled').map(x => x.amount));
  if (held + reserved + cents(value) + cents(invoice.paid_amount) > cents(invoice.amount)) throw new Error('hold exceeds invoice available balance');
  const row = {
    id: text(p, 'id'), case_id: invoice.case_id, invoice_id: invoice.id, amount: value,
    reason_code: text(p, 'reason_code'), status: 'active', placed_by: actor.actor,
    released_by: '', released_amount: 0, placed_at: now(ctx), released_at: '',
  };
  ctx.db.hold.insert(row);
  m.capture('holds', row.id, null, row);
};

function consumeApproval(m: Mutation, id: string, action: string, target: string, caseId: string, value: number, invoiceId: string) {
  const { ctx } = m;
  const approval = must(ctx.db.approval.id.find(id), 'P-003: human approval does not exist');
  const human = ctx.db.principal.identity.find(approval.approver);
  if (approval.decision !== 'approved' || !approval.code_ok || approval.consumed_at || !human?.active || human.role !== 'human') throw new Error('P-003: valid unused human approval required');
  if (approval.action !== action || approval.target_id !== target || approval.case_id !== caseId || approval.invoice_id !== invoiceId || cents(approval.amount) !== cents(value)) throw new Error('approval is for a different action, target, case, invoice or amount');
  if (Date.parse(approval.expires_at) <= Number(ctx.timestamp.microsSinceUnixEpoch / 1000n)) throw new Error('approval has expired');
  if (approval.requester_identity === approval.approver) throw new Error('P-005: requester cannot approve');
  const next = { ...approval, consumed_at: now(ctx) };
  ctx.db.approval.id.update(next);
  m.capture('approval', approval.id, approval, next);
  return approval;
}

export const releaseHold: Handler = m => {
  const { ctx, p, actor } = m;
  const previous = must(ctx.db.hold.id.find(text(p, 'id')), 'hold does not exist');
  invoiceFor(m, previous.invoice_id, previous.case_id);
  const remaining = cents(previous.amount) - cents(previous.released_amount);
  const value = amount(p, 'amount', remaining / 100);
  if (cents(value) <= 0 || cents(value) > remaining) throw new Error('release exceeds remaining hold');
  // P-003 is thresholded on everything ever held on the invoice: original hold
  // amounts, released holds included. Holds are never deleted or shrunk, so the
  // total only grows and at most $5,000 of an invoice can be released without a
  // human, however the hold is split, partially released or placed in turn.
  const exposure = sumCents([...ctx.db.hold.invoice_id.filter(previous.invoice_id)].map(x => x.amount));
  if (exposure > 500000) consumeApproval(m, text(p, 'approval_id', text(p, 'human_approval_id', '')), 'release_hold', previous.id, previous.case_id, value, previous.invoice_id);
  const released_amount = (cents(previous.released_amount) + cents(value)) / 100;
  const row = { ...previous, released_amount, status: cents(released_amount) === cents(previous.amount) ? 'released' : 'active', released_by: actor.actor, released_at: now(ctx) };
  ctx.db.hold.id.update(row);
  m.capture('holds', row.id, previous, row);
  p.case_id = row.case_id;
};

export const proposePayment: Handler = m => {
  const { ctx, p, actor } = m;
  const invoice = invoiceFor(m, text(p, 'invoice_id'), text(p, 'case_id'));
  const value = positiveAmount(p);
  if (p.carrier_id !== undefined && p.carrier_id !== invoice.carrier_id) throw new Error('carrier does not match invoice');
  if ([...ctx.db.hold.invoice_id.filter(invoice.id)].some(x => cents(x.amount) > cents(x.released_amount))) throw new Error('P-002: invoice has an active hold');
  const reserved = sumCents([...ctx.db.payment_proposal.invoice_id.filter(invoice.id)].filter(x => x.status !== 'cancelled').map(x => x.amount));
  if (reserved + cents(value) + cents(invoice.paid_amount) > cents(invoice.amount)) throw new Error('payment exceeds invoice available balance');
  // Inflated invoices cannot bypass investigation by skipping place_hold.
  if (cents(invoice.amount) * 100 > cents(invoice.contract_amount) * 102 && reserved + cents(value) + cents(invoice.paid_amount) > cents(invoice.contract_amount)) throw new Error('P-001: payment exceeds contract on an inflated invoice');
  const cutoff = Number(ctx.timestamp.microsSinceUnixEpoch / 1000n) - 14 * 86400000;
  if ([...ctx.db.payment_proposal.carrier_id.filter(invoice.carrier_id)].some(x => x.status !== 'cancelled' && x.load_id === invoice.load_id && cents(x.amount) === cents(value) && Date.parse(x.proposed_at) >= cutoff)) throw new Error('P-007: duplicate carrier/load/amount payment within 14 days');
  const id = text(p, 'id');
  const approval_id = text(p, 'approval_id', text(p, 'human_approval_id', ''));
  // Authorization follows the invoice exposure, preventing differently sized
  // sub-threshold proposals from splitting an otherwise approvable payment.
  if (cents(invoice.amount) > 500000) consumeApproval(m, approval_id, 'propose_payment', id, invoice.case_id, value, invoice.id);
  const row = {
    id, case_id: invoice.case_id, invoice_id: invoice.id, carrier_id: invoice.carrier_id,
    load_id: invoice.load_id, amount: value, status: 'proposed', proposed_by: actor.actor, proposed_at: now(ctx), approval_id,
  };
  ctx.db.payment_proposal.insert(row);
  m.capture('payments', id, null, row);
};

export const requestApproval: Handler = m => {
  const { ctx, p, actor } = m;
  const case_id = text(p, 'case_id');
  caseFor(ctx, case_id, actor);
  const approver = text(p, 'approver').toLowerCase().replace(/^0x/, '');
  const human = must(ctx.db.principal.identity.find(approver), 'approver is not provisioned');
  if (!human.active || human.role !== 'human') throw new Error('approver must be an active human identity');
  if (approver === actor.identity) throw new Error('P-005: requester cannot approve');
  const action = choice(p, 'action', ['release_hold', 'propose_payment']);
  const target_id = text(p, 'target_id');
  const value = positiveAmount(p);
  let invoice_id: string;
  if (action === 'release_hold') {
    const hold = must(ctx.db.hold.id.find(target_id), 'approval target hold does not exist');
    if (hold.case_id !== case_id || cents(value) > cents(hold.amount) - cents(hold.released_amount)) throw new Error('approval target case or amount does not match hold');
    invoice_id = hold.invoice_id;
    if (p.invoice_id !== undefined && p.invoice_id !== invoice_id) throw new Error('approval invoice does not match hold');
  } else invoice_id = invoiceFor(m, text(p, 'invoice_id'), case_id).id;
  const task_id = text(p, 'task_id', '');
  if (task_id && ctx.db.task.task_id.find(task_id)?.case_id !== case_id) throw new Error('approval task belongs to another case');
  const expires = uint(p, 'expires_in_seconds', 900);
  if (expires < 30 || expires > 3600) throw new Error('approval expiry must be between 30 and 3600 seconds');
  const row = {
    id: text(p, 'id'), case_id, task_id, action, target_id, amount: value, invoice_id,
    requested_by: actor.actor, requester_identity: actor.identity, approver, approver_name: human.actor,
    decision: 'pending', code_ok: false, decided_at: '', expires_at: timestampAfter(ctx, expires), consumed_at: '',
    code_issued: false, failed_attempts: 0, last_error: '',
  };
  ctx.db.approval.insert(row);
  m.capture('approval', row.id, null, row);
};

export const issueApprovalCode: Handler = m => {
  const { ctx, p } = m;
  const previous = must(ctx.db.approval.id.find(text(p, 'approval_id')), 'approval does not exist');
  if (previous.decision !== 'pending' || previous.code_issued) throw new Error('approval challenge already issued or decided');
  const code = text(p, 'code', undefined, 64);
  if (code.length < 6 || !/^[A-Za-z0-9-]+$/.test(code)) throw new Error('code must be 6–64 alphanumeric characters');
  const expires = uint(p, 'expires_in_seconds', 300);
  if (expires < 30 || expires > 900) throw new Error('code expiry must be 30–900 seconds');
  const expires_micros = ctx.timestamp.microsSinceUnixEpoch + BigInt(expires) * 1000000n;
  if (Date.parse(previous.expires_at) <= Number(ctx.timestamp.microsSinceUnixEpoch / 1000n)) throw new Error('approval has expired');
  ctx.db.approval_secret.insert({ approval_id: previous.id, code_hash: hash(`${previous.id}:${previous.approver}:${code}`), expires_micros, attempts: 0, consumed: false });
  const next = { ...previous, code_issued: true };
  ctx.db.approval.id.update(next);
  m.capture('approval', previous.id, previous, next);
  p.case_id = previous.case_id;
};

export const decideApproval: Handler = m => {
  const { ctx, p, actor } = m;
  const previous = must(ctx.db.approval.id.find(text(p, 'id')), 'approval does not exist');
  if (previous.approver !== actor.identity || previous.requester_identity === actor.identity) throw new Error('P-005: only the designated separate human may decide');
  if (previous.decision !== 'pending' || previous.consumed_at) throw new Error('approval has already been decided');
  const decision = choice(p, 'decision', ['approved', 'rejected']);
  const challenge = must(ctx.db.approval_secret.approval_id.find(previous.id), 'approval code has not been issued');
  if (challenge.consumed || challenge.attempts >= 5) throw new Error('approval code is consumed or locked');
  if (ctx.timestamp.microsSinceUnixEpoch >= challenge.expires_micros || Date.parse(previous.expires_at) <= Number(ctx.timestamp.microsSinceUnixEpoch / 1000n)) throw new Error('approval code has expired');
  const code = text(p, 'code', undefined, 64);
  const verified = hash(`${previous.id}:${actor.identity}:${code}`) === challenge.code_hash;
  ctx.db.approval_secret.approval_id.update({ ...challenge, attempts: challenge.attempts + 1, consumed: verified });
  const next = verified
    ? { ...previous, decision, code_ok: true, decided_at: now(ctx), last_error: '' }
    : { ...previous, failed_attempts: previous.failed_attempts + 1, last_error: challenge.attempts + 1 >= 5 ? 'code_locked' : 'invalid_code' };
  ctx.db.approval.id.update(next);
  m.capture('approval', previous.id, previous, next);
  p.case_id = previous.case_id;
};

function enqueue(m: Mutation, id: string, kind: string, caseId: string, taskId: string, body: object, targetAgent = 'validator'): void {
  const { ctx } = m;
  if (ctx.db.routing_outbox.id.find(id)) return;
  const row = { id, kind, target_agent: targetAgent, case_id: caseId, task_id: taskId, payload_json: canonical(body), status: 'pending', attempts: 0, created_at: now(ctx), updated_at: now(ctx), claimed_by: '', lease_expires_at: '', last_error: '' };
  ctx.db.routing_outbox.insert(row);
  m.capture('routing_outbox', id, null, row);
}

function refreshCounts(m: Mutation, agent: string): void {
  const { ctx } = m;
  const previous = ctx.db.agent.name.find(agent);
  const tasks = [...ctx.db.task.agent.filter(agent)];
  const row = {
    name: agent, status: previous?.status ?? 'online', active: tasks.filter(x => !TERMINAL.includes(x.a2a_state) && x.a2a_state !== 'submitted').length,
    queued: tasks.filter(x => x.a2a_state === 'submitted').length,
    done: tasks.filter(x => x.a2a_state === 'completed').length,
    errors: tasks.filter(x => ['failed', 'rejected'].includes(x.a2a_state)).length,
    p50_ms: previous?.p50_ms ?? 0, card_url: previous?.card_url ?? '', last_seen: previous?.last_seen ?? now(ctx),
  };
  if (previous) ctx.db.agent.name.update(row); else ctx.db.agent.insert(row);
  m.capture('agent', agent, previous, row);
}

export const upsertTask: Handler = m => {
  const { ctx, p, actor } = m;
  const task_id = text(p, 'task_id');
  const previous = ctx.db.task.task_id.find(task_id);
  const case_id = text(p, 'case_id', previous?.case_id);
  const c = caseFor(ctx, case_id, actor, !!previous);
  const agent = text(p, 'agent', actor.role);
  if (agent !== actor.role || (previous && previous.agent !== actor.role)) throw new Error('agent may only write its own tasks');
  const parent_task_id = text(p, 'parent_task_id', previous?.parent_task_id ?? '');
  const skill = text(p, 'skill', previous?.skill);
  if (!SKILLS[agent]?.includes(skill)) throw new Error('skill is not owned by this agent');
  if (!previous && parent_task_id) throw new Error('child tasks must first be reserved by record_delegation');
  if (previous && (case_id !== previous.case_id || parent_task_id !== previous.parent_task_id || skill !== previous.skill)) throw new Error('task case, parent and skill are immutable');
  const a2a_state = choice(p, 'a2a_state', A2A_STATES, previous?.a2a_state ?? 'submitted');
  if (previous) taskTransition(previous.a2a_state, a2a_state);
  if (c.status === 'human_owned' && previous && !TERMINAL.includes(a2a_state) && a2a_state !== previous.a2a_state) throw new Error('human takeover blocks further task execution');
  const row = {
    task_id, case_id, parent_task_id, agent, skill, display_id: text(p, 'display_id', previous?.display_id ?? task_id),
    a2a_state, business_status: choice(p, 'business_status', BUSINESS_STATES, previous?.business_status ?? 'queued'),
    outcome: text(p, 'outcome', previous?.outcome ?? ''), confidence: probability(p, 'confidence', previous?.confidence ?? 0),
    model_used: text(p, 'model_used', previous?.model_used ?? ''), started_at: previous?.started_at ?? now(ctx), updated_at: now(ctx),
    evidence_refs: p.evidence_refs === undefined ? previous?.evidence_refs ?? [] : list(p, 'evidence_refs'),
    artifact_json: p.artifact_json === undefined ? previous?.artifact_json ?? '{}' : json(p, 'artifact_json'),
    policy_ids: p.policy_ids === undefined ? previous?.policy_ids ?? [] : list(p, 'policy_ids'),
    instr_version: uint(p, 'instr_version', previous?.instr_version ?? 0), memory_ids: p.memory_ids === undefined ? previous?.memory_ids ?? [] : list(p, 'memory_ids'), depth: previous?.depth ?? 0,
  };
  const artifact: unknown = JSON.parse(row.artifact_json);
  if (!artifact || Array.isArray(artifact) || typeof artifact !== 'object') throw new Error('artifact_json must be a JSON object');
  if (previous && TERMINAL.includes(previous.a2a_state)) {
    if (canonical({ ...row, updated_at: previous.updated_at }) !== canonical(previous)) throw new Error('terminal task results are immutable; create a new task for revised output');
    return;
  }
  if (row.instr_version && !ctx.db.instruction_version.id.find(`${agent}:${row.instr_version}`)) throw new Error('task references an unknown instruction version');
  for (const id of row.memory_ids) if (ctx.db.agent_memory.id.find(id)?.agent !== agent) throw new Error('task references another agent or missing memory');
  if (previous) ctx.db.task.task_id.update(row); else ctx.db.task.insert(row);
  m.capture('task', task_id, previous, row);
  refreshCounts(m, agent);
  if (TERMINAL.includes(a2a_state) && (!previous || !TERMINAL.includes(previous.a2a_state)) && ['carrier_ap', 'shipper_ar', 'controller', 'cfo'].includes(agent)) {
    enqueue(m, `review_output:${task_id}`, 'review_output', case_id, task_id, { target_task_id: task_id, case_id, agent, artifact: JSON.parse(row.artifact_json) });
  }
};

export const recordDelegation: Handler = m => {
  const { ctx, p, actor } = m;
  const parent = ownedTask(ctx, text(p, 'parent_task_id'), actor);
  const case_id = text(p, 'case_id', parent.case_id);
  if (case_id !== parent.case_id) throw new Error('delegation must remain in its parent case');
  caseFor(ctx, case_id, actor);
  if (TERMINAL.includes(parent.a2a_state)) throw new Error('terminal tasks may not delegate');
  const to_agent = text(p, 'to_agent');
  const skill = text(p, 'skill');
  if (!DELEGATIONS[actor.role]?.includes(to_agent) || !SKILLS[to_agent]?.includes(skill)) throw new Error('delegation agent or skill is not allowlisted');
  if (ctx.db.agent_config.agent.find(to_agent)?.enabled === false) throw new Error('target agent is disabled');
  if (parent.depth >= 3) throw new Error('maximum delegation depth is 3');
  let ancestor: typeof parent | null = parent;
  const visited = new Set<string>();
  while (ancestor) {
    if (visited.has(ancestor.task_id)) throw new Error('task ancestry contains a cycle');
    visited.add(ancestor.task_id);
    if (ancestor.agent === to_agent) throw new Error('delegation would repeat an ancestor agent');
    ancestor = ancestor.parent_task_id ? must(ctx.db.task.task_id.find(ancestor.parent_task_id), 'missing parent task') : null;
  }
  if ([...ctx.db.delegation_edge.parent_task_id.filter(parent.task_id)].length >= 3) throw new Error('maximum delegation fanout is 3');
  const task_id = text(p, 'task_id');
  if (ctx.db.task.task_id.find(task_id)) throw new Error('delegated task ID already exists');
  const edge = { id: text(p, 'id'), ts: now(ctx), case_id, from_agent: actor.role, to_agent, skill, task_id, parent_task_id: parent.task_id, depth: parent.depth + 1 };
  ctx.db.delegation_edge.insert(edge);
  m.capture('delegation_edge', edge.id, null, edge);
  const child = {
    task_id, case_id, parent_task_id: parent.task_id, agent: to_agent, skill, display_id: text(p, 'display_id', task_id),
    a2a_state: 'submitted', business_status: 'queued', outcome: '', confidence: 0, model_used: '',
    started_at: now(ctx), updated_at: now(ctx), evidence_refs: [], artifact_json: '{}', policy_ids: [], instr_version: 0, memory_ids: [], depth: edge.depth,
  };
  ctx.db.task.insert(child);
  m.capture('task', task_id, null, child);
  refreshCounts(m, to_agent);
};

export const agentHeartbeat: Handler = m => {
  const { ctx, p, actor } = m;
  if (text(p, 'name', actor.role) !== actor.role) throw new Error('heartbeat may only update caller agent');
  refreshCounts(m, actor.role);
  const previous = must(ctx.db.agent.name.find(actor.role), 'agent was not initialized');
  const row = { ...previous, status: choice(p, 'status', ['online', 'busy', 'idle', 'offline', 'error'], 'online'), p50_ms: uint(p, 'p50_ms', previous.p50_ms), card_url: text(p, 'card_url', previous.card_url), last_seen: now(ctx) };
  ctx.db.agent.name.update(row);
  m.capture('agent', actor.role, previous, row);
};

export const appendAudit: Handler = m => {
  const { ctx, p, actor } = m;
  caseFor(ctx, text(p, 'case_id'), actor, true);
  if (p.task_id) {
    const task = ownedTask(ctx, text(p, 'task_id'), actor);
    if (task.case_id !== p.case_id) throw new Error('audit case does not match task');
  }
};

export const recordDocRead: Handler = m => {
  const { ctx, p, actor } = m;
  const task = ownedTask(ctx, text(p, 'task_id'), actor);
  const change_set_id = text(p, 'change_set_id', '');
  if (change_set_id && ctx.db.change_set.id.find(change_set_id)?.task_id !== task.task_id) throw new Error('document read change set belongs to another task');
  const row = { id: text(p, 'id'), change_set_id, task_id: task.task_id, agent: actor.role, doc_ref: text(p, 'doc_ref'), page: uint(p, 'page', 1), span: text(p, 'span', ''), extracted_json: json(p, 'extracted_json') };
  ctx.db.doc_read.insert(row);
  m.capture('doc_read', row.id, null, row);
  p.case_id = task.case_id;
};

export const setDemoTarget: Handler = m => {
  const { ctx, p } = m;
  const key = phone(text(p, 'phone'));
  const previous = ctx.db.demo_target.phone.find(key);
  const row = { phone: key, label: text(p, 'label'), enabled: bool(p, 'enabled') };
  if (previous) ctx.db.demo_target.phone.update(row); else ctx.db.demo_target.insert(row);
  m.capture('demo_target', key, previous, row);
};

export const upsertCompany: Handler = m => {
  const { ctx, p } = m;
  const id = text(p, 'id');
  const previous = ctx.db.company.id.find(id);
  const row = { id, kind: choice(p, 'kind', ['carrier', 'shipper', 'broker', 'other'], previous?.kind ?? 'carrier'), name: text(p, 'name', previous?.name), usdot: text(p, 'usdot', previous?.usdot ?? ''), mc: text(p, 'mc', previous?.mc ?? ''), phone: text(p, 'phone', previous?.phone ?? ''), website: text(p, 'website', previous?.website ?? ''), source_url: text(p, 'source_url', previous?.source_url ?? '') };
  if (row.phone) phone(row.phone);
  if (previous) ctx.db.company.id.update(row); else ctx.db.company.insert(row);
  m.capture('company', id, previous, row);
};

// sec_edgar: a shipper's SEC registration. fixture: a labeled stand-in, never a real-world record.
const CONTACT_SOURCES = ['fmcsa', 'sec_edgar', 'own_key', 'context_dev', 'website', 'fixture'];

export const upsertContact: Handler = m => {
  const { ctx, p } = m;
  const id = text(p, 'id');
  const previous = ctx.db.contact.id.find(id);
  const company_id = text(p, 'company_id', previous?.company_id);
  must(ctx.db.company.id.find(company_id), 'contact company does not exist');
  if (previous && previous.company_id !== company_id) throw new Error('contact company is immutable');
  const row = { id, company_id, name: text(p, 'name', previous?.name), title: text(p, 'title', previous?.title ?? ''), phone: text(p, 'phone', previous?.phone ?? ''), email: text(p, 'email', previous?.email ?? ''), linkedin_url: text(p, 'linkedin_url', previous?.linkedin_url ?? ''), source: choice(p, 'source', CONTACT_SOURCES, previous?.source ?? 'website'), source_url: text(p, 'source_url', previous?.source_url ?? ''), confidence: probability(p, 'confidence', previous?.confidence ?? 0), alternates_json: p.alternates_json === undefined ? previous?.alternates_json ?? '[]' : json(p, 'alternates_json', []) };
  if (row.phone) phone(row.phone);
  if (previous) ctx.db.contact.id.update(row); else ctx.db.contact.insert(row);
  m.capture('contact', id, previous, row);
};

const SOURCE_KINDS = ['case', 'company', 'contacts', 'dataset'];

// Source truth agents read in place of source.json, one row per record. The digest is SHA-256 of
// record_json exactly as sent; an unchanged digest is a no-op, so backfills add no audit history.
export const upsertSourceRecord: Handler = m => {
  const { ctx, p } = m;
  const id = text(p, 'id');
  const kind = choice(p, 'kind', SOURCE_KINDS);
  if (!id.startsWith(`${kind}:`) || id.length === kind.length + 1) throw new Error('source record id must be <kind>:<key>');
  const record_json = text(p, 'record_json', undefined, 262144);
  let record: unknown;
  try { record = JSON.parse(record_json); } catch { throw new Error('record_json must be valid JSON'); }
  const array = kind === 'contacts';
  if (!record || typeof record !== 'object' || Array.isArray(record) !== array) throw new Error(`${kind} record_json must be a JSON ${array ? 'array' : 'object'}`);
  const digest = text(p, 'digest');
  if (hash(record_json) !== digest) throw new Error('digest does not match record_json');
  // Case records chain under their case; directory and dataset records under the system chain.
  const case_id = kind === 'case' ? id.slice('case:'.length) : '';
  if (p.case_id !== undefined && p.case_id !== case_id) throw new Error('source record case is derived from its id');
  p.case_id = case_id;
  const previous = ctx.db.source_record.id.find(id);
  if (previous?.digest === digest) { m.unchanged = true; return; }
  const row = { id, kind, case_id, record_json, digest, updated_at: now(ctx) };
  if (previous) ctx.db.source_record.id.update(row); else ctx.db.source_record.insert(row);
  m.capture('source_record', id, previous, row);
};

export const upsertCall: Handler = m => {
  const { ctx, p, actor } = m;
  const id = text(p, 'id');
  const previous = ctx.db.call.id.find(id);
  const case_id = text(p, 'case_id', previous?.case_id);
  caseFor(ctx, case_id, actor, !!previous);
  const task = ownedTask(ctx, text(p, 'task_id', previous?.task_id), actor);
  if (task.case_id !== case_id) throw new Error('call task belongs to another case');
  const company_id = text(p, 'company_id', previous?.company_id);
  const company = must(ctx.db.company.id.find(company_id), 'call company does not exist');
  const contact_id = text(p, 'contact_id', previous?.contact_id ?? '');
  const contact = contact_id ? must(ctx.db.contact.id.find(contact_id), 'contact does not exist') : null;
  if (contact && (contact.company_id !== company_id || contact.confidence < 0.7 || !contact.source_url)) throw new Error('P-008: contact is not verified for this company');
  if (!contact && !company.source_url) throw new Error('P-008: company phone must have a verified source');
  const intended_to = phone(contact?.phone || company.phone);
  if (p.intended_to !== undefined && p.intended_to !== intended_to) throw new Error('P-008: intended number differs from verified directory');
  const routed_to = phone(text(p, 'routed_to', previous?.routed_to));
  if (!ctx.db.demo_target.phone.find(routed_to)?.enabled) throw new Error('P-011: routed number is not on the demo allowlist');
  if (previous && (previous.case_id !== case_id || previous.task_id !== task.task_id || previous.company_id !== company_id || previous.contact_id !== contact_id || previous.intended_to !== intended_to || previous.routed_to !== routed_to)) throw new Error('call routing and ownership are immutable');
  const persona = text(p, 'persona', previous?.persona ?? 'verification');
  const direction = choice(p, 'direction', ['outbound', 'inbound'], previous?.direction ?? 'outbound');
  const status = choice(p, 'status', ['queued', 'ringing', 'in_progress', 'completed', 'no_answer', 'failed', 'cancelled'], previous?.status ?? 'queued');
  // This assertion comes only from the authenticated Voice service after it
  // consumes an operator's durable, task-bound demo grant. It cannot relax
  // directory/routing controls or authorize ordinary counterparty calls.
  const teamDemo = bool(p, 'team_demo_authorized', false);
  if (teamDemo && (actor.role !== 'voice_desk' || task.skill !== 'place_call'
      || case_id !== 'SYN-CARTESIA-TEAM-CALL' || company_id !== 'DEMO-TEAM'
      || persona !== 'verification' || direction !== 'outbound'
      || company.source_url !== 'operator://explicit-team-demo-destination'
      || company.phone !== intended_to || intended_to !== routed_to)) {
    throw new Error('team demo authorization does not match the operator team call scope');
  }
  if (!previous) {
    const companyCalls = [...ctx.db.call.company_id.filter(company_id)];
    if (company_id === 'DEMO-TEAM' && companyCalls.some(x => ['queued', 'ringing', 'in_progress'].includes(x.status))) {
      throw new Error('an active team demo call must finish before another can start');
    }
    if (teamDemo) {
      if (status !== 'queued') throw new Error('an authorized team demo call must start queued');
      p.reason_code = 'operator_team_demo_call';
    } else {
      const cutoff = Number(ctx.timestamp.microsSinceUnixEpoch / 1000n) - 3600000;
      if (companyCalls.some(x => Date.parse(x.started_at) >= cutoff)) throw new Error('at most one call attempt per company per hour');
    }
  }
  if (previous && ['completed', 'no_answer', 'failed', 'cancelled'].includes(previous.status) && status !== previous.status) throw new Error('terminal call cannot restart');
  const row = {
    id, case_id, task_id: task.task_id, persona, direction,
    to_masked: `${intended_to.slice(0, 3)}…${intended_to.slice(-4)}`, intended_to, routed_to, company_id, contact_id, status,
    started_at: previous?.started_at ?? now(ctx), ended_at: ['completed', 'no_answer', 'failed', 'cancelled'].includes(status) ? previous?.ended_at || now(ctx) : '',
    findings_json: p.findings_json === undefined ? previous?.findings_json ?? '{}' : json(p, 'findings_json'),
    transcript_redacted: text(p, 'transcript_redacted', previous?.transcript_redacted ?? '', 100000), recording_url: text(p, 'recording_url', previous?.recording_url ?? ''), disclosure_given: bool(p, 'disclosure_given', previous?.disclosure_given ?? false),
  };
  if (previous) ctx.db.call.id.update(row); else ctx.db.call.insert(row);
  m.capture('call', id, previous, row);
};

export const addPrecedent: Handler = m => {
  const { ctx, p, actor } = m;
  const source_case = text(p, 'source_case');
  caseFor(ctx, source_case, actor);
  const row = { id: text(p, 'id'), carrier_id: text(p, 'carrier_id'), claim_type: text(p, 'claim_type'), lane: text(p, 'lane', ''), pattern_json: json(p, 'pattern_json'), outcome: text(p, 'outcome'), as_of: now(ctx), source_case };
  ctx.db.precedent.insert(row);
  m.capture('precedent', row.id, null, row);
  p.case_id = source_case;
};

type Dispatch = NonNullable<ReturnType<Ctx['db']['dispatch']['event_id']['find']>>;
const ROUTED = ['queued', 'accepted', 'duplicate'];

// One row per event. Once routed, its case, agent and skill are fixed, its task is fixed once known,
// and later observations keep the original queue time and policy version. A queued event stays queued
// until a task is known; a re-observed accepted event becomes a duplicate.
function writeDispatch(m: Mutation, row: Dispatch): void {
  const { ctx } = m;
  const previous = ctx.db.dispatch.event_id.find(row.event_id);
  const requested = row.status;
  if (row.agent && !SKILLS[row.agent].includes(row.skill)) throw new Error('dispatch skill does not belong to agent');
  if (ROUTED.includes(requested) && (!row.case_id || !row.agent || !row.skill)) throw new Error(`${requested} dispatch requires case, agent and skill`);
  if (requested === 'accepted' && !row.task_id) throw new Error('accepted dispatch requires a task');
  if (requested === 'queued' && row.task_id) throw new Error('queued dispatch cannot carry a task');
  if (previous && ROUTED.includes(previous.status)) {
    if (!ROUTED.includes(requested) ||
        ['case_id', 'agent', 'skill'].some(key => previous[key as keyof Dispatch] !== row[key as keyof Dispatch])) {
      throw new Error('routed dispatch route is immutable');
    }
    if (previous.task_id && row.task_id && previous.task_id !== row.task_id) throw new Error('accepted dispatch task is immutable');
    if (previous.task_id && requested === 'queued') throw new Error('accepted dispatch cannot return to queued');
    row.task_id ||= previous.task_id;
    if (previous.status !== 'queued') row.status = 'duplicate';
    else if (requested === 'duplicate' && !row.task_id) row.status = 'queued';
    row.queue_ms = previous.queue_ms;
    row.policy_version = previous.policy_version;
  }
  if (previous) ctx.db.dispatch.event_id.update(row); else ctx.db.dispatch.insert(row);
  m.capture('dispatch', row.event_id, previous, row);
}

export const recordDispatch: Handler = m => {
  const { ctx, p } = m;
  writeDispatch(m, {
    event_id: text(p, 'event_id'), case_id: text(p, 'case_id', ''), skill: text(p, 'skill', ''),
    agent: choice(p, 'agent', ['', ...AGENTS], ''), task_id: text(p, 'task_id', ''),
    status: choice(p, 'status', ['queued', 'accepted', 'triage', 'not_routed', 'duplicate']),
    reason: text(p, 'reason'), queue_ms: uint(p, 'queue_ms'), policy_version: text(p, 'policy_version'), ts: now(ctx),
  });
};

// The router commits a routed event here instead of pushing it over A2A: the outbox row and its
// queued dispatch land in one transaction, and the dispatch worker delivers the row to the agent.
export const enqueueFirstHop: Handler = m => {
  const { ctx, p, actor } = m;
  const event_id = text(p, 'event_id');
  const case_id = text(p, 'case_id');
  caseFor(ctx, case_id, actor);
  const agent = choice(p, 'agent', AGENTS);
  const skill = text(p, 'skill');
  if (!SKILLS[agent].includes(skill)) throw new Error('first-hop skill does not belong to agent');
  const envelope = p.envelope;
  if (!envelope || Array.isArray(envelope) || typeof envelope !== 'object') throw new Error('envelope must be a JSON object');
  // The worker rebuilds the A2A task from this envelope, so it must describe the same route.
  if (envelope.case_id !== case_id || envelope.task_type !== skill) throw new Error('envelope case and task type must match the first hop');
  const queue_ms = uint(p, 'queue_ms');
  const policy_version = text(p, 'policy_version');
  const id = `first_hop:${event_id}`;
  const body = { event_id, case_id, skill, agent, envelope, queue_ms, policy_version };
  const existing = ctx.db.routing_outbox.id.find(id);
  if (existing && existing.payload_json !== canonical(body)) throw new Error('first hop for this event was already enqueued with different contents');
  writeDispatch(m, { event_id, case_id, skill, agent, task_id: '', status: 'queued', reason: text(p, 'reason'), queue_ms, policy_version, ts: now(ctx) });
  enqueue(m, id, 'first_hop', case_id, '', body, agent);
};

export const logLlmCall: Handler = m => {
  const { ctx, p } = m;
  const row = { id: text(p, 'id'), ts: now(ctx), agent: choice(p, 'agent', [...AGENTS, 'router', 'ingest']), model: text(p, 'model'), latency_ms: uint(p, 'latency_ms'), tokens: uint(p, 'tokens'), route_reason: text(p, 'route_reason', ''), ensemble_agreement: bool(p, 'ensemble_agreement') };
  ctx.db.llm_call.insert(row);
  m.capture('llm_call', row.id, null, row);
};

export const setMetric: Handler = m => {
  const { ctx, p } = m;
  const name = text(p, 'name');
  const previous = ctx.db.metric.name.find(name);
  const row = { name, value: num(p, 'value'), updated_at: now(ctx) };
  if (previous) ctx.db.metric.name.update(row); else ctx.db.metric.insert(row);
  m.capture('metric', name, previous, row);
};

export const setAgentModel: Handler = m => {
  const { ctx, p } = m;
  const agent = choice(p, 'agent', AGENTS);
  const previous = ctx.db.agent_config.agent.find(agent);
  const row = { agent, model: text(p, 'model', 'auto'), enabled: bool(p, 'enabled') };
  if (previous) ctx.db.agent_config.agent.update(row); else ctx.db.agent_config.insert(row);
  m.capture('agent_config', agent, previous, row);
};

// init seeds agent_config once; agents added after a database was initialised get the same
// defaults here. Existing rows, including human-disabled agents, are left alone.
export const ensureAgentDefaults: Handler = m => {
  const { ctx } = m;
  for (const agent of AGENTS) {
    if (ctx.db.agent_config.agent.find(agent)) continue;
    const config = { agent, model: 'auto', enabled: true };
    ctx.db.agent_config.insert(config);
    m.capture('agent_config', agent, null, config);
  }
  m.unchanged = !m.cells.length;
};

export const upsertForecast: Handler = m => {
  const { ctx, p, actor } = m;
  const id = text(p, 'id');
  const previous = ctx.db.cash_forecast.id.find(id);
  const case_id = text(p, 'case_id', previous?.case_id);
  caseFor(ctx, case_id, actor);
  if (previous && previous.case_id !== case_id) throw new Error('forecast case is immutable');
  const row = { id, case_id, period: text(p, 'period', previous?.period), category: text(p, 'category', previous?.category ?? 'cash'), amount: amount(p, 'amount', undefined, true), confidence: probability(p, 'confidence', previous?.confidence ?? 0), assumptions_json: json(p, 'assumptions_json'), updated_at: now(ctx) };
  if (previous) ctx.db.cash_forecast.id.update(row); else ctx.db.cash_forecast.insert(row);
  m.capture('cash_forecast', id, previous, row);
};

export const addReview: Handler = m => {
  const { ctx, p } = m;
  const task = must(ctx.db.task.task_id.find(text(p, 'target_task_id')), 'review target task does not exist');
  if (!TERMINAL.includes(task.a2a_state)) throw new Error('only terminal tasks can receive output reviews');
  const case_id = text(p, 'case_id', task.case_id);
  if (case_id !== task.case_id) throw new Error('review case differs from target task');
  const actions = list(p, 'actions');
  if (actions.some(x => !['none', 'reopen_case', 'rerun_task'].includes(x))) throw new Error('unknown review action');
  const row = { id: text(p, 'id'), target_task_id: task.task_id, case_id, verdict: choice(p, 'verdict', ['pass', 'issue']), checks_json: json(p, 'checks_json', []), friction_json: json(p, 'friction_json', []), ts: now(ctx), actions_json: canonical(actions) };
  ctx.db.review.insert(row);
  m.capture('review', row.id, null, row);
  if (row.verdict === 'issue' && actions.some(x => x !== 'none')) {
    const c = must(ctx.db.case.case_id.find(case_id), 'case does not exist');
    if (c.status !== 'human_owned') {
      // One deterministic request per original task prevents review/retry loops.
      enqueue(m, `review_followup:${task.task_id}`, 'review_followup', case_id, task.task_id, { target_task_id: task.task_id, case_id, agent: task.agent, actions, review_id: row.id }, task.agent);
    }
  }
};

export const upsertMemory: Handler = m => {
  const { ctx, p, actor } = m;
  const id = text(p, 'id');
  const previous = ctx.db.agent_memory.id.find(id);
  const agent = choice(p, 'agent', AGENTS, previous?.agent);
  if (previous && previous.agent !== agent) throw new Error('memory agent is immutable');
  const scopeText = p.scope === undefined ? previous?.scope ?? '{}' : json(p, 'scope');
  const scope: unknown = JSON.parse(scopeText);
  if (!scope || Array.isArray(scope) || typeof scope !== 'object' || Object.keys(scope).some(key => !['carrier_id', 'lane', 'claim_type', 'skill'].includes(key)) || Object.values(scope).some(value => typeof value !== 'string')) throw new Error('P-012: memory scope can contain only carrier_id, lane, claim_type, skill strings');
  const source_ref = text(p, 'source_ref', previous?.source_ref);
  if (actor.role === 'validator' && !ctx.db.review.id.find(source_ref) && !ctx.db.flag.id.find(source_ref)) throw new Error('validator lessons must cite an existing review or flag');
  const row = { id, agent, scope: scopeText, text: text(p, 'text', previous?.text, 32000), source: actor.role === 'human' ? 'human' : 'validator', source_ref, version: (previous?.version ?? 0) + 1, active: bool(p, 'active'), created_at: now(ctx) };
  if (previous) ctx.db.agent_memory.id.update(row); else ctx.db.agent_memory.insert(row);
  const revision = { ...row, id: `${id}:v${row.version}`, memory_id: id };
  ctx.db.memory_revision.insert(revision);
  m.capture('agent_memory', id, previous, row);
  m.capture('memory_revision', revision.id, null, revision);
};

function writeInstruction(m: Mutation, agent: string, diff: string, reason: string, source_ref: string, reverts_version = 0): void {
  const { ctx, actor } = m;
  const versions = [...ctx.db.instruction_version.agent.filter(agent)];
  const version = versions.reduce((max, row) => Math.max(max, row.version), 0) + 1;
  for (const previous of versions.filter(x => x.active)) {
    const row = { ...previous, active: false };
    ctx.db.instruction_version.id.update(row);
    m.capture('instruction_version', row.id, previous, row);
  }
  const row = { id: `${agent}:${version}`, agent, version, diff, reason, author: actor.role === 'human' ? 'human' : 'validator', source_ref, created_at: now(ctx), active: true, reverts_version };
  ctx.db.instruction_version.insert(row);
  m.capture('instruction_version', row.id, null, row);
}

function checkInstructionVersion(m: Mutation, agent: string): void {
  if (m.p.expected_version === undefined || m.p.expected_version === null) throw new Error('expected_version is required');
  const latest = [...m.ctx.db.instruction_version.agent.filter(agent)]
    .reduce((max, row) => Math.max(max, row.version), 0);
  if (uint(m.p, 'expected_version') !== latest) throw new Error('instruction version conflict');
}

// Automated learning is append-only: one blank line plus either the code-owned advisory template
// or one bounded model-written "Learned:" line. Nothing can remove instructions or touch controls.
const ADVISORY_TEMPLATE = 'Before completing a result, cite the source records and recheck monetary totals in exact cents.';
const LEARNED_LINE = /^Learned: [\x20-\x7E]{10,300}$/;
const CONTROL_WORDS = /\b(ignore|disregard|override|bypass|disable|allowlist|permissions?|credentials?|passwords?|tokens?|api keys?)\b/i;
function validateAutomatedInstruction(diff: string): void {
  const match = /^--- a\/instructions\.md\n\+\+\+ b\/instructions\.md\n@@ -(\d+),0 \+(\d+),2 @@\n\+\n\+([^\n]*)\n$/.exec(diff);
  const line = match?.[3] ?? '';
  const allowed = line === ADVISORY_TEMPLATE || (LEARNED_LINE.test(line) && !CONTROL_WORDS.test(line));
  if (!match || Number(match[2]) !== Number(match[1]) + 1 || !allowed) {
    throw new Error('P-012: validator instructions must be one approved append-only line');
  }
}

export const publishInstruction: Handler = m => {
  const { ctx, p, actor } = m;
  const source_ref = text(p, 'source_ref');
  if (actor.role === 'validator' && !ctx.db.review.id.find(source_ref) && !ctx.db.flag.id.find(source_ref)) throw new Error('validator instruction changes must cite an existing review or flag');
  const agent = choice(p, 'agent', AGENTS);
  checkInstructionVersion(m, agent);
  const diff = text(p, 'diff', undefined, 32000);
  if (actor.role === 'validator') validateAutomatedInstruction(diff);
  writeInstruction(m, agent, diff, text(p, 'reason'), source_ref);
};

export const revertInstruction: Handler = m => {
  const { ctx, p } = m;
  const agent = choice(p, 'agent', AGENTS);
  const version = uint(p, 'version');
  checkInstructionVersion(m, agent);
  const target = version === 0 ? null : must(ctx.db.instruction_version.id.find(`${agent}:${version}`), 'instruction version does not exist');
  writeInstruction(m, agent, target?.diff ?? '', text(p, 'reason'), target?.id ?? `${agent}:0`, version);
};

export const raiseFlag: Handler = m => {
  const { ctx, p, actor } = m;
  const case_id = text(p, 'case_id', '');
  if (case_id) caseFor(ctx, case_id, actor, true);
  const row = { id: text(p, 'id'), target_ref: text(p, 'target_ref'), case_id, kind: choice(p, 'kind', ['wrong', 'unclear', 'policy', 'praise', 'other']), note: text(p, 'note', '', 16000), raised_by: actor.actor, status: 'open', resolution_ref: '', ts: now(ctx) };
  ctx.db.flag.insert(row);
  m.capture('flag', row.id, null, row);
  enqueue(m, `review_flag:${row.id}`, 'review_flag', case_id, '', { flag_id: row.id, target_ref: row.target_ref, case_id });
};

export const resolveFlag: Handler = m => {
  const { ctx, p } = m;
  const previous = must(ctx.db.flag.id.find(text(p, 'id')), 'flag does not exist');
  if (['resolved', 'rejected'].includes(previous.status)) throw new Error('flag has already been resolved');
  const status = choice(p, 'status', ['reviewing', 'resolved', 'rejected']);
  const resolution_ref = text(p, 'resolution_ref', '');
  if (status !== 'reviewing' && (!resolution_ref || (!ctx.db.review.id.find(resolution_ref) && !ctx.db.agent_memory.id.find(resolution_ref) && !ctx.db.instruction_version.id.find(resolution_ref) && !ctx.db.change_set.id.find(resolution_ref)))) throw new Error('resolved flag must cite an existing review, lesson or change set');
  const row = { ...previous, status, resolution_ref };
  ctx.db.flag.id.update(row);
  m.capture('flag', row.id, previous, row);
  p.case_id = row.case_id;
};

export const claimOutbox: Handler = m => {
  const { ctx, p, actor } = m;
  const previous = must(ctx.db.routing_outbox.id.find(text(p, 'id')), 'outbox item does not exist');
  if (previous.status === 'done') throw new Error('outbox item already delivered');
  if (previous.status === 'processing' && Date.parse(previous.lease_expires_at) > Number(ctx.timestamp.microsSinceUnixEpoch / 1000n)) throw new Error('outbox item has an active delivery lease');
  const seconds = uint(p, 'lease_seconds', 60);
  if (seconds < 5 || seconds > 300) throw new Error('lease must be 5–300 seconds');
  const row = { ...previous, status: 'processing', attempts: previous.attempts + 1, claimed_by: actor.identity, lease_expires_at: timestampAfter(ctx, seconds), updated_at: now(ctx), last_error: '' };
  ctx.db.routing_outbox.id.update(row);
  m.capture('routing_outbox', row.id, previous, row);
};

export const ackOutbox: Handler = m => {
  const { ctx, p, actor } = m;
  const previous = must(ctx.db.routing_outbox.id.find(text(p, 'id')), 'outbox item does not exist');
  if (previous.status !== 'processing' || previous.claimed_by !== actor.identity || Date.parse(previous.lease_expires_at) <= Number(ctx.timestamp.microsSinceUnixEpoch / 1000n)) throw new Error('caller does not hold a current outbox lease');
  const success = bool(p, 'success');
  const row = { ...previous, status: success ? 'done' : 'pending', claimed_by: '', lease_expires_at: '', updated_at: now(ctx), last_error: success ? '' : text(p, 'error', 'delivery failed') };
  ctx.db.routing_outbox.id.update(row);
  m.capture('routing_outbox', row.id, previous, row);
};
