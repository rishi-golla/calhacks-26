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
  case: 'case_id', task: 'task_id', agent: 'name', agent_config: 'agent', ledger_entry: 'id', hold: 'id',
  payment_proposal: 'id', approval: 'id', audit_event: 'id', call: 'id', precedent: 'id', llm_call: 'id', metric: 'name',
  change_set: 'id', cell_change: 'id', doc_read: 'id', review: 'id', agent_memory: 'id', instruction_version: 'id',
  flag: 'id', company: 'id', contact: 'id', delegation_edge: 'id', invoice: 'id', accounting_period: 'period',
  principal: 'identity', cash_forecast: 'id', journal_request: 'id', routing_outbox: 'id', memory_revision: 'id',
  demo_target: 'phone', approval_secret: 'approval_id', write_receipt: 'id', chain_head: 'name',
};
const identities: Record<string, string> = {};
['admin', 'human', 'controller', 'carrier_ap', 'shipper_ar', 'cfo', 'risk', 'verification', 'voice_desk', 'validator', 'ingest', 'harness', 'evaluator', 'router', 'stranger'].forEach((name, i) => identities[name] = (i + 1).toString(16).padStart(64, '0'));

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

test('memory revisions and instruction reverts preserve immutable lesson history', () => {
  const h = new Harness(); h.open();
  h.run('human', 'upsert_memory', ['human'], op.upsertMemory, { id: 'MEM-1', agent: 'carrier_ap', scope: { carrier_id: 'C-1' }, text: 'Check receipts', source_ref: 'human-note' });
  h.run('human', 'upsert_memory', ['human'], op.upsertMemory, { id: 'MEM-1', agent: 'carrier_ap', text: 'Check time and receipts', source_ref: 'human-note-2' });
  assert.equal(h.row('memory_revision', 'MEM-1:v1').text, 'Check receipts');
  assert.equal(h.row('agent_memory', 'MEM-1').version, 2);
  assert.throws(() => h.run('human', 'upsert_memory', ['human'], op.upsertMemory, { id: 'MEM-BAD', agent: 'carrier_ap', scope: { approval_limit: '99999' }, text: 'override', source_ref: 'note' }), /P-012/);
  for (const diff of ['first instruction patch', 'second instruction patch']) h.run('human', 'publish_instruction_version', ['human'], op.publishInstruction, { agent: 'carrier_ap', diff, reason: 'improve', source_ref: 'human-note' });
  h.run('human', 'revert_instruction_version', ['human'], op.revertInstruction, { agent: 'carrier_ap', version: 1, reason: 'restore known behavior' });
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
