import {
  AGENTS,
  agentName,
  money,
  timestamp,
  type Row,
  type Snapshot,
} from "./data";

export type ToolCallStatus = "started" | "completed" | "failed" | "waiting" | "blocked";
export type ToolCall = {
  id: string;
  name: string;
  status: ToolCallStatus;
  arguments: Row;
  result: Row | null;
  durationMs?: number;
  history: { status: ToolCallStatus; at: number }[];
};

export type AgentMessage = {
  id: string;
  at: number;
  agentId: string;
  recipientId?: string;
  caseId: string;
  taskId?: string;
  kind: "handoff" | "status" | "result" | "audit" | "call" | "review" | "tool";
  title: string;
  body: string;
  evidenceRefs: string[];
  policyIds: string[];
  model?: string;
  syntheticSource?: boolean;
  liveExternalEvidence?: boolean;
  toolCall?: ToolCall;
  details?: unknown;
};

const knownAgents = new Set<string>(AGENTS.map((agent) => agent.id));
const terminalStates = new Set([
  "completed",
  "failed",
  "canceled",
  "cancelled",
  "rejected",
]);
const terminalBusiness = new Set(["resolved", "rejected", "failed"]);
const text = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";
const readable = (value: unknown): string => text(value).replaceAll("_", " ");
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? [
        ...new Set(
          value.filter(
            (item): item is string => typeof item === "string" && !!item.trim(),
          ),
        ),
      ]
    : [];
const object = (value: unknown): Row => {
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return {};
    }
  }
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Row)
    : {};
};
export const toolStatusLabel: Record<ToolCallStatus, string> = {
  started: "Running",
  completed: "Completed",
  failed: "Failed",
  waiting: "Waiting",
  blocked: "Blocked",
};
const toolStatusOrder: Record<ToolCallStatus, number> = {
  started: 0, waiting: 1, blocked: 2, failed: 3, completed: 4,
};
const sensitiveToolKey = /(?:password|secret|token|api.?key|authorization|credential|capability|phone|email|bank|account.?number|routing.?number|transcript|prompt|reasoning|instruction|encrypted|address)/i;
function safeToolText(value: string): string {
  return value
    .replace(/\b(?:sk-[A-Za-z0-9_-]+|Bearer\s+\S+)/gi, "[redacted]")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[redacted]")
    .replace(/(?:\+?\d[\s().-]*){10,}/g, "[redacted]")
    .slice(0, 700);
}
/** Defense in depth: only bounded, sanitized tool data reaches the inspector. */
function safeToolData(value: unknown, depth = 0, budget = { remaining: 6000 }): unknown {
  if (budget.remaining <= 0 || depth > 6) return "[truncated]";
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const safe = safeToolText(value).slice(0, budget.remaining);
    budget.remaining -= safe.length;
    return safe;
  }
  if (Array.isArray(value))
    return value.slice(0, 20).map((item) => safeToolData(item, depth + 1, budget));
  if (value && typeof value === "object")
    return Object.fromEntries(Object.entries(value).slice(0, 30).map(([key, item]) => {
      budget.remaining -= key.length;
      return [safeToolText(key).slice(0, 100), sensitiveToolKey.test(key)
        ? "[redacted]" : safeToolData(item, depth + 1, budget)];
    }));
  return null;
}
function toolEvent(audit: Row): ToolCall | undefined {
  // Do not render the raw summary when the version or shape is not understood.
  if (typeof audit.summary !== "string" || audit.summary.length > 50_000) return;
  const event = object(audit.summary);
  const status = text(event.status) as ToolCallStatus;
  if (event.version !== 1 || !Object.hasOwn(toolStatusLabel, status) ||
      text(audit.reason_code) !== `tool_${status}` ||
      !/^[a-zA-Z0-9_-]{1,160}$/.test(text(event.tool_call_id)) ||
      !/^[a-zA-Z0-9_-]{1,100}$/.test(text(event.tool_name)) ||
      !text(audit.task_id) || !event.arguments || typeof event.arguments !== "object" ||
      Array.isArray(event.arguments) ||
      (event.result !== null && (!event.result || typeof event.result !== "object" || Array.isArray(event.result)))) return;
  const duration = typeof event.duration_ms === "number" && Number.isFinite(event.duration_ms) && event.duration_ms >= 0
    ? event.duration_ms : undefined;
  return {
    id: text(event.tool_call_id), name: text(event.tool_name), status,
    arguments: safeToolData(event.arguments) as Row,
    result: event.result === null ? null : safeToolData(event.result) as Row,
    durationMs: duration,
    history: [{ status, at: timestamp(audit.ts) }],
  };
}
const protocolState = (row: Row): string =>
  text(row.a2a_state ?? row.a2_a_state ?? row.a_2_a_state).toLowerCase();
const statusLabels: Record<string, string> = {
  submitted: "Task received",
  queued: "Queued",
  working: "Working",
  investigating: "Investigating",
  awaiting_other_agent: "Waiting for another agent",
  awaiting_external_verification: "Waiting for evidence",
  awaiting_call: "Waiting for a call",
  awaiting_human_approval: "Needs human approval",
  "input-required": "Waiting for input",
  "auth-required": "Needs authorization",
  completed: "Completed",
  resolved: "Resolved",
  failed: "Failed",
  canceled: "Canceled",
  rejected: "Rejected",
};
function label(row: Row): string {
  const state = protocolState(row);
  const business = text(row.business_status);
  return terminalStates.has(state)
    ? (statusLabels[state] ?? readable(state))
    : (statusLabels[business] ??
        statusLabels[state] ??
        readable(business || state)) ||
        "Status recorded";
}
function reviewSummary(review: Row): string {
  const checks = Array.isArray(review.checks) ? review.checks.map(object) : [];
  const failed = checks
    .filter((check) => check.ok === false)
    .map((check) => readable(check.name))
    .filter(Boolean);
  const verdict = readable(review.verdict);
  const recorded = checks.filter((check) => typeof check.ok === "boolean");
  const passed = recorded.filter((check) => check.ok === true).length;
  return [
    verdict ? `Review verdict: ${verdict}.` : "",
    recorded.length
      ? `${passed} of ${recorded.length} recorded checks passed.`
      : "",
    failed.length ? `Checks needing attention: ${failed.join(", ")}.` : "",
  ]
    .filter(Boolean)
    .join(" ");
}

// Summary values must really be present and numeric. In particular, null, booleans,
// blank strings and malformed text must not turn into a fictitious zero-dollar fact.
function amount(value: unknown): number | undefined {
  if (
    typeof value !== "number" &&
    (typeof value !== "string" || !/^-?\d+(?:\.\d+)?$/.test(value.trim()))
  )
    return;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}
const dollars = (value: unknown): string | undefined => {
  const parsed = amount(value);
  return parsed === undefined ? undefined : money(parsed);
};
const listLabel = (value: unknown) => strings(value).map(readable).join(", ");

function mutationSummary(receipt: Row, noun: string, value?: unknown): string {
  if (!Object.keys(receipt).length) return "";
  const formatted = dollars(value ?? object(receipt.proposed_payload).amount);
  const suffix = formatted === undefined ? "" : ` for ${formatted}`;
  if (receipt.executed === true) return `${noun}${suffix} recorded.`;
  if (receipt.executed === false)
    return `${noun}${suffix} not executed${text(receipt.decision) && receipt.decision !== "allow" ? ` (${readable(receipt.decision)})` : ""}.`;
  return `${noun}${suffix} has no execution receipt.`;
}

function callSummary(call: Row): string {
  const result = object(call.findings);
  return [
    call.reached === true
      ? "Reported call result: contact reached."
      : call.reached === false
        ? "Reported call result: contact not reached."
        : "",
    result.claim_confirmed === true
      ? "The caller confirmed the claim."
      : result.claim_confirmed === false
        ? "The claim was not confirmed."
        : "",
    result.authorized_change === true
      ? "The caller reported authorizing the change."
      : "",
    result.receipt_provided === true
      ? "A receipt was reported in the call findings."
      : result.receipt_provided === false
        ? "No receipt was recorded."
        : "",
  ]
    .filter(Boolean)
    .join(" ");
}

/** Render structured findings as facts, not invented speech or claimed financial execution. */
function findingsSummary(
  agent: string,
  findings: Row,
  outcome: string,
): string {
  const parts: string[] = [];
  const add = (value: string) => {
    if (value) parts.push(value);
  };
  const currencyFact = (name: string, value: unknown) => {
    const formatted = dollars(value);
    if (formatted !== undefined) add(`${name}: ${formatted}.`);
  };
  if (agent === "carrier_ap") {
    const match = object(findings.match);
    currencyFact("Invoice", match.invoice_amount);
    currencyFact("Contract", match.contract_rate);
    currencyFact("Invoice difference", match.delta);
    if (findings.payment_blocked === true)
      add("Payment proposal blocked by the workflow.");
    const missing = strings(match.missing_documents);
    if (missing.length) add(`Missing documents: ${listLabel(missing)}.`);
    if (strings(findings.duplicate_invoice_ids).length)
      add(
        `Duplicate matches: ${strings(findings.duplicate_invoice_ids).join(", ")}.`,
      );
    if (findings.already_paid === true)
      add("The source invoice is already paid.");
    add(mutationSummary(object(findings.payment_proposal), "Payment proposal"));
    currencyFact("Recorded paid amount", findings.paid_amount);
  } else if (agent === "risk") {
    currencyFact("Unsupported charges", findings.unsupported_amount);
    const dwell = amount(findings.gps_dwell_min);
    const free = amount(findings.free_time_min);
    if (dwell !== undefined)
      add(
        `Recorded dwell: ${dwell} minutes${free !== undefined ? `; free time: ${free} minutes` : ""}.`,
      );
    else if (free !== undefined) add(`Contract free time: ${free} minutes.`);
    if (strings(findings.unsupported_charges).length)
      add(`Audit findings: ${listLabel(findings.unsupported_charges)}.`);
    if (findings.insufficient_timing_evidence === true)
      add("Timing evidence is insufficient.");
    if (findings.needs_verification === true)
      add("Independent verification is required.");
  } else if (agent === "documents") {
    if (findings.complete === true) add("Document packet complete.");
    const missing = strings(findings.missing);
    if (missing.length)
      add(`Missing documents (${missing.length}): ${listLabel(missing)}.`);
    else if (findings.complete === false) add("Document packet incomplete.");
    const documents = Array.isArray(findings.documents)
      ? findings.documents.map(object)
      : [];
    if (
      documents.some(
        (document) => document.kind === "pod" && document.signed === true,
      )
    )
      add("A signed POD is recorded.");
    const rejected = strings(findings.rejected_refs);
    if (rejected.length)
      add(
        `${rejected.length} document reference${rejected.length === 1 ? " was" : "s were"} rejected.`,
      );
  } else if (agent === "shipper_ar") {
    currencyFact("Invoice", findings.invoice_amount);
    currencyFact("Matched deposits", findings.matched_amount);
    currencyFact("Balance", findings.balance);
    if (findings.recommendation_only === true)
      add("Credit recommendation only; no credit limit changed.");
    const requests = Array.isArray(findings.journal_requests)
      ? findings.journal_requests.map(object)
      : [];
    if (requests.some((receipt) => receipt.executed === false))
      add("Some journal requests were not executed.");
  } else if (agent === "cfo") {
    currencyFact("Invoiced load margin", findings.invoiced_margin);
    currencyFact("Contracted load margin", findings.contracted_margin);
    currencyFact("Expected collections", findings.expected_collections);
    currencyFact("Net cash forecast", findings.net_cash);
  } else if (agent === "controller") {
    const hold = object(findings.hold);
    if (Object.keys(hold).length)
      add(mutationSummary(hold, "Hold", findings.hold_amount));
    else currencyFact("Proposed hold", findings.hold_amount);
    if (findings.payment_blocked === true)
      add("Payment proposal blocked by the workflow.");
    if (findings.hold_required === false)
      add("No hold required by this review.");
    if (findings.balanced === true) add("The recorded journal balances.");
    else if (findings.balanced === false)
      add("The recorded journal does not balance.");
    currencyFact("Debits", findings.debits);
    currencyFact("Credits", findings.credits);
    add(mutationSummary(object(findings.journal), "Journal entry"));
    add(mutationSummary(object(findings.release), "Hold release"));
    if (findings.already_posted === true)
      add("The journal entry was already posted.");
    if (findings.already_released === true)
      add("The hold was already released.");
    currencyFact("Recorded paid amount", findings.paid_amount);
  } else if (agent === "verification") {
    if (outcome) add(`Verification outcome: ${readable(outcome)}.`);
    if (strings(findings.risk_signals).length)
      add(`Risk signals: ${listLabel(findings.risk_signals)}.`);
    add(callSummary(object(findings.call_result)));
    if (findings.bank_details_changed === false)
      add("Bank details were not changed.");
  } else if (agent === "voice_desk") {
    add(callSummary(object(findings.call_result)));
    if (findings.requires_intake === true)
      add("This inbound call requires intake routing.");
  } else if (agent === "validator") {
    add(reviewSummary(object(findings.review)));
  }
  if (!parts.length && text(findings.reason)) add(text(findings.reason));
  return parts.join(" ");
}

/** Useful details are an allowlist; payloads, phone numbers and arbitrary nested data stay out. */
function findingDetails(findings: Row): Row {
  const result: Row = {};
  const numericKeys = [
    "invoice_amount",
    "contract_rate",
    "delta",
    "unsupported_amount",
    "gps_dwell_min",
    "free_time_min",
    "matched_amount",
    "balance",
    "invoiced_margin",
    "contracted_margin",
    "net_cash",
    "expected_collections",
    "hold_amount",
    "paid_amount",
    "debits",
    "credits",
  ];
  for (const [prefix, source] of [
    ["", findings],
    ["match.", object(findings.match)],
  ] as const) {
    for (const key of numericKeys) {
      const value = amount(source[key]);
      if (value !== undefined) result[prefix + key] = value;
    }
  }
  for (const key of [
    "payment_blocked",
    "already_paid",
    "complete",
    "hold_required",
    "balanced",
    "bank_details_changed",
    "recommendation_only",
  ]) {
    if (typeof findings[key] === "boolean") result[key] = findings[key];
  }
  for (const key of [
    "missing",
    "missing_documents",
    "unsupported_charges",
    "risk_signals",
    "duplicate_invoice_ids",
  ]) {
    const values = strings(findings[key]);
    if (values.length) result[key] = values;
  }
  const call = object(findings.call_result);
  if (typeof call.reached === "boolean") result.call_reached = call.reached;
  for (const key of ["hold", "payment_proposal", "journal", "release"]) {
    const receipt = object(findings[key]);
    if (typeof receipt.executed === "boolean")
      result[`${key}_executed`] = receipt.executed;
  }
  return result;
}

/** A chronological projection of published records, never a generated conversation. */
export function buildAgentMessages(snapshot: Snapshot): AgentMessage[] {
  const messages = new Map<string, AgentMessage>();
  const tasks = new Map(snapshot.task.map((row) => [text(row.task_id), row]));
  const syntheticCases = new Set(
    snapshot.case
      .filter((row) => isSyntheticCase(row))
      .map((row) => text(row.case_id)),
  );
  const add = (message: AgentMessage) => {
    if (!message.id || !knownAgents.has(message.agentId)) return;
    const previous = messages.get(message.id);
    if (!previous || message.at >= previous.at)
      messages.set(message.id, {
        ...message,
        ...(message.toolCall?.result?.available === true &&
          message.toolCall.result.provider === "apify" &&
          message.toolCall.result.source_simulated === false
          ? { liveExternalEvidence: true }
          : {}),
        ...(syntheticCases.has(message.caseId)
          ? { syntheticSource: true }
          : {}),
      });
  };
  for (const edge of snapshot.delegation_edge) {
    const id = text(edge.id);
    const agentId = text(edge.from_agent);
    const recipientId = text(edge.to_agent);
    if (!id || !knownAgents.has(recipientId)) continue;
    add({
      id: `handoff:${id}`,
      at: timestamp(edge.ts),
      agentId,
      recipientId,
      caseId: text(edge.case_id),
      taskId: text(edge.task_id) || undefined,
      kind: "handoff",
      title: `Handoff to ${agentName(recipientId)}`,
      body: `Requested ${readable(edge.skill) || "a specialist review"}.`,
      evidenceRefs: [],
      policyIds: [],
      details: {
        skill: edge.skill,
        parentTaskId: edge.parent_task_id,
        depth: edge.depth,
      },
    });
  }
  for (const task of snapshot.task) {
    const taskId = text(task.task_id);
    if (!taskId) continue;
    const artifact = object(task.artifact_json);
    const findings = object(artifact.findings);
    const assessment = object(findings.assessment);
    const review = object(findings.review);
    const callFindings = object(object(findings.call_result).findings);
    const outcome = text(artifact.outcome) || text(task.outcome);
    const model = text(artifact.model_used) || text(task.model_used);
    const simulated =
      findings.simulated === true ||
      callFindings.simulated === true ||
      model.startsWith("fixture/");
    const terminal =
      terminalStates.has(protocolState(task)) ||
      terminalBusiness.has(text(task.business_status));
    const hasArtifact = Object.keys(artifact).length > 0;
    const parent = tasks.get(text(task.parent_task_id));
    const recipientId =
      parent &&
      text(parent.case_id) === text(task.case_id) &&
      knownAgents.has(text(parent.agent))
        ? text(parent.agent)
        : undefined;
    const assessmentSummary =
      text(object(findings.tool_assessment).summary) || text(assessment.summary);
    const assessmentError = safeToolText(text(object(findings.tool_loop).assessment_error));
    const useRecordedFacts =
      !assessmentSummary ||
      model.startsWith("fixture/") ||
      assessmentSummary.startsWith("Fixture assessment for ");
    const rawSummary =
      (useRecordedFacts
        ? findingsSummary(text(task.agent), findings, outcome)
        : assessmentSummary) ||
      reviewSummary(review) ||
      text(callFindings.summary) ||
      text(object(callFindings.assessment).summary);
    const fallbackSummary = outcome
      ? `Recorded outcome: ${readable(outcome)}.`
      : "An artifact was published for this task.";
    const summary = `${simulated ? "Simulation: " : ""}${rawSummary || fallbackSummary}${assessmentError ? `\n\nSummary note: ${assessmentError}` : ""}`;
    add({
      id: `task:${taskId}:${terminal ? "result" : "status"}`,
      at: timestamp(task.updated_at) || timestamp(task.started_at),
      agentId: text(task.agent),
      ...(terminal && recipientId ? { recipientId } : {}),
      caseId: text(task.case_id),
      taskId,
      kind: terminal && hasArtifact ? "result" : "status",
      title: terminal && hasArtifact ? "Result published" : label(task),
      body:
        terminal && hasArtifact
          ? summary ||
            (outcome
              ? `Recorded outcome: ${readable(outcome)}.`
              : "An artifact was published for this task.")
          : `${label(task)}${text(task.skill) ? ` · ${readable(task.skill)}` : ""}.`,
      evidenceRefs: strings(artifact.evidence_refs ?? task.evidence_refs),
      policyIds: strings(artifact.policy_ids ?? task.policy_ids),
      model: model || undefined,
      details: {
        skill: task.skill,
        status: text(task.business_status) || protocolState(task),
        outcome,
        confidence: artifact.confidence ?? task.confidence,
        simulated,
        ...(assessmentError ? { assessmentError } : {}),
        facts: findingDetails(findings),
        review: Object.keys(review).length ? review : undefined,
      },
    });
  }
  const projectedActions = new Set([
    "upsert_task",
    "record_delegation",
    "agent_heartbeat",
    "upsert_call",
    "add_review",
  ]);
  const toolMessages = new Map<string, AgentMessage>();
  for (const audit of snapshot.audit_event) {
    const id = text(audit.id);
    if (!id || projectedActions.has(text(audit.kind))) continue;
    if (text(audit.kind) === "tool_call") {
      const tool = toolEvent(audit);
      if (!tool) {
        add({
          id: `audit:${id}`, at: timestamp(audit.ts), agentId: text(audit.agent),
          caseId: text(audit.case_id), taskId: text(audit.task_id) || undefined,
          kind: "tool", title: "Tool event unavailable",
          body: "This tool record has an unsupported or incomplete format.",
          evidenceRefs: [], policyIds: [],
        });
        continue;
      }
      const messageId = `tool:${text(audit.task_id)}:${tool.id}`;
      const previous = toolMessages.get(messageId);
      // A malformed cross-role/case collision must not reassign an existing call.
      if (previous && (previous.agentId !== text(audit.agent) || previous.caseId !== text(audit.case_id))) continue;
      const at = timestamp(audit.ts);
      const eventModel = text(object(audit.summary).model);
      const history = [...(previous?.toolCall?.history || []), ...tool.history]
        .filter((item, index, all) => all.findIndex((other) => other.at === item.at && other.status === item.status) === index)
        .sort((a, b) => a.at - b.at || toolStatusOrder[a.status] - toolStatusOrder[b.status]);
      const newer = !previous || at > previous.at ||
        (at === previous.at && toolStatusOrder[tool.status] > toolStatusOrder[previous.toolCall!.status]);
      if (!newer) {
        previous.toolCall!.history = history;
        continue;
      }
      tool.history = history;
      const body = {
        started: "Calling this tool with the task’s scoped permissions.",
        completed: "Tool returned a result. Inspect the output for actions and evidence.",
        failed: "The tool call failed. Inspect the recorded error for details.",
        waiting: "This call is waiting for a dependency, evidence, or approval.",
        blocked: "This tool call was blocked. Inspect the recorded reason.",
      }[tool.status];
      toolMessages.set(messageId, {
        id: messageId, at, agentId: text(audit.agent), caseId: text(audit.case_id),
        taskId: text(audit.task_id), kind: "tool", title: readable(tool.name), body,
        evidenceRefs: strings(audit.evidence_refs).map(safeToolText),
        policyIds: strings(audit.policy_ids).map(safeToolText),
        model: safeToolText(eventModel || text(audit.model_used)) || undefined,
        toolCall: tool,
      });
      continue;
    }
    const summary = text(audit.summary);
    if (!summary) continue;
    const recordedStatus = text(audit.kind) === "a2a_status";
    add({
      id: `audit:${id}`,
      at: timestamp(audit.ts),
      agentId: text(audit.agent),
      caseId: text(audit.case_id),
      taskId: text(audit.task_id) || undefined,
      kind: recordedStatus ? "status" : "audit",
      title: recordedStatus
        ? label(audit)
        : readable(audit.kind) || "Decision recorded",
      body: summary,
      evidenceRefs: strings(audit.evidence_refs),
      policyIds: strings(audit.policy_ids),
      model: text(audit.model_used) || undefined,
      details: {
        reasonCode: audit.reason_code,
        changeSetId: audit.change_set_id,
      },
    });
  }
  for (const message of toolMessages.values()) add(message);
  for (const call of snapshot.call) {
    const id = text(call.id);
    if (!id) continue;
    const result = object(call.findings_json);
    const findings = object(result.findings);
    const assessment = object(findings.assessment);
    const transcript = text(call.transcript_redacted);
    add({
      id: `call:${id}`,
      at: timestamp(call.ended_at) || timestamp(call.started_at),
      agentId: "voice_desk",
      caseId: text(call.case_id),
      taskId: text(call.task_id) || undefined,
      kind: "call",
      title: `Call ${readable(call.status) || "recorded"}`,
      body:
        text(assessment.summary) ||
        transcript ||
        `Call status: ${readable(call.status) || "not recorded"}.`,
      evidenceRefs: strings([result.transcript_ref]),
      policyIds: [],
      model: text(findings.model_used) || undefined,
      details: {
        status: call.status,
        purpose: call.persona,
        disclosureGiven: call.disclosure_given,
        transcript: transcript || undefined,
      },
    });
  }
  for (const review of snapshot.review) {
    const id = text(review.id);
    if (!id) continue;
    const target = tasks.get(text(review.target_task_id));
    const parsedChecks = (() => {
      try {
        return typeof review.checks_json === "string"
          ? JSON.parse(review.checks_json)
          : review.checks_json;
      } catch {
        return [];
      }
    })();
    const checks = Array.isArray(parsedChecks) ? parsedChecks.map(object) : [];
    const record = { verdict: review.verdict, checks };
    add({
      id: `review:${id}`,
      at: timestamp(review.ts),
      agentId: "validator",
      ...(target &&
      text(target.case_id) === text(review.case_id) &&
      knownAgents.has(text(target.agent))
        ? { recipientId: text(target.agent) }
        : {}),
      caseId: text(review.case_id),
      taskId: text(review.target_task_id) || undefined,
      kind: "review",
      title: "Quality review",
      body: reviewSummary(record) || "A quality review was recorded.",
      evidenceRefs:
        target && text(target.case_id) === text(review.case_id)
          ? strings(target.evidence_refs)
          : [],
      policyIds: [],
      details: record,
    });
  }
  return [...messages.values()].sort(
    (a, b) => a.at - b.at || a.id.localeCompare(b.id),
  );
}

/** Explicit source labels are independent of whether model inference is real. */
export function isSyntheticCase(row: Row): boolean {
  return /^\[(?:live\s+)?synthetic\b[^\]]*\]/i.test(text(row.title));
}

/** Online means a fresh service heartbeat, never merely a connected database. */
export function onlineAgentCount(
  snapshot: Snapshot,
  now: number,
  connected: boolean,
): number {
  if (!connected) return 0;
  return AGENTS.filter((agent) => {
    const row = snapshot.agent.find((item) => item.name === agent.id);
    if (!row || ["offline", "error", "stopped"].includes(text(row.status)))
      return false;
    const elapsed = now - timestamp(row.last_seen);
    return (
      timestamp(row.last_seen) > 0 && elapsed >= -60_000 && elapsed < 60_000
    );
  }).length;
}

export function messagesForAgent(
  messages: AgentMessage[],
  agentId = "all",
  caseId = "",
  query = "",
): AgentMessage[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return messages.filter((message) => {
    if (
      agentId !== "all" &&
      message.agentId !== agentId &&
      message.recipientId !== agentId
    )
      return false;
    if (caseId && message.caseId !== caseId) return false;
    const haystack = [
      message.title,
      message.body,
      message.caseId,
      message.taskId,
      message.toolCall?.name,
      message.toolCall ? toolStatusLabel[message.toolCall.status] : "",
      agentName(message.agentId),
      message.recipientId ? agentName(message.recipientId) : "",
      ...message.evidenceRefs,
      ...message.policyIds,
    ]
      .join(" ")
      .toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export function agentActivity(
  snapshot: Snapshot,
  id: string,
  now: number,
  connected: boolean,
): {
  label: string;
  state: "working" | "waiting" | "idle" | "offline";
} {
  if (!connected) return { label: "Disconnected", state: "offline" };
  const agent = snapshot.agent.find((row) => row.name === id);
  if (agent && ["offline", "error", "stopped"].includes(text(agent.status))) {
    return {
      label: text(agent.status) === "error" ? "Needs attention" : "Offline",
      state: "offline",
    };
  }
  const tasks = snapshot.task
    .filter((row) => row.agent === id)
    .sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at));
  const recent = (value: unknown) => {
    const at = timestamp(value);
    return at > 0 && now - at < 60_000 && now - at > -60_000;
  };
  const freshHeartbeat = !!agent && recent(agent.last_seen);
  const latest = tasks[0];
  const freshTask = !!latest && recent(latest.updated_at);
  if (!freshHeartbeat && !freshTask)
    return { label: "No recent update", state: "offline" };
  const unfinished = tasks.filter(
    (task) =>
      !terminalStates.has(protocolState(task)) &&
      !terminalBusiness.has(text(task.business_status)),
  );
  const active = unfinished.find(
    (task) =>
      (["working"].includes(protocolState(task)) &&
        !text(task.business_status).startsWith("awaiting_")) ||
      text(task.business_status) === "investigating",
  );
  if (active && (freshHeartbeat || recent(active.updated_at)))
    return { label: "Working", state: "working" };
  const waiting = unfinished.find(
    (task) =>
      text(task.business_status).startsWith("awaiting_") ||
      ["input-required", "auth-required"].includes(protocolState(task)),
  );
  if (waiting && (freshHeartbeat || recent(waiting.updated_at)))
    return { label: label(waiting), state: "waiting" };
  if (unfinished.length && (freshHeartbeat || freshTask))
    return { label: "Work queued", state: "waiting" };
  return { label: freshHeartbeat ? "Ready" : "Recently active", state: "idle" };
}

/** Include visible content so an updated stable record can become unread again. */
export function inboxActivityFingerprint(message: AgentMessage): string {
  const value = JSON.stringify([
    message.id,
    message.at,
    message.title,
    message.body,
    message.toolCall,
  ]);
  let hash = 0x811c9dc5;
  for (let i = 0; i < value.length; i++)
    hash = Math.imul(hash ^ value.charCodeAt(i), 0x01000193);
  return `${message.id}:${(hash >>> 0).toString(16).padStart(8, "0")}`;
}
