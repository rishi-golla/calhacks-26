import { t, SenderError } from 'spacetimedb/server';
import db from './schema';
import { AGENTS } from './policy';
import * as op from './operations';

export default db;
export const init = db.init(op.initialize);
const input = { payload: t.string() };

// Keep the policy engine independent of V8 host imports for unit tests; only
// the module boundary converts validation errors into client SenderErrors.
function run(...args: Parameters<typeof op.execute>): void {
  try { op.execute(...args); }
  catch (error) { throw new SenderError(error instanceof Error ? error.message : 'invalid request'); }
}

export const grant_role = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'grant_role', ['admin'], op.grantRole));
export const open_case = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'open_case', ['ingest'], op.openCase));
export const update_case = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'update_case', ['controller', 'validator', 'human'], op.updateCase));
export const upsert_invoice = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_invoice', ['ingest'], op.upsertInvoice));
export const set_period = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'set_period', ['admin', 'human'], op.setPeriod));
export const request_journal_entry = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'request_journal_entry', ['carrier_ap', 'shipper_ar', 'cfo', 'controller'], op.requestJournalEntry));
export const post_journal_entry = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'post_journal_entry', ['controller'], op.postJournalEntry));
export const place_hold = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'place_hold', ['controller'], op.placeHold));
export const release_hold = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'release_hold', ['controller'], op.releaseHold));
export const propose_payment = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'propose_payment', ['carrier_ap'], op.proposePayment));
export const request_approval = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'request_approval', ['controller'], op.requestApproval));
export const issue_approval_code = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'issue_approval_code', ['admin'], op.issueApprovalCode));
export const decide_approval = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'decide_approval', ['human'], op.decideApproval));
export const upsert_task = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_task', AGENTS, op.upsertTask));
export const record_delegation = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'record_delegation', AGENTS, op.recordDelegation));
export const agent_heartbeat = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'agent_heartbeat', AGENTS, op.agentHeartbeat));
export const append_audit = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'append_audit', AGENTS, op.appendAudit));
export const record_doc_read = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'record_doc_read', AGENTS, op.recordDocRead));
export const set_demo_target = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'set_demo_target', ['admin'], op.setDemoTarget));
export const upsert_company = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_company', ['ingest'], op.upsertCompany));
export const upsert_contact = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_contact', ['ingest'], op.upsertContact));
export const upsert_call = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_call', ['voice_desk'], op.upsertCall));
export const add_precedent = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'add_precedent', ['controller'], op.addPrecedent));
export const log_llm_call = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'log_llm_call', ['harness'], op.logLlmCall));
export const set_metric = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'set_metric', ['evaluator'], op.setMetric));
export const set_agent_model = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'set_agent_model', ['human'], op.setAgentModel));
export const upsert_forecast = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_forecast', ['cfo'], op.upsertForecast));
export const add_review = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'add_review', ['validator'], op.addReview));
export const upsert_memory = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'upsert_memory', ['validator', 'human'], op.upsertMemory));
export const publish_instruction_version = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'publish_instruction_version', ['validator', 'human'], op.publishInstruction));
export const revert_instruction_version = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'revert_instruction_version', ['human'], op.revertInstruction));
export const raise_flag = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'raise_flag', ['human'], op.raiseFlag));
export const resolve_flag = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'resolve_flag', ['validator'], op.resolveFlag));
export const claim_outbox = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'claim_outbox', ['router'], op.claimOutbox));
export const ack_outbox = db.reducer(input, (ctx, { payload }) => run(ctx, payload, 'ack_outbox', ['router'], op.ackOutbox));
