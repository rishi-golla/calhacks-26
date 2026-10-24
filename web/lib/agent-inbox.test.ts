import assert from "node:assert/strict";
import test from "node:test";
import {
  agentActivity,
  buildAgentMessages,
  inboxActivityFingerprint,
  isSyntheticCase,
  messagesForAgent,
  onlineAgentCount,
} from "./agent-inbox";
import { emptySnapshot, normalize, type Row } from "./data";

const at = "2026-10-24T12:00:00.000Z";
const now = Date.parse(at);
const task = {
  task_id: "ap-1",
  case_id: "case-a",
  agent: "carrier_ap",
  skill: "process_carrier_invoice",
  a2a_state: "completed",
  business_status: "resolved",
  outcome: "hold",
  started_at: at,
  updated_at: at,
  artifact_json: JSON.stringify({
    outcome: "hold",
    evidence_refs: ["invoice:9"],
    policy_ids: ["P-002"],
    model_used: "configured/model",
    findings: { assessment: { summary: "A receipt is missing." } },
  }),
};

function toolAudit(status: string, changes: Row = {}, eventChanges: Row = {}): Row {
  return {
    id: `tool-${status}`, agent: "carrier_ap", case_id: "case-a", task_id: "ap-1",
    ts: at, kind: "tool_call", reason_code: `tool_${status}`,
    evidence_refs: ["invoice:9"], policy_ids: [],
    summary: JSON.stringify({
      version: 1, tool_call_id: "call_123", tool_name: "read_payment_status", status,
      arguments: {}, result: status === "completed" ? { paid: false } : null,
      duration_ms: status === "completed" ? 1032 : null,
      model: "openai/gpt-6-luna", ...eventChanges,
    }),
    ...changes,
  };
}

test("result previews prefer the complete tool assessment and reject unusable summaries", () => {
  const snapshot = emptySnapshot();
  const artifact = JSON.parse(task.artifact_json);
  const completeSummary = "Checked the payment status and supporting documents; the receipt is still missing.";
  for (const [toolAssessment, expected] of [
    [{ summary: completeSummary }, completeSummary],
    [null, "A receipt is missing."],
    [{ summary: "  " }, "A receipt is missing."],
    [{ summary: { untrusted: "not a summary" } }, "A receipt is missing."],
    [[{ summary: completeSummary }], "A receipt is missing."],
  ]) {
    snapshot.task = [{ ...task, artifact_json: JSON.stringify({
      ...artifact, findings: { ...artifact.findings, tool_assessment: toolAssessment },
    }) }];
    const message = buildAgentMessages(snapshot)[0];
    assert.equal(message.body, expected);
    assert.equal(message.kind, "result");
    assert.deepEqual(message.evidenceRefs, ["invoice:9"]);
  }
});

test("final narration failures retain successful domain results and show the limitation", () => {
  const snapshot = emptySnapshot();
  const error = "Final model summary unavailable; guarded operation result preserved.";
  snapshot.task = [{ ...task, artifact_json: JSON.stringify({
    outcome: "approve", model_used: "openai/gpt-6-luna", evidence_refs: ["invoice:9"],
    findings: {
      payment_proposal: { executed: true },
      tool_assessment: null, tool_loop: { assessment_error: error },
    },
  }) }];
  const message = buildAgentMessages(snapshot)[0];
  assert.equal(message.title, "Result published");
  assert.equal(message.kind, "result");
  assert.match(message.body, /^Payment proposal recorded\./);
  assert.ok(message.body.includes(`Summary note: ${error}`));
  assert.equal((message.details as Row).outcome, "approve");
  assert.equal((message.details as Row).assessmentError, error);
  assert.equal(((message.details as Row).facts as Row).payment_proposal_executed, true);
});

test("tool lifecycle events collapse into one live card in any snapshot order", () => {
  const snapshot = emptySnapshot();
  snapshot.audit_event = [toolAudit("completed"), toolAudit("started"), toolAudit("waiting"), toolAudit("completed")];
  const messages = buildAgentMessages(snapshot);
  assert.equal(messages.length, 1);
  const message = messages[0];
  assert.equal(message.id, "tool:ap-1:call_123");
  assert.equal(message.kind, "tool");
  assert.equal(message.toolCall?.name, "read_payment_status");
  assert.equal(message.toolCall?.status, "completed");
  assert.equal(message.toolCall?.durationMs, 1032);
  assert.deepEqual(message.toolCall?.result, { paid: false });
  assert.deepEqual(message.toolCall?.history.map((event) => event.status), ["started", "waiting", "completed"]);
  assert.equal(message.model, "openai/gpt-6-luna");
  assert.ok(!message.body.includes("Payment recorded"));
  snapshot.audit_event.reverse();
  assert.deepEqual(buildAgentMessages(snapshot), messages);
});

test("tool calls remain task scoped, searchable, and visible only in their role thread", () => {
  const snapshot = emptySnapshot();
  snapshot.audit_event = [
    toolAudit("started"),
    toolAudit("blocked", { id: "second-task", task_id: "ap-2", agent: "risk", case_id: "case-b" }),
  ];
  const messages = buildAgentMessages(snapshot);
  assert.equal(messages.length, 2);
  assert.equal(messagesForAgent(messages, "carrier_ap").length, 1);
  assert.equal(messagesForAgent(messages, "risk", "case-b", "read_payment_status blocked").length, 1);
  assert.equal(messagesForAgent(messages, "documents").length, 0);
  assert.match(messages.find((message) => message.agentId === "risk")!.body, /blocked/);
});

test("malformed tool telemetry never dumps raw summaries into the conversation", () => {
  const snapshot = emptySnapshot();
  snapshot.audit_event = [
    toolAudit("started", { id: "bad-json", summary: "password=super-secret malformed json" }),
    toolAudit("started", { id: "wrong-version" }, { version: 2 }),
    toolAudit("started", { id: "wrong-status" }, { status: "completed" }),
    toolAudit("started", { id: "bad-arguments" }, { arguments: "secret prompt" }),
    toolAudit("started", { id: "bad-result" }, { result: ["private"] }),
    toolAudit("started", { id: "missing-task", task_id: "" }),
  ];
  const messages = buildAgentMessages(snapshot);
  assert.equal(messages.length, 6);
  assert.ok(messages.every((message) => message.title === "Tool event unavailable" && !message.toolCall));
  assert.ok(!JSON.stringify(messages).includes("super-secret"));
  assert.ok(!JSON.stringify(messages).includes("secret prompt"));
});

test("tool inspector redacts sensitive nested fields and limits displayed output", () => {
  const snapshot = emptySnapshot();
  snapshot.audit_event = [toolAudit("completed", {}, {
    arguments: { task: "current", phone: "+14155551234", token: "private-token", nested: { account_number: "89900123456" } },
    result: {
      summary: "Contact hello@example.com or +1 (415) 555-1234; sk-secret_123",
      prompt: "hidden prompt", reasoning: "hidden reasoning", transcript: "hidden transcript",
      ok: true, balance: 0, rows: Array.from({ length: 100 }, () => ({ note: "x".repeat(400) })),
    },
  })];
  const message = buildAgentMessages(snapshot)[0];
  const serialized = JSON.stringify(message);
  for (const hidden of ["14155551234", "private-token", "89900123456", "hello@example.com", "415", "sk-secret_123", "hidden prompt", "hidden reasoning", "hidden transcript"])
    assert.ok(!serialized.includes(hidden), hidden);
  assert.equal(message.toolCall?.result?.ok, true);
  assert.equal(message.toolCall?.result?.balance, 0);
  assert.ok(serialized.length < 9000);
  assert.equal((message.toolCall?.result?.rows as unknown[]).length, 20);
});

test("tool completion changes unread fingerprint even at the same timestamp", () => {
  const snapshot = emptySnapshot();
  snapshot.audit_event = [toolAudit("started")];
  const started = buildAgentMessages(snapshot)[0];
  snapshot.audit_event.push(toolAudit("completed"));
  const completed = buildAgentMessages(snapshot)[0];
  assert.equal(started.id, completed.id);
  assert.notEqual(inboxActivityFingerprint(started), inboxActivityFingerprint(completed));
  assert.ok(!inboxActivityFingerprint(completed).includes("paid"));
});

test("real tool execution on synthetic source data retains separate source labels", () => {
  const snapshot = emptySnapshot();
  snapshot.case = [{ case_id: "case-a", title: "[Live synthetic] Tool check" }];
  snapshot.audit_event = [toolAudit("failed", {}, { result: { error: "SourceUnavailable" }, duration_ms: -1 })];
  const message = buildAgentMessages(snapshot)[0];
  assert.equal(message.syntheticSource, true);
  assert.equal(message.model, "openai/gpt-6-luna");
  assert.equal(message.toolCall?.status, "failed");
  assert.equal(message.toolCall?.durationMs, undefined);
  assert.ok(!message.body.includes("Simulation"));
});

test("live Apify evidence is distinguished from the synthetic case that requested it", () => {
  const snapshot = emptySnapshot();
  snapshot.case = [{ case_id: "case-a", title: "[Live synthetic] Sponsor check" }];
  snapshot.audit_event = [toolAudit("completed", {}, {
    result: { provider: "apify", source_simulated: false, available: true },
  })];
  const message = buildAgentMessages(snapshot)[0];
  assert.equal(message.syntheticSource, true);
  assert.equal(message.liveExternalEvidence, true);
  for (const result of [
    { provider: "apify", source_simulated: false, available: false },
    { provider: "apify", source_simulated: true, available: true },
    { provider: "apify", available: true },
  ]) {
    snapshot.audit_event = [toolAudit("completed", {}, { result })];
    assert.equal(buildAgentMessages(snapshot)[0].liveExternalEvidence, undefined);
  }
});

test("synthetic source labels do not turn real model results into fixture inference", () => {
  const snapshot = emptySnapshot();
  snapshot.case = [
    { case_id: "case-a", title: "[Live synthetic] Concurrent A2A check" },
  ];
  snapshot.task = [
    {
      ...task,
      artifact_json: JSON.stringify({
        model_used: "openai/gpt-6-luna",
        findings: {
          simulated: false,
          assessment: { summary: "The supplied invoice matches." },
        },
      }),
    },
  ];
  const result = buildAgentMessages(snapshot)[0];
  assert.equal(result.syntheticSource, true);
  assert.equal(result.model, "openai/gpt-6-luna");
  assert.equal(result.body, "The supplied invoice matches.");
  assert.equal(isSyntheticCase({ title: "[Synthetic live demo] Check" }), true);
  assert.equal(
    isSyntheticCase({ title: "Synthetic packaging carrier invoice" }),
    false,
  );
});

test("online count requires fresh known-agent heartbeats and a live connection", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [task];
  assert.equal(onlineAgentCount(snapshot, now, true), 0);
  snapshot.agent = [
    { name: "carrier_ap", status: "idle", last_seen: at },
    { name: "risk", status: "busy", last_seen: at },
    { name: "verification", status: "error", last_seen: at },
    {
      name: "documents",
      status: "idle",
      last_seen: new Date(now - 60_000).toISOString(),
    },
    {
      name: "cfo",
      status: "idle",
      last_seen: new Date(now + 61_000).toISOString(),
    },
    { name: "unknown", status: "idle", last_seen: at },
  ];
  assert.equal(onlineAgentCount(snapshot, now, true), 2);
  assert.equal(onlineAgentCount(snapshot, now, false), 0);
});

test("a busy heartbeat alone cannot claim an agent is still working", () => {
  const snapshot = emptySnapshot();
  snapshot.agent = [{ name: "carrier_ap", status: "busy", last_seen: at }];
  snapshot.task = [task];
  assert.equal(agentActivity(snapshot, "carrier_ap", now, true).state, "idle");
  snapshot.task = [
    { ...task, a2a_state: "working", business_status: "investigating" },
  ];
  assert.equal(
    agentActivity(snapshot, "carrier_ap", now, true).state,
    "working",
  );
  assert.equal(
    agentActivity(snapshot, "carrier_ap", now + 61_000, true).state,
    "offline",
  );
});

test("WebSocket and SQL task states normalize to the same terminal activity", () => {
  const artifact = task.artifact_json;
  const websocket = normalize({
    taskId: "normalized",
    caseId: "case-a",
    agent: "carrier_ap",
    a2AState: "completed",
    businessStatus: "investigating",
    updatedAt: at,
    artifactJson: artifact,
  }) as Row;
  const sql = normalize({
    task_id: "normalized",
    case_id: "case-a",
    agent: "carrier_ap",
    a_2_a_state: "completed",
    business_status: "investigating",
    updated_at: at,
    artifact_json: artifact,
  }) as Row;
  assert.deepEqual(websocket, sql);
  assert.equal(websocket.a2a_state, "completed");
  assert.equal(websocket.a2_a_state, undefined);
  assert.equal(sql.a_2_a_state, undefined);
  const snapshot = emptySnapshot();
  snapshot.agent = [{ name: "carrier_ap", status: "busy", last_seen: at }];
  for (const row of [websocket, sql]) {
    snapshot.task = [row];
    assert.equal(buildAgentMessages(snapshot)[0].kind, "result");
    assert.equal(
      agentActivity(snapshot, "carrier_ap", now, true).state,
      "idle",
    );
  }
});

test("empty records cannot manufacture activity; unknown actors do not break the roster", () => {
  const snapshot = emptySnapshot();
  assert.deepEqual(buildAgentMessages(snapshot), []);
  snapshot.task = [{ ...task, agent: "unknown" }];
  snapshot.audit_event = [
    { id: "x", agent: "unknown", summary: "No known role", ts: at },
  ];
  assert.deepEqual(buildAgentMessages(snapshot), []);
});

test("stable ordering and IDs survive repeated/reordered snapshot records", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [task, task];
  snapshot.audit_event = [
    {
      id: "b",
      agent: "controller",
      kind: "place_hold",
      summary: "Hold retained.",
      ts: at,
      case_id: "case-a",
    },
    {
      id: "a",
      agent: "controller",
      kind: "place_hold",
      summary: "Receipt required.",
      ts: at,
      case_id: "case-a",
    },
    {
      id: "task-audit",
      agent: "carrier_ap",
      kind: "upsert_task",
      summary: "upsert_task",
      ts: at,
    },
  ];
  const messages = buildAgentMessages(snapshot);
  assert.deepEqual(
    messages.map((message) => message.id),
    ["audit:a", "audit:b", "task:ap-1:result"],
  );
  snapshot.audit_event.reverse();
  assert.deepEqual(buildAgentMessages(snapshot), messages);
  assert.equal(messages[2].body, "A receipt is missing.");
  assert.deepEqual(messages[2].evidenceRefs, ["invoice:9"]);
});

test("handoffs appear for both participants and results return only to same-case parent", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [
    task,
    { ...task, task_id: "risk-1", parent_task_id: "ap-1", agent: "risk" },
  ];
  snapshot.delegation_edge = [
    {
      id: "e",
      ts: at,
      from_agent: "carrier_ap",
      to_agent: "risk",
      case_id: "case-a",
      task_id: "risk-1",
      skill: "investigate_charge",
      parent_task_id: "ap-1",
      depth: 1,
    },
  ];
  const messages = buildAgentMessages(snapshot);
  const handoff = messages.find((message) => message.kind === "handoff")!;
  assert.ok(messagesForAgent(messages, "carrier_ap").includes(handoff));
  assert.ok(messagesForAgent(messages, "risk").includes(handoff));
  assert.ok(!messagesForAgent(messages, "documents").includes(handoff));
  assert.equal(
    messages.find((message) => message.id === "task:risk-1:result")
      ?.recipientId,
    "carrier_ap",
  );
  snapshot.task[1].case_id = "different-case";
  assert.equal(
    buildAgentMessages(snapshot).find(
      (message) => message.id === "task:risk-1:result",
    )?.recipientId,
    undefined,
  );
});

test("case and multi-word search filters include evidence and agent names", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [task, { ...task, task_id: "b", case_id: "case-b" }];
  const messages = buildAgentMessages(snapshot);
  assert.equal(
    messagesForAgent(messages, "all", "case-a", "settlement receipt").length,
    1,
  );
  assert.equal(
    messagesForAgent(messages, "all", "case-b", "invoice:9").length,
    1,
  );
  assert.equal(messagesForAgent(messages, "risk", "", "receipt").length, 0);
});

test("malformed and scalar artifacts are not passed off as model summaries", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [
    "null",
    "[]",
    "false",
    "{broken",
    { findings: { assessment: ["fake"] } },
  ].map((artifact, i) => ({
    ...task,
    task_id: `t${i}`,
    artifact_json: artifact,
  }));
  const messages = buildAgentMessages(snapshot);
  assert.equal(messages.length, 5);
  assert.ok(messages.every((message) => !message.body.includes("fake")));
  assert.ok(messages.every((message) => typeof message.body === "string"));
});

test("call projection uses redacted text and never includes destination fields", () => {
  const snapshot = emptySnapshot();
  snapshot.call = [
    {
      id: "c",
      case_id: "case-a",
      task_id: "voice-1",
      status: "completed",
      started_at: at,
      intended_to: "+14155550111",
      routed_to: "+14155550222",
      transcript_redacted: "user: Send it to [email].",
      findings_json: JSON.stringify({
        intended_to: "+14155550111",
        findings: {},
        transcript_ref: "call://c",
      }),
    },
  ];
  const message = buildAgentMessages(snapshot)[0];
  assert.equal(message.body, "user: Send it to [email].");
  assert.ok(!JSON.stringify(message).includes("+1415555"));
  assert.deepEqual(message.evidenceRefs, ["call://c"]);
});

test("quality review reports recorded failures and targets the original agent", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [task];
  snapshot.review = [
    {
      id: "r",
      case_id: "case-a",
      target_task_id: "ap-1",
      ts: at,
      verdict: "issue",
      checks_json: JSON.stringify([
        { name: "missing_receipt", ok: false },
        { name: "exact_cents", ok: true },
      ]),
    },
  ];
  const review = buildAgentMessages(snapshot).find(
    (message) => message.kind === "review",
  )!;
  assert.equal(review.recipientId, "carrier_ap");
  assert.match(review.body, /missing receipt/);
  assert.ok(!review.body.includes("exact cents"));
});

test("service liveness distinguishes fresh work, waits, stale state, and disconnected source", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [
    { ...task, a2a_state: "working", business_status: "investigating" },
  ];
  assert.deepEqual(agentActivity(snapshot, "carrier_ap", now, true), {
    label: "Working",
    state: "working",
  });
  assert.equal(
    agentActivity(snapshot, "carrier_ap", now + 61_000, true).label,
    "No recent update",
  );
  assert.equal(
    agentActivity(snapshot, "carrier_ap", now, false).state,
    "offline",
  );
  snapshot.agent = [
    {
      name: "carrier_ap",
      status: "online",
      last_seen: new Date(now + 61_000).toISOString(),
    },
  ];
  assert.equal(
    agentActivity(snapshot, "carrier_ap", now + 61_000, true).state,
    "working",
  );
  snapshot.task[0].a2a_state = "input-required";
  snapshot.task[0].business_status = "awaiting_human_approval";
  assert.deepEqual(agentActivity(snapshot, "carrier_ap", now + 61_000, true), {
    label: "Needs human approval",
    state: "waiting",
  });
});

test("content changes to stable records change unread fingerprint", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [task];
  const message = buildAgentMessages(snapshot)[0];
  assert.notEqual(
    inboxActivityFingerprint(message),
    inboxActivityFingerprint({ ...message, body: "Updated result." }),
  );
});

function fixtureMessage(
  agent: string,
  findings: Record<string, unknown>,
  outcome = "verified",
) {
  const snapshot = emptySnapshot();
  snapshot.task = [
    {
      ...task,
      agent,
      artifact_json: JSON.stringify({
        outcome,
        model_used: "fixture/deterministic",
        findings: {
          ...findings,
          simulated: true,
          assessment: { summary: "Fixture assessment for a role." },
        },
      }),
    },
  ];
  return buildAgentMessages(snapshot)[0];
}

test("fixture summaries derive varied invoice and delta amounts, retaining zero", () => {
  const result = fixtureMessage(
    "carrier_ap",
    {
      match: { invoice_amount: 321.45, contract_rate: 300, delta: 21.45 },
      payment_blocked: true,
    },
    "hold",
  );
  assert.match(result.body, /^Simulation: /);
  assert.match(result.body, /Invoice: \$321\.45/);
  assert.match(result.body, /Contract: \$300\.00/);
  assert.match(result.body, /difference: \$21\.45/);
  assert.match(result.body, /Payment proposal blocked/);
  assert.ok(!result.body.includes("Fixture assessment"));
  const zero = fixtureMessage("carrier_ap", {
    match: { invoice_amount: "0", contract_rate: 0, delta: 0 },
  });
  assert.equal(zero.body.match(/\$0\.00/g)?.length, 3);
});

test("malformed financial values are omitted instead of coerced to zero", () => {
  const result = fixtureMessage("shipper_ar", {
    invoice_amount: "not money",
    matched_amount: null,
    balance: false,
  });
  assert.ok(!result.body.includes("$"));
  assert.match(result.body, /^Simulation: Recorded outcome/);
  assert.deepEqual((result.details as { facts: unknown }).facts, {});
});

test("risk and documents summaries reflect only recorded evidence fields", () => {
  const risk = fixtureMessage("risk", {
    unsupported_amount: 42.75,
    gps_dwell_min: 0,
    free_time_min: 30,
    unsupported_charges: ["unsupported_lumper"],
    needs_verification: true,
  });
  assert.match(risk.body, /\$42\.75/);
  assert.match(risk.body, /0 minutes; free time: 30 minutes/);
  assert.match(risk.body, /unsupported lumper/);
  const docs = fixtureMessage("documents", {
    complete: true,
    missing: [],
    documents: [{ kind: "pod", signed: true, intended_to: "+14155550111" }],
  });
  assert.match(docs.body, /Document packet complete/);
  assert.match(docs.body, /signed POD/);
  assert.ok(!JSON.stringify(docs).includes("+14155550111"));
  const missing = fixtureMessage("documents", {
    complete: false,
    missing: ["pod", "receipt"],
  });
  assert.match(missing.body, /Missing documents \(2\): pod, receipt/);
});

test("receivables and profitability preserve zero and negative recorded amounts", () => {
  const ar = fixtureMessage("shipper_ar", {
    invoice_amount: 900,
    matched_amount: 900,
    balance: 0,
  });
  assert.match(ar.body, /Matched deposits: \$900\.00/);
  assert.match(ar.body, /Balance: \$0\.00/);
  const cfo = fixtureMessage("cfo", {
    invoiced_margin: -14.25,
    contracted_margin: 0,
    net_cash: -5,
  });
  assert.match(cfo.body, /margin: -\$14\.25/);
  assert.match(cfo.body, /Contracted load margin: \$0\.00/);
  assert.match(cfo.body, /Net cash forecast: -\$5\.00/);
});

test("simulated hold and payment receipts cannot imply a committed financial action", () => {
  const controller = fixtureMessage(
    "controller",
    {
      hold_amount: 73.25,
      payment_blocked: true,
      hold: {
        executed: false,
        decision: "allow",
        proposed_payload: { amount: 73.25 },
      },
    },
    "hold",
  );
  assert.match(controller.body, /Hold for \$73\.25 not executed/);
  assert.ok(!controller.body.includes("recorded."));
  const proposed = fixtureMessage(
    "carrier_ap",
    {
      payment_proposal: { executed: false, proposed_payload: { amount: 940 } },
    },
    "approve",
  );
  assert.match(proposed.body, /Payment proposal for \$940\.00 not executed/);
  const committed = fixtureMessage(
    "controller",
    { hold_amount: 73.25, hold: { executed: true } },
    "hold",
  );
  assert.match(committed.body, /Hold for \$73\.25 recorded/);
});

test("voice and verification summaries label simulated caller findings without exposing phone payloads", () => {
  const call = {
    reached: true,
    intended_to: "+14155550111",
    routed_to: "+14155550222",
    findings: {
      claim_confirmed: false,
      receipt_provided: false,
      simulated: true,
    },
  };
  const voice = fixtureMessage(
    "voice_desk",
    { call_result: call },
    "unverified",
  );
  assert.match(
    voice.body,
    /^Simulation: Reported call result: contact reached/,
  );
  assert.match(voice.body, /claim was not confirmed/);
  assert.ok(!JSON.stringify(voice).includes("+1415555"));
  const verification = fixtureMessage(
    "verification",
    {
      call_result: call,
      risk_signals: ["carrier_insurance_not_confirmed"],
      bank_details_changed: false,
    },
    "unverified",
  );
  assert.match(verification.body, /Verification outcome: unverified/);
  assert.match(verification.body, /carrier insurance not confirmed/);
  assert.match(verification.body, /Bank details were not changed/);
});

test("Validator summary uses actual ok checks and does not borrow cross-case review evidence", () => {
  const validator = fixtureMessage(
    "validator",
    {
      review: {
        verdict: "issue",
        checks: [
          { name: "first", ok: true },
          { name: "second", ok: false },
          { name: "unrecorded" },
        ],
      },
    },
    "issue",
  );
  assert.match(validator.body, /1 of 2 recorded checks passed/);
  assert.match(validator.body, /Checks needing attention: second/);
  const snapshot = emptySnapshot();
  snapshot.task = [task];
  snapshot.review = [
    {
      id: "r",
      case_id: "another-case",
      target_task_id: "ap-1",
      verdict: "pass",
      ts: at,
    },
  ];
  const review = buildAgentMessages(snapshot).find(
    (message) => message.kind === "review",
  )!;
  assert.equal(review.recipientId, undefined);
  assert.deepEqual(review.evidenceRefs, []);
});

test("recorded A2A status is a status message and unread fingerprint stores no message body", () => {
  const snapshot = emptySnapshot();
  snapshot.audit_event = [
    {
      id: "sdk",
      agent: "carrier_ap",
      kind: "a2a_status",
      business_status: "investigating",
      a2a_state: "working",
      summary: "Task investigating.",
      ts: at,
    },
  ];
  const status = buildAgentMessages(snapshot)[0];
  assert.equal(status.kind, "status");
  assert.equal(status.title, "Investigating");
  const fingerprint = inboxActivityFingerprint({
    ...status,
    body: "private receipt number 7381249",
  });
  assert.ok(!fingerprint.includes("private"));
  assert.ok(!fingerprint.includes("7381249"));
  assert.match(fingerprint, /^audit:sdk:[0-9a-f]{8}$/);
});
