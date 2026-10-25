import test from 'node:test';
import assert from 'node:assert/strict';
import { canonical, hash, type Payload } from '../src/policy';
import type { Ctx } from '../src/schema';
import * as op from '../src/operations';

// Unit harness exercises the same reducer handlers with an atomic in-memory
// transaction boundary. scripts/spacetime_smoke.py separately checks the real
// SpacetimeDB 2.10 HTTP/auth/storage integration.
type Row = Record<string, unknown>;
const primaryKeys: Record<string, string> = {
  dispatch: 'event_id', case: 'case_id', task: 'task_id', agent: 'name', agent_config: 'agent', ledger_entry: 'id', hold: 'id',
  payment_proposal: 'id', approval: 'id', audit_event: 'id', call: 'id', precedent: 'id', llm_call: 'id', metric: 'name',
  change_set: 'id', cell_change: 'id', doc_read: 'id', review: 'id', agent_memory: 'id', instruction_version: 'id',
  flag: 'id', company: 'id', contact: 'id', delegation_edge: 'id', invoice: 'id', accounting_period: 'period',
  principal: 'identity', cash_forecast: 'id', journal_request: 'id', routing_outbox: 'id', memory_revision: 'id',
  demo_target: 'phone', approval_secret: 'approval_id', write_receipt: 'id', chain_head: 'name',
};
const identities: Record<string, string> = {};
['admin', 'human', 'controller', 'carrier_ap', 'shipper_ar', 'cfo', 'risk', 'verification', 'voice_desk', 'validator', 'documents', 'ingest', 'harness', 'evaluator', 'router', 'stranger'].forEach((name, i) => identities[name] = (i + 1).toString(16).padStart(64, '0'));

class Harness {
  data: Record<string, Map<unknown, Row>> = {};
  micros = 1792857600000000n;
  serial = 0;
  ctx: Ctx;
  caller = 'admin';
  constructor() {
    const db: Record<string, unknown> = {};
    for (const [name, pk] of Object.entries(primaryKeys)) {
      this.data[name] = new Map();
      db[name] = new Proxy({}, { get: (_target, property: string) => {
        if (property === 'insert') return (row: Row) => {
          if (this.data[name].has(row[pk])) throw new Error(`duplicate ${name} primary key`);
          this.data[name].set(row[pk], structuredClone(row));
          return structuredClone(row);
        };
        if (property === 'iter') return () => [...this.data[name].values()].map(x => structuredClone(x))[Symbol.iterator]();
        return {
          find: (value: unknown) => [...this.data[name].values()].find(row => row[property] === value) ?? null,
          filter: (value: unknown) => [...this.data[name].values()].filter(row => row[property] === value)[Symbol.iterator](),
          update: (row: Row) => {
            if (!this.data[name].has(row[pk])) throw new Error(`missing ${name} primary key`);
            this.data[name].set(row[pk], structuredClone(row));
          },
          delete: (value: unknown) => this.data[name].delete(value),
        };
      } });
    }
    const h = this;
    this.ctx = {
      db, sender: { toHexString: () => identities[this.caller] },
      timestamp: { get microsSinceUnixEpoch() { return h.micros; }, toISOString: () => new Date(Number(this.micros / 1000n)).toISOString() },
    } as unknown as Ctx;
    op.initialize(this.ctx);
    for (const role of Object.keys(identities).filter(x => !['admin', 'stranger'].includes(x))) {
      this.run('admin', 'grant_role', ['admin'], op.grantRole, { identity: identities[role], role, actor: role === 'human' ? 'human:operator' : role });
    }
  }
  run(actor: string, action: string, roles: readonly string[], handler: Parameters<typeof op.execute>[4], payload: Payload) {
    this.caller = actor;
    const p = { write_id: `unit-${++this.serial}`, ...payload };
    const snapshot = structuredClone(this.data);
    try { op.execute(this.ctx, JSON.stringify(p), action, roles, handler); }
    catch (error) { this.data = snapshot; throw error; }
    this.micros += 1000n;
    return p;
  }
  row(table: string, id: string): Row { return assertPresent(this.data[table].get(id)); }
  rows(table: string): Row[] { return [...this.data[table].values()]; }
  open(id = 'CASE-1', total = 9400) {
    this.run('ingest', 'open_case', ['ingest'], op.openCase, { case_id: id, load_id: `LOAD-${id}`, title: 'Test case', exposure_usd: total });
    this.run('ingest', 'upsert_invoice', ['ingest'], op.upsertInvoice, { id: `INV-${id}`, case_id: id, carrier_id: 'C-1', load_id: `LOAD-${id}`, amount: total, contract_amount: total });
  }
  hold(id = 'HOLD-1', value = 9400, caseId = 'CASE-1') {
    return this.run('controller', 'place_hold', ['controller'], op.placeHold, { id, case_id: caseId, invoice_id: `INV-${caseId}`, amount: value, reason_code: 'P-001' });
  }
  approval(id = 'APPROVAL-1', action = 'release_hold', target = 'HOLD-1', invoice = 'INV-CASE-1', value = 9400) {
    this.run('controller', 'request_approval', ['controller'], op.requestApproval, { id, case_id: 'CASE-1', action, target_id: target, invoice_id: invoice, amount: value, approver: identities.human });
    this.run('admin', 'issue_approval_code', ['admin'], op.issueApprovalCode, { approval_id: id, code: '839271', expires_in_seconds: 300 });
  }
  decide(id = 'APPROVAL-1', code = '839271', decision = 'approved') {
    return this.run('human', 'decide_approval', ['human'], op.decideApproval, { id, code, decision });
  }
}
function assertPresent<T>(value: T | undefined): T { assert.notEqual(value, undefined); return value as T; }
const lines = [{ account: 'Expense', debit: 9400, credit: 0 }, { account: 'AP', debit: 0, credit: 9400 }];

test('harness can log intake model calls without granting intake writers telemetry access', () => {
  const h = new Harness();
  const payload = { model: 'intake-classifier', latency_ms: 12, tokens: 34, route_reason: 'classify event', ensemble_agreement: false };
  for (const agent of ['router', 'ingest', 'documents', 'carrier_ap']) {
    h.run('harness', 'log_llm_call', ['harness'], op.logLlmCall, { ...payload, id: `LLM-${agent}`, agent });
    assert.equal(h.row('llm_call', `LLM-${agent}`).agent, agent);
  }
  assert.throws(() => h.run('harness', 'log_llm_call', ['harness'], op.logLlmCall, { ...payload, id: 'invalid', agent: 'unknown' }));
  for (const actor of ['router', 'ingest']) {
    assert.throws(() => h.run(actor, 'log_llm_call', ['harness'], op.logLlmCall, { ...payload, id: `unauthorized-${actor}`, agent: actor }));
  }
  assert.equal(h.rows('llm_call').length, 4);
});

test('sender auth cannot be replaced by caller payload or admin privilege', () => {
  const h = new Harness();
  h.open();
  for (const actor of ['stranger', 'risk', 'admin', 'human']) {
    assert.throws(() => h.run(actor, 'place_hold', ['controller'], op.placeHold, { id: `H-${actor}`, case_id: 'CASE-1', invoice_id: 'INV-CASE-1', amount: 1, reason_code: 'P-001', actor: 'controller' }));
  }
  assert.equal(h.rows('hold').length, 0);
});

test('write IDs retry exactly once and reject changed content', () => {
  const h = new Harness(); h.open(); h.hold('HOLD-1', 100);
  const p = h.run('controller', 'release_hold', ['controller'], op.releaseHold, { id: 'HOLD-1', amount: 50 });
  const count = h.rows('change_set').length;
  h.run('controller', 'release_hold', ['controller'], op.releaseHold, p);
  assert.equal(h.rows('change_set').length, count);
  assert.equal(h.row('hold', 'HOLD-1').released_amount, 50);
  assert.throws(() => h.run('controller', 'release_hold', ['controller'], op.releaseHold, { ...p, amount: 49 }));
});

test('hold and payment controls read persisted invoice and reject fractional cents', () => {
  const h = new Harness(); h.open();
  for (const value of [9400.01, 1.001, -1]) assert.throws(() => h.hold(`H-${value}`, value));
  h.hold('HOLD-1', 300);
  assert.throws(() => h.run('carrier_ap', 'propose_payment', ['carrier_ap'], op.proposePayment, { id: 'PAY-1', case_id: 'CASE-1', invoice_id: 'INV-CASE-1', amount: 100, on_hold: false }));
  assert.equal(h.rows('payment_proposal').length, 0);
});

test('OTP failures persist, lock after five attempts, and never leak into audit', () => {
  const h = new Harness(); h.open(); h.hold(); h.approval();
  for (let i = 0; i < 5; i++) h.decide('APPROVAL-1', `wrong-${i}`);
  assert.equal(h.row('approval', 'APPROVAL-1').failed_attempts, 5);
  assert.equal(h.row('approval', 'APPROVAL-1').last_error, 'code_locked');
  assert.throws(() => h.decide());
  const publicRows = Object.keys(primaryKeys).filter(x => !['approval_secret', 'write_receipt', 'chain_head'].includes(x)).flatMap(x => h.rows(x));
  const serialized = canonical(publicRows);
  assert.ok(!serialized.includes('839271'));
  assert.ok(!serialized.includes(String(h.row('approval_secret', 'APPROVAL-1').code_hash)));
});

test('large releases require a designated human, exact approval, and consume once', () => {
  const h = new Harness(); h.open(); h.hold();
  assert.throws(() => h.run('controller', 'release_hold', ['controller'], op.releaseHold, { id: 'HOLD-1', amount: 9400, code_ok: true }));
  assert.throws(() => h.run('controller', 'release_hold', ['controller'], op.releaseHold, { id: 'HOLD-1', amount: 4700 }), /approval/);
  h.approval();
  assert.throws(() => h.run('controller', 'decide_approval', ['human'], op.decideApproval, { id: 'APPROVAL-1', code: '839271', decision: 'approved' }));
  h.decide();
  const count = h.rows('change_set').length;
  assert.throws(() => h.run('controller', 'release_hold', ['controller'], op.releaseHold, { id: 'HOLD-1', amount: 9399, approval_id: 'APPROVAL-1' }));
  assert.equal(h.rows('change_set').length, count);
  h.run('controller', 'release_hold', ['controller'], op.releaseHold, { id: 'HOLD-1', amount: 9400, approval_id: 'APPROVAL-1' });
  assert.equal(h.row('hold', 'HOLD-1').status, 'released');
  assert.ok(h.row('approval', 'APPROVAL-1').consumed_at);
  assert.throws(() => h.decide());
});

const release = (h: Harness, id: string, amount: number, approval_id?: string) =>
  h.run('controller', 'release_hold', ['controller'], op.releaseHold, approval_id ? { id, amount, approval_id } : { id, amount });

test('P-003 split attack: sub-$5,000 holds on one invoice cannot release over $5,000 without a human', () => {
  const h = new Harness(); h.open(); h.hold('HOLD-A', 4700); h.hold('HOLD-B', 4700);
  assert.throws(() => release(h, 'HOLD-A', 4700), /P-003/);
  assert.throws(() => release(h, 'HOLD-B', 100), /P-003/);
  // The approval binds hold, invoice, case, action and exact amount.
  h.approval('APP-A', 'release_hold', 'HOLD-A', 'INV-CASE-1', 4700); h.decide('APP-A');
  assert.throws(() => release(h, 'HOLD-B', 4700, 'APP-A'), /different action, target/);
  assert.throws(() => release(h, 'HOLD-A', 4600, 'APP-A'), /different action, target/);
  assert.equal(h.row('approval', 'APP-A').consumed_at, '');
  release(h, 'HOLD-A', 4700, 'APP-A');
  assert.equal(h.row('hold', 'HOLD-A').status, 'released');
  assert.ok(h.row('approval', 'APP-A').consumed_at);
  assert.throws(() => release(h, 'HOLD-B', 4700, 'APP-A'), /P-003/);
  assert.equal(h.row('hold', 'HOLD-B').released_amount, 0);
});

test('P-003 sequential split: released holds still count toward the invoice total', () => {
  const h = new Harness(); h.open();
  h.hold('HOLD-A', 4000); release(h, 'HOLD-A', 4000);
  h.hold('HOLD-B', 4000);
  assert.throws(() => release(h, 'HOLD-B', 4000), /P-003/);
  assert.throws(() => release(h, 'HOLD-B', 1000), /P-003/);
  assert.equal(h.row('hold', 'HOLD-B').released_amount, 0);
  // Another invoice keeps its own total.
  h.open('CASE-2'); h.hold('HOLD-C', 4000, 'CASE-2'); release(h, 'HOLD-C', 4000);
  assert.equal(h.row('hold', 'HOLD-C').status, 'released');
});

test('P-003 shrink attack: a large hold cannot be reduced below the threshold and then released', () => {
  const h = new Harness(); h.open('CASE-1', 12000); h.hold('HOLD-1', 9400);
  assert.throws(() => release(h, 'HOLD-1', 4400), /P-003/);
  assert.throws(() => h.hold('HOLD-1', 100), /duplicate/);
  h.approval('APP-1', 'release_hold', 'HOLD-1', 'INV-CASE-1', 4500); h.decide('APP-1');
  release(h, 'HOLD-1', 4500, 'APP-1');
  assert.equal(h.row('hold', 'HOLD-1').released_amount, 4500);
  // $4,900 remains, under the threshold, but the invoice has held $9,400.
  assert.throws(() => release(h, 'HOLD-1', 4900), /P-003/);
  assert.throws(() => release(h, 'HOLD-1', 4900, 'APP-1'), /P-003/);
  h.hold('HOLD-2', 100);
  assert.throws(() => release(h, 'HOLD-2', 100), /P-003/);
  assert.equal(h.row('hold', 'HOLD-1').released_amount, 4500);
});

test('P-003 legitimate small releases need no approval while the invoice holds $5,000 or less', () => {
  const h = new Harness(); h.open(); h.hold('HOLD-A', 300); h.hold('HOLD-B', 4700);
  release(h, 'HOLD-A', 100); release(h, 'HOLD-A', 200); release(h, 'HOLD-B', 4700);
  assert.equal(h.row('hold', 'HOLD-A').status, 'released');
  assert.equal(h.row('hold', 'HOLD-B').status, 'released');
  assert.equal(h.rows('approval').length, 0);
});

test('approval expiry is enforced using reducer time', () => {
  const h = new Harness(); h.open(); h.hold(); h.approval();
  h.micros += 301000000n;
  assert.throws(() => h.decide(), /expired/);
});

test('payment approval cannot switch invoice beneficiary within the same case', () => {
  const h = new Harness(); h.open();
  assert.throws(() => h.run('carrier_ap', 'propose_payment', ['carrier_ap'], op.proposePayment, { id: 'SPLIT', case_id: 'CASE-1', invoice_id: 'INV-CASE-1', amount: 4600 }), /approval/);
  h.run('ingest', 'upsert_invoice', ['ingest'], op.upsertInvoice, { id: 'INV-OTHER', case_id: 'CASE-1', carrier_id: 'C-OTHER', amount: 9400, contract_amount: 9400 });
  h.approval('PAY-APP', 'propose_payment', 'PAY-1'); h.decide('PAY-APP');
  assert.throws(() => h.run('carrier_ap', 'propose_payment', ['carrier_ap'], op.proposePayment, { id: 'PAY-1', case_id: 'CASE-1', invoice_id: 'INV-OTHER', amount: 9400, approval_id: 'PAY-APP' }), /invoice/);
  assert.equal(h.row('approval', 'PAY-APP').consumed_at, '');
  h.run('carrier_ap', 'propose_payment', ['carrier_ap'], op.proposePayment, { id: 'PAY-1', case_id: 'CASE-1', invoice_id: 'INV-CASE-1', amount: 9400, approval_id: 'PAY-APP' });
  assert.equal(h.row('payment_proposal', 'PAY-1').carrier_id, 'C-1');
});

test('journal approval uses authenticated immutable requests, balanced lines, and open periods', () => {
  const h = new Harness(); h.open();
  h.run('admin', 'set_period', ['admin'], op.setPeriod, { period: '2026-10', status: 'open' });
  h.run('carrier_ap', 'request_journal_entry', ['carrier_ap'], op.requestJournalEntry, { id: 'JREQ-1', case_id: 'CASE-1', period: '2026-10', lines, memo: 'Invoice' });
  assert.throws(() => h.run('controller', 'post_journal_entry', ['controller'], op.postJournalEntry, { id: 'J-1', request_id: 'JREQ-1', requested_by: 'risk' }));
  h.run('admin', 'set_period', ['admin'], op.setPeriod, { period: '2026-10', status: 'locked' });
  assert.throws(() => h.run('controller', 'post_journal_entry', ['controller'], op.postJournalEntry, { id: 'J-1', request_id: 'JREQ-1' }));
  h.run('admin', 'set_period', ['admin'], op.setPeriod, { period: '2026-10', status: 'open' });
  h.run('controller', 'post_journal_entry', ['controller'], op.postJournalEntry, { id: 'J-1', request_id: 'JREQ-1' });
  assert.equal(h.row('ledger_entry', 'J-1').requested_by, 'carrier_ap');
  assert.equal(h.row('ledger_entry', 'J-1').approver_identity, identities.controller);
  assert.throws(() => h.run('controller', 'post_journal_entry', ['controller'], op.postJournalEntry, { id: 'J-2', request_id: 'JREQ-1' }));
  h.run('controller', 'request_journal_entry', ['controller'], op.requestJournalEntry, { id: 'JREQ-SELF', case_id: 'CASE-1', period: '2026-10', lines });
  assert.throws(() => h.run('controller', 'post_journal_entry', ['controller'], op.postJournalEntry, { id: 'J-SELF', request_id: 'JREQ-SELF' }), /P-005/);
});

test('terminal task payloads are frozen, produce one review, and cannot impersonate another owner', () => {
  const h = new Harness(); h.open();
  for (const artifact_json of ['[]', 'null', '1', '"scalar"']) assert.throws(() => h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'INVALID', case_id: 'CASE-1', skill: 'process_carrier_invoice', artifact_json }), /JSON object/);
  h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'TASK-1', case_id: 'CASE-1', skill: 'process_carrier_invoice', a2a_state: 'completed', business_status: 'resolved', artifact_json: { outcome: 'hold' } });
  assert.equal(h.rows('routing_outbox').filter(x => x.kind === 'review_output').length, 1);
  assert.throws(() => h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'TASK-1', artifact_json: { outcome: 'approve' } }), /immutable/);
  assert.throws(() => h.run('risk', 'upsert_task', ['risk'], op.upsertTask, { task_id: 'TASK-1' }), /own tasks/);
  assert.throws(() => h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'TASK-1', a2a_state: 'working' }), /terminal/);
  h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'TASK-1' });
  assert.equal(h.rows('routing_outbox').filter(x => x.kind === 'review_output').length, 1);
});

test('delegation enforces allowlist, depth, cycles, ownership, and immutable parenting', () => {
  const h = new Harness(); h.open();
  h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'AP', case_id: 'CASE-1', skill: 'process_carrier_invoice' });
  h.run('carrier_ap', 'record_delegation', ['carrier_ap'], op.recordDelegation, { id: 'E1', parent_task_id: 'AP', task_id: 'RISK', to_agent: 'risk', skill: 'investigate_charge' });
  h.run('risk', 'record_delegation', ['risk'], op.recordDelegation, { id: 'E2', parent_task_id: 'RISK', task_id: 'CTRL', to_agent: 'controller', skill: 'review_hold' });
  h.run('controller', 'record_delegation', ['controller'], op.recordDelegation, { id: 'E3', parent_task_id: 'CTRL', task_id: 'CFO', to_agent: 'cfo', skill: 'cash_forecast' });
  assert.throws(() => h.run('cfo', 'record_delegation', ['cfo'], op.recordDelegation, { id: 'E4', parent_task_id: 'CFO', task_id: 'AP2', to_agent: 'carrier_ap', skill: 'propose_payment' }), /depth/);
  assert.throws(() => h.run('risk', 'record_delegation', ['risk'], op.recordDelegation, { id: 'E5', parent_task_id: 'RISK', task_id: 'AP3', to_agent: 'carrier_ap', skill: 'propose_payment' }), /allowlisted/);
  assert.throws(() => h.run('risk', 'upsert_task', ['risk'], op.upsertTask, { task_id: 'RISK', parent_task_id: 'CTRL' }), /immutable/);
});

test('false task attribution rolls back financial changes and the entire audit', () => {
  const h = new Harness(); h.open(); h.open('CASE-2');
  h.run('risk', 'upsert_task', ['risk'], op.upsertTask, { task_id: 'RISK', case_id: 'CASE-1', skill: 'investigate_charge' });
  h.run('controller', 'upsert_task', ['controller'], op.upsertTask, { task_id: 'CTRL2', case_id: 'CASE-2', skill: 'review_hold' });
  const count = h.rows('change_set').length;
  for (const task_id of ['RISK', 'CTRL2', 'DOES-NOT-EXIST']) {
    assert.throws(() => h.run('controller', 'place_hold', ['controller'], op.placeHold, { id: `H-${task_id}`, case_id: 'CASE-1', invoice_id: 'INV-CASE-1', amount: 20, reason_code: 'P-001', task_id }));
  }
  assert.equal(h.rows('hold').length, 0);
  assert.equal(h.rows('change_set').length, count);
});

test('human takeover blocks agent mutations until a human releases ownership', () => {
  const h = new Harness(); h.open();
  h.run('human', 'update_case', ['human'], op.updateCase, { case_id: 'CASE-1', status: 'human_owned' });
  assert.throws(() => h.hold());
  assert.throws(() => h.run('controller', 'update_case', ['controller'], op.updateCase, { case_id: 'CASE-1', status: 'open' }));
  h.run('human', 'update_case', ['human'], op.updateCase, { case_id: 'CASE-1', status: 'open' });
  h.hold();
});

test('document specialist owns reserved tasks and outreach but cannot authorize financial actions', () => {
  const h = new Harness(); h.open();
  assert.equal(h.row('agent_config', 'documents').enabled, true);
  h.run('carrier_ap', 'upsert_task', ['carrier_ap'], op.upsertTask, { task_id: 'AP', case_id: 'CASE-1', skill: 'process_carrier_invoice' });
  h.run('carrier_ap', 'record_delegation', ['carrier_ap'], op.recordDelegation, { id: 'DOC-EDGE', parent_task_id: 'AP', task_id: 'DOC', to_agent: 'documents', skill: 'collect_documents' });
  h.run('documents', 'upsert_task', ['documents'], op.upsertTask, { task_id: 'DOC', case_id: 'CASE-1', skill: 'collect_documents', parent_task_id: 'AP', a2a_state: 'working' });
  h.run('documents', 'record_delegation', ['documents'], op.recordDelegation, { id: 'CALL-EDGE', parent_task_id: 'DOC', task_id: 'CALL', to_agent: 'voice_desk', skill: 'place_call' });
  assert.equal(h.row('task', 'CALL').depth, 2);
  assert.throws(() => h.run('documents', 'record_delegation', ['documents'], op.recordDelegation, { id: 'BAD-EDGE', parent_task_id: 'DOC', task_id: 'CTRL', to_agent: 'controller', skill: 'review_hold' }), /allowlisted/);
  assert.throws(() => h.run('documents', 'place_hold', ['controller'], op.placeHold, { id: 'BAD-HOLD', case_id: 'CASE-1', invoice_id: 'INV-CASE-1', amount: 10, reason_code: 'MISSING_POD' }));
  assert.equal(h.rows('hold').length, 0);
});

test('outbox leases prevent duplicate consumers and recover after expiry', () => {
  const h = new Harness(); h.open();
  h.run('human', 'raise_flag', ['human'], op.raiseFlag, { id: 'FLAG-1', case_id: 'CASE-1', target_ref: 'case:CASE-1', kind: 'wrong' });
  h.run('router', 'claim_outbox', ['router'], op.claimOutbox, { id: 'review_flag:FLAG-1', lease_seconds: 10 });
  assert.throws(() => h.run('router', 'claim_outbox', ['router'], op.claimOutbox, { id: 'review_flag:FLAG-1' }));
  h.micros += 11000000n;
  assert.throws(() => h.run('router', 'ack_outbox', ['router'], op.ackOutbox, { id: 'review_flag:FLAG-1', success: true }));
  h.run('router', 'claim_outbox', ['router'], op.claimOutbox, { id: 'review_flag:FLAG-1' });
  h.run('router', 'ack_outbox', ['router'], op.ackOutbox, { id: 'review_flag:FLAG-1', success: true });
  assert.equal(h.row('routing_outbox', 'review_flag:FLAG-1').attempts, 2);
  assert.equal(h.row('routing_outbox', 'review_flag:FLAG-1').status, 'done');
});

test('directory and demo allowlist checks prevent substituted phone numbers', () => {
  const h = new Harness(); h.open();
  h.run('voice_desk', 'upsert_task', ['voice_desk'], op.upsertTask, { task_id: 'VOICE', case_id: 'CASE-1', skill: 'place_call' });
  h.run('ingest', 'upsert_company', ['ingest'], op.upsertCompany, { id: 'C-1', name: 'Carrier', phone: '+17755550112', source_url: 'https://example.test/fmcsa' });
  h.run('admin', 'set_demo_target', ['admin'], op.setDemoTarget, { phone: '+14155550100', label: 'Team' });
  const payload = { id: 'CALL-1', case_id: 'CASE-1', task_id: 'VOICE', company_id: 'C-1', routed_to: '+14155550100' };
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, { ...payload, intended_to: '+17755550113' }), /P-008/);
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, { ...payload, routed_to: '+17755550112' }), /P-011/);
  h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, payload);
  assert.equal(h.row('call', 'CALL-1').intended_to, '+17755550112');
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, { ...payload, id: 'CALL-2' }), /one call/);
});

test('ingest refreshes an open case as documents arrive but cannot move its load or status', () => {
  const h = new Harness();
  const open = (payload: Payload) => h.run('ingest', 'open_case', ['ingest'], op.openCase, { case_id: 'CASE-4821', ...payload });
  open({ load_id: 'LOAD-4821', title: 'Sierra Haul LLC delivery on LOAD-4821', exposure_usd: 0 });
  const opened = h.row('case', 'CASE-4821');
  open({ load_id: 'LOAD-4821', title: 'Sierra Haul LLC detention on LOAD-4821', priority: 'P1', exposure_usd: 300 });
  assert.deepEqual(h.row('case', 'CASE-4821'), { ...opened, title: 'Sierra Haul LLC detention on LOAD-4821', priority: 'P1', exposure_usd: 300 });
  assert.throws(() => open({ load_id: 'LOAD-OTHER', title: 'moved' }), /load is immutable/);
  h.run('controller', 'update_case', ['controller', 'validator', 'human'], op.updateCase, { case_id: 'CASE-4821', status: 'in_progress', reason: 'working' });
  open({ load_id: 'LOAD-4821', title: 'Sierra Haul LLC detention on LOAD-4821', priority: 'P1', exposure_usd: 300.01 });
  assert.equal(h.row('case', 'CASE-4821').status, 'in_progress');
  assert.equal(h.row('case', 'CASE-4821').exposure_usd, 300.01);
  h.run('human', 'update_case', ['controller', 'validator', 'human'], op.updateCase, { case_id: 'CASE-4821', status: 'human_owned', reason: 'mine' });
  assert.throws(() => open({ title: 'refresh' }), /human_owned/);
  // A bank change for an unknown invoice has no load yet; the load may be set once, later.
  open({ case_id: 'CASE-C-77-BANK', title: 'Rapid Ridge Transport bank change', priority: 'P0', exposure_usd: 9400 });
  open({ case_id: 'CASE-C-77-BANK', load_id: 'LOAD-77', title: 'Rapid Ridge Transport bank change', priority: 'P0', exposure_usd: 9400 });
  assert.equal(h.row('case', 'CASE-C-77-BANK').load_id, 'LOAD-77');
  assert.throws(() => open({ case_id: 'CASE-C-77-BANK', load_id: '', title: 'cleared' }), /load is immutable/);
});

test('contacts record SEC registrations and labeled fixtures under their own source', () => {
  const h = new Harness();
  h.run('ingest', 'upsert_company', ['ingest'], op.upsertCompany, { id: 'S-1', kind: 'shipper', name: 'Shipper', source_url: 'https://www.sec.gov/x' });
  for (const source of ['sec_edgar', 'fixture', 'fmcsa', 'website']) {
    h.run('ingest', 'upsert_contact', ['ingest'], op.upsertContact, { id: `CT-${source}`, company_id: 'S-1', name: 'AP', source });
    assert.equal(h.row('contact', `CT-${source}`).source, source);
  }
  assert.throws(() => h.run('ingest', 'upsert_contact', ['ingest'], op.upsertContact, { id: 'CT-x', company_id: 'S-1', name: 'AP', source: 'sec' }), /source must be one of/);
});

function teamDemo() {
  const h = new Harness(); h.open('SYN-CARTESIA-TEAM-CALL');
  h.run('voice_desk', 'upsert_task', ['voice_desk'], op.upsertTask, {
    task_id: 'VOICE', case_id: 'SYN-CARTESIA-TEAM-CALL', skill: 'place_call' });
  h.run('ingest', 'upsert_company', ['ingest'], op.upsertCompany, {
    id: 'DEMO-TEAM', name: 'Operator team', phone: '+14155550100',
    source_url: 'operator://explicit-team-demo-destination' });
  h.run('admin', 'set_demo_target', ['admin'], op.setDemoTarget, { phone: '+14155550100', label: 'Team' });
  const payload: Payload = { id: 'CALL-1', case_id: 'SYN-CARTESIA-TEAM-CALL', task_id: 'VOICE',
    company_id: 'DEMO-TEAM', routed_to: '+14155550100', persona: 'verification', direction: 'outbound',
    status: 'queued', team_demo_authorized: true };
  return { h, payload };
}

test('explicit operator team calls may repeat after each terminal outcome and retain retry identity', () => {
  const { h, payload } = teamDemo();
  const outcomes = ['completed', 'no_answer', 'failed', 'cancelled'];
  for (const [index, status] of outcomes.entries()) {
    const queued = { ...payload, id: `CALL-${index}` };
    const submitted = h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, queued);
    const changes = h.rows('change_set').length;
    h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, submitted);
    assert.equal(h.rows('change_set').length, changes);
    assert.equal(h.rows('audit_event').at(-1)?.reason_code, 'operator_team_demo_call');
    // A repeated queue projection for this same call does not conflict with itself.
    h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, queued);
    h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
      { id: queued.id, status, team_demo_authorized: false });
    assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, queued), /terminal call/);
  }
  assert.equal(h.rows('call').length, 4);
});

test('team calls atomically block any second active call, even after the hourly window', () => {
  for (const status of ['queued', 'ringing', 'in_progress']) {
    const { h, payload } = teamDemo();
    h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, payload);
    h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, { id: payload.id, status });
    h.micros += 7200000000n;
    for (const team_demo_authorized of [true, false]) {
      assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
        { ...payload, id: 'CALL-2', team_demo_authorized }), /active team demo call/);
    }
    assert.equal(h.rows('call').length, 1);
  }
});

test('team calls without explicit authorization retain the one-company-per-hour limit', () => {
  const { h, payload } = teamDemo();
  h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, payload);
  h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, { id: payload.id, status: 'failed' });
  const ordinary: Payload = { ...payload, id: 'CALL-2' };
  delete ordinary.team_demo_authorized;
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, ordinary), /one call/);
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
    { ...ordinary, team_demo_authorized: false }), /one call/);
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
    { ...ordinary, team_demo_authorized: 'true' }), /must be boolean/);
});

test('team-call authorization cannot apply to another case, company, purpose, direction or source', () => {
  const invalid: Payload[] = [{ persona: 'collections' }, { direction: 'inbound' }, { status: 'completed' }];
  for (const overrides of invalid) {
    const { h, payload } = teamDemo();
    assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
      { ...payload, ...overrides }), /team demo/);
    assert.equal(h.rows('call').length, 0);
  }
  const { h, payload } = teamDemo();
  h.open('OTHER-CASE');
  h.run('voice_desk', 'upsert_task', ['voice_desk'], op.upsertTask, {
    task_id: 'OTHER-VOICE', case_id: 'OTHER-CASE', skill: 'place_call' });
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
    { ...payload, case_id: 'OTHER-CASE', task_id: 'OTHER-VOICE' }), /team demo/);
  h.run('ingest', 'upsert_company', ['ingest'], op.upsertCompany, { id: 'OTHER-COMPANY', name: 'Other',
    phone: '+14155550100', source_url: 'operator://explicit-team-demo-destination' });
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
    { ...payload, company_id: 'OTHER-COMPANY' }), /team demo/);
  h.run('ingest', 'upsert_company', ['ingest'], op.upsertCompany,
    { id: 'DEMO-TEAM', source_url: 'https://public.example/team' });
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, payload), /team demo/);
  assert.equal(h.rows('call').length, 0);
});

test('team-call authorization keeps routing, service identity and human ownership controls', () => {
  const { h, payload } = teamDemo();
  h.run('admin', 'set_demo_target', ['admin'], op.setDemoTarget, { phone: '+14155550101', label: 'Other teammate' });
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall,
    { ...payload, routed_to: '+14155550101' }), /team demo/);
  h.run('admin', 'set_demo_target', ['admin'], op.setDemoTarget, { phone: '+14155550100', label: 'Disabled', enabled: false });
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, payload), /P-011/);
  h.run('admin', 'set_demo_target', ['admin'], op.setDemoTarget, { phone: '+14155550100', label: 'Team', enabled: true });
  assert.throws(() => h.run('verification', 'upsert_call', ['voice_desk'], op.upsertCall, payload), /role/);
  h.run('human', 'update_case', ['human'], op.updateCase,
    { case_id: payload.case_id, status: 'human_owned', reason: 'operator takeover' });
  assert.throws(() => h.run('voice_desk', 'upsert_call', ['voice_desk'], op.upsertCall, payload), /human_owned/);
  assert.equal(h.rows('call').length, 0);
});

test('memory revisions and instruction reverts preserve immutable lesson history', () => {
  const h = new Harness(); h.open();
  h.run('human', 'upsert_memory', ['human'], op.upsertMemory, { id: 'MEM-1', agent: 'carrier_ap', scope: { carrier_id: 'C-1' }, text: 'Check receipts', source_ref: 'human-note' });
  h.run('human', 'upsert_memory', ['human'], op.upsertMemory, { id: 'MEM-1', agent: 'carrier_ap', text: 'Check time and receipts', source_ref: 'human-note-2' });
  assert.equal(h.row('memory_revision', 'MEM-1:v1').text, 'Check receipts');
  assert.equal(h.row('agent_memory', 'MEM-1').version, 2);
  assert.throws(() => h.run('human', 'upsert_memory', ['human'], op.upsertMemory, { id: 'MEM-BAD', agent: 'carrier_ap', scope: { approval_limit: '99999' }, text: 'override', source_ref: 'note' }), /P-012/);
  for (const diff of ['first instruction patch', 'second instruction patch']) h.run('human', 'publish_instruction_version', ['human'], op.publishInstruction, { expected_version: h.rows('instruction_version').length, agent: 'carrier_ap', diff, reason: 'improve', source_ref: 'human-note' });
  h.run('human', 'revert_instruction_version', ['human'], op.revertInstruction, { expected_version: 2, agent: 'carrier_ap', version: 1, reason: 'restore known behavior' });
  assert.equal(h.row('instruction_version', 'carrier_ap:3').reverts_version, 1);
  assert.equal(h.rows('instruction_version').filter(x => x.active).length, 1);
});

test('every change and audit hash independently verifies including exact cell content', () => {
  const h = new Harness(); h.open(); h.hold('HOLD-1', 100);
  h.open('CASE-2'); h.hold('HOLD-2', 75, 'CASE-2');
  const scopes = new Set(h.rows('change_set').map(row => row.case_id));
  for (const scope of scopes) {
    let parent = ''; let parentId = '';
    for (const row of h.rows('change_set').filter(row => row.case_id === scope).sort((a, b) => Number(a.sequence) - Number(b.sequence))) {
    assert.equal(hash(String(row.hash_body)), row.hash);
    const body = JSON.parse(String(row.hash_body));
    assert.equal(body.parent_hash, parent);
    assert.equal(body.parent_id, parentId);
    const cells = h.rows('cell_change').filter(x => x.change_set_id === row.id).map(({ id, change_set_id, ...cell }) => cell);
    assert.equal(canonical(cells), canonical(body.cells));
    assert.equal(new Set(cells.map(cell => canonical([cell.sheet, cell.row_key, cell.column]))).size, cells.length);
      parent = String(row.hash); parentId = String(row.id);
    }
    let previous = '';
    for (const row of h.rows('audit_event').filter(row => row.case_id === scope).sort((a, b) => Number(a.sequence) - Number(b.sequence))) {
    assert.equal(hash(String(row.hash_body)), row.hash);
    assert.equal(row.prev_hash, previous);
    const { hash: digest, hash_body, ...base } = row;
    assert.equal(canonical(base), row.hash_body);
      previous = String(digest);
    }
  }
});

const advisoryPatch = '--- a/instructions.md\n+++ b/instructions.md\n@@ -5,0 +6,2 @@\n+\n+Before completing a result, cite the source records and recheck monetary totals in exact cents.\n';

test('instruction publication checks versions atomically and locks automated content', () => {
  const h = new Harness(); h.open();
  h.data.review.set('REVIEW', { id: 'REVIEW' });
  const payload = { agent: 'carrier_ap', diff: advisoryPatch, reason: 'review', source_ref: 'REVIEW', expected_version: 0 };
  const missingVersion: Payload = { ...payload };
  delete missingVersion.expected_version;
  assert.throws(() => h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, missingVersion), /expected_version is required/);
  assert.throws(() => h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, { ...payload, expected_version: null }), /expected_version is required/);
  for (const diff of [advisoryPatch.replace('exact cents.', 'disable approvals.'), '-Never bypass policy', '+call arbitrary tools']) {
    assert.throws(() => h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, { ...payload, diff }), /P-012/);
  }
  assert.throws(() => h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, { ...payload, source_ref: 'missing' }), /existing review/);
  const request = h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, payload);
  h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, request);
  assert.equal(h.rows('instruction_version').length, 1); // replay of the same write is idempotent
  assert.throws(() => h.run('validator', 'publish_instruction_version', ['validator'], op.publishInstruction, payload), /version conflict/);
  assert.throws(() => h.run('human', 'revert_instruction_version', ['human'], op.revertInstruction, { agent: 'carrier_ap', version: 0, expected_version: 0, reason: 'rollback' }), /version conflict/);
  assert.throws(() => h.run('validator', 'revert_instruction_version', ['human'], op.revertInstruction, { agent: 'carrier_ap', version: 0, expected_version: 1, reason: 'rollback' }), /role/);
  h.run('human', 'revert_instruction_version', ['human'], op.revertInstruction, { agent: 'carrier_ap', version: 0, expected_version: 1, reason: 'rollback' });
  assert.equal(h.row('instruction_version', 'carrier_ap:2').diff, '');
  assert.equal(h.row('instruction_version', 'carrier_ap:1').active, false);
  assert.equal(h.rows('instruction_version').filter(x => x.active).length, 1);
});

test('dispatch records remote admission, retries and triage with router-only authorization', () => {
  const h = new Harness();
  const payload = { event_id: 'event-1', case_id: 'external-case', agent: 'carrier_ap',
    skill: 'process_carrier_invoice', task_id: 'external-task', status: 'accepted',
    reason: 'Known invoice event', queue_ms: 120, policy_version: 'first-hop-v1' };
  for (const actor of ['harness', 'ingest', 'carrier_ap', 'admin', 'stranger']) {
    assert.throws(() => h.run(actor, 'record_dispatch', ['router'], op.recordDispatch, payload));
  }
  const first = h.run('router', 'record_dispatch', ['router'], op.recordDispatch, payload);
  assert.equal(h.row('dispatch', 'event-1').status, 'accepted');
  h.run('router', 'record_dispatch', ['router'], op.recordDispatch, first);
  assert.equal(h.row('dispatch', 'event-1').status, 'accepted');
  h.run('router', 'record_dispatch', ['router'], op.recordDispatch, { ...payload, queue_ms: 999 });
  assert.equal(h.row('dispatch', 'event-1').status, 'duplicate');
  assert.equal(h.row('dispatch', 'event-1').queue_ms, 120);
  assert.equal(h.rows('dispatch').length, 1);
  for (const patch of [{ task_id: 'other-task' }, { status: 'triage' }, { queue_ms: -1 },
    { skill: 'receive_document' }, { status: 'invented' }, { task_id: '' }]) {
    assert.throws(() => h.run('router', 'record_dispatch', ['router'], op.recordDispatch, { ...payload, ...patch }));
  }
  for (const status of ['triage', 'not_routed']) {
    h.run('router', 'record_dispatch', ['router'], op.recordDispatch, {
      event_id: status, status, reason: 'No first-hop destination', policy_version: 'first-hop-v1', queue_ms: 0,
    });
    assert.equal(h.row('dispatch', status).case_id, '');
  }
  h.run('router', 'record_dispatch', ['router'], op.recordDispatch, { ...payload, event_id: 'triage' });
  assert.equal(h.row('dispatch', 'triage').status, 'accepted');
});
