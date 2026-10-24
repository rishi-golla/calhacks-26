import { schema, table, t, type InferSchema, type ReducerCtx } from 'spacetimedb/server';

const s = () => t.string();
const pk = () => s().primaryKey();
const indexed = () => s().index('btree');
const dollars = () => t.f64();
const strings = () => t.array(s());
const journalLine = t.object('JournalLine', { account: s(), debit: dollars(), credit: dollars() });

// These are the PRD's canonical SQL names. Accessors and columns deliberately
// remain snake_case; generated TypeScript client bindings camel-case them.
const spacetimedb = schema({
  case: table({ name: 'case', public: true }, {
    case_id: pk(), load_id: indexed(), opened_at: s(), status: s(), priority: s(),
    title: s(), exposure_usd: dollars(), resolved_at: s(),
  }),
  task: table({ name: 'task', public: true }, {
    task_id: pk(), case_id: indexed(), parent_task_id: indexed(), agent: indexed(),
    skill: s(), display_id: s(), a2a_state: s(), business_status: s(), outcome: s(),
    confidence: t.f64(), model_used: s(), started_at: s(), updated_at: s(),
    evidence_refs: strings(), artifact_json: s(), policy_ids: strings(),
    instr_version: t.u32(), memory_ids: strings(), depth: t.u32(),
  }),
  agent: table({ name: 'agent', public: true }, {
    name: pk(), status: s(), active: t.u32(), queued: t.u32(), done: t.u32(),
    errors: t.u32(), p50_ms: t.u32(), card_url: s(), last_seen: s(),
  }),
  agent_config: table({ name: 'agent_config', public: true }, {
    agent: pk(), model: s(), enabled: t.bool(),
  }),
  ledger_entry: table({ name: 'ledger_entry', public: true }, {
    id: pk(), case_id: indexed(), period: indexed(), lines: t.array(journalLine),
    memo: s(), requested_by: s(), approved_by: s(), posted_at: s(), request_id: s(),
    requester_identity: s(), approver_identity: s(),
  }),
  hold: table({ name: 'hold', public: true }, {
    id: pk(), case_id: indexed(), invoice_id: indexed(), amount: dollars(), reason_code: s(),
    status: s(), placed_by: s(), released_by: s(), released_amount: dollars(),
    placed_at: s(), released_at: s(),
  }),
  payment_proposal: table({ name: 'payment_proposal', public: true }, {
    id: pk(), case_id: indexed(), carrier_id: indexed(), invoice_id: indexed(), load_id: s(),
    amount: dollars(), status: s(), proposed_by: s(), proposed_at: s(), approval_id: s(),
  }),
  approval: table({ name: 'approval', public: true }, {
    id: pk(), case_id: indexed(), task_id: s(), action: s(), target_id: s(), amount: dollars(),
    requested_by: s(), requester_identity: s(), approver: s(), approver_name: s(),
    decision: s(), code_ok: t.bool(), decided_at: s(), expires_at: s(), consumed_at: s(),
    code_issued: t.bool(), failed_attempts: t.u32(), last_error: s(),
    invoice_id: s().default(''),
  }),
  audit_event: table({ name: 'audit_event', public: true }, {
    id: pk(), ts: s(), case_id: indexed(), task_id: s(), agent: s(), kind: s(),
    reason_code: s(), summary: s(), evidence_refs: strings(), policy_ids: strings(),
    model_used: s(), prev_hash: s(), hash: s(), sequence: t.u32(), hash_body: s(),
    change_set_id: s(), actor_identity: s(),
  }),
  call: table({ name: 'call', public: true }, {
    id: pk(), case_id: indexed(), task_id: indexed(), persona: s(), direction: s(),
    to_masked: s(), intended_to: s(), routed_to: s(), company_id: indexed(), contact_id: s(),
    status: s(), started_at: s(), ended_at: s(), findings_json: s(), transcript_redacted: s(),
    recording_url: s(), disclosure_given: t.bool(),
  }),
  precedent: table({ name: 'precedent', public: true }, {
    id: pk(), carrier_id: indexed(), claim_type: s(), lane: s(), pattern_json: s(),
    outcome: s(), as_of: s(), source_case: s(),
  }),
  llm_call: table({ name: 'llm_call', public: true }, {
    id: pk(), ts: s(), agent: indexed(), model: s(), latency_ms: t.u32(), tokens: t.u32(),
    route_reason: s(), ensemble_agreement: t.bool(),
  }),
  dispatch: table({ name: 'dispatch', public: true }, {
    event_id: pk(), case_id: indexed(), skill: s(), agent: indexed(), task_id: s(),
    status: s(), reason: s(), queue_ms: t.u32(), policy_version: s(), ts: s(),
  }),
  metric: table({ name: 'metric', public: true }, { name: pk(), value: t.f64(), updated_at: s() }),
  change_set: table({ name: 'change_set', public: true }, {
    id: pk(), parent_id: s(), case_id: indexed(), task_id: s(), agent: s(), action: s(), summary: s(),
    policy_ids: strings(), instr_version: t.u32(), memory_ids: strings(), ts: s(), hash: s(),
    parent_hash: s(), sequence: t.u32(), hash_body: s(), actor_identity: s(), write_id: s(),
  }),
  cell_change: table({ name: 'cell_change', public: true }, {
    id: pk(), change_set_id: indexed(), sheet: indexed(), row_key: indexed(), column: s(),
    before: s(), after: s(),
  }),
  doc_read: table({ name: 'doc_read', public: true }, {
    id: pk(), change_set_id: indexed(), task_id: indexed(), agent: s(), doc_ref: s(),
    page: t.u32(), span: s(), extracted_json: s(),
  }),
  review: table({ name: 'review', public: true }, {
    id: pk(), target_task_id: indexed(), case_id: indexed(), verdict: s(),
    checks_json: s(), friction_json: s(), ts: s(), actions_json: s(),
  }),
  agent_memory: table({ name: 'agent_memory', public: true }, {
    id: pk(), agent: indexed(), scope: s(), text: s(), source: s(), source_ref: s(),
    version: t.u32(), active: t.bool(), created_at: s(),
  }),
  instruction_version: table({ name: 'instruction_version', public: true }, {
    id: pk(), agent: indexed(), version: t.u32(), diff: s(), reason: s(), author: s(),
    source_ref: s(), created_at: s(), active: t.bool(), reverts_version: t.u32(),
  }),
  flag: table({ name: 'flag', public: true }, {
    id: pk(), target_ref: s(), case_id: indexed(), kind: s(), note: s(), raised_by: s(),
    status: s(), resolution_ref: s(), ts: s(),
  }),
  company: table({ name: 'company', public: true }, {
    id: pk(), kind: s(), name: s(), usdot: s(), mc: s(), phone: s(), website: s(), source_url: s(),
  }),
  contact: table({ name: 'contact', public: true }, {
    id: pk(), company_id: indexed(), name: s(), title: s(), phone: s(), email: s(), linkedin_url: s(),
    source: s(), source_url: s(), confidence: t.f64(), alternates_json: s(),
  }),
  delegation_edge: table({ name: 'delegation_edge', public: true }, {
    id: pk(), ts: s(), case_id: indexed(), from_agent: s(), to_agent: s(), skill: s(),
    task_id: s().unique(), parent_task_id: indexed(), depth: t.u32(),
  }),
  // Reducer-owned supporting truth. Invoice amounts are mirrored from W4's
  // source system before financial operations; no caller-supplied invoice limit.
  invoice: table({ name: 'invoice', public: true }, {
    id: pk(), case_id: indexed(), carrier_id: indexed(), load_id: indexed(),
    amount: dollars(), contract_amount: dollars(), paid_amount: dollars(), issued_at: s(), status: s(),
  }),
  accounting_period: table({ name: 'accounting_period', public: true }, {
    period: pk(), status: s(), updated_at: s(),
  }),
  principal: table({ name: 'principal', public: true }, {
    identity: pk(), role: s(), actor: indexed(), active: t.bool(), granted_at: s(),
  }),
  cash_forecast: table({ name: 'cash_forecast', public: true }, {
    id: pk(), case_id: indexed(), period: s(), category: s(), amount: dollars(),
    confidence: t.f64(), assumptions_json: s(), updated_at: s(),
  }),
  journal_request: table({ name: 'journal_request', public: true }, {
    id: pk(), case_id: indexed(), period: s(), lines: t.array(journalLine), memo: s(),
    requested_by: s(), requester_identity: s(), created_at: s(), posted_entry_id: s(),
  }),
  routing_outbox: table({ name: 'routing_outbox', public: true }, {
    id: pk(), kind: s(), target_agent: s(), case_id: indexed(), task_id: s(), payload_json: s(),
    status: s(), attempts: t.u32(), created_at: s(), updated_at: s(),
    claimed_by: s(), lease_expires_at: s(), last_error: s(),
  }),
  memory_revision: table({ name: 'memory_revision', public: true }, {
    id: pk(), memory_id: indexed(), agent: s(), version: t.u32(), scope: s(), text: s(),
    source: s(), source_ref: s(), active: t.bool(), created_at: s(),
  }),
  demo_target: table({ name: 'demo_target', public: true }, {
    phone: pk(), label: s(), enabled: t.bool(),
  }),
  approval_secret: table({ name: 'approval_secret' }, {
    approval_id: pk(), code_hash: s(), expires_micros: t.u64(), attempts: t.u32(), consumed: t.bool(),
  }),
  write_receipt: table({ name: 'write_receipt' }, {
    id: pk(), payload_hash: s(), action: s(), actor_identity: s(), change_set_id: s(), ts: s(),
  }),
  chain_head: table({ name: 'chain_head' }, { name: pk(), id: s(), hash: s(), sequence: t.u32() }),
});

export default spacetimedb;
export type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;
