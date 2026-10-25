import assert from "node:assert/strict";
import test from "node:test";
import { apifyCaseIds, apifyEvidence, apifyHeadline, apifyRunId, isApifyCall } from "./apify";

// Shaped like a live `lookup_carrier_authority` audit summary (public_value already redacted contacts).
const carrierResult = {
  available: true,
  provider: "apify",
  actor_id: "humble-echidna~fmcsa-carrier-check",
  run_id: "hk6MfR4GuwJIg2EaC",
  cached: false,
  source_url: "https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string=3141592",
  source_simulated: false,
  identity_match: true,
  records: [
    {
      legal_name: "TN LOGISTICS INC",
      dba_name: null,
      authority_status: "not authorized",
      usdot: "3141592",
      docket_numbers: ["MC99574"],
      phone: "[redacted]",
      out_of_service: false,
      identity_match: true,
      authority_confirmed_active: false,
    },
  ],
  evidence_refs: ["apify://runs/hk6MfR4GuwJIg2EaC"],
};

test("run ids come only from apify://runs refs", () => {
  assert.equal(apifyRunId("apify://runs/hk6MfR4GuwJIg2EaC"), "hk6MfR4GuwJIg2EaC");
  assert.equal(apifyRunId("doc:POD-1"), null);
  assert.equal(apifyRunId("apify://runs/../x"), null);
  assert.equal(apifyRunId(42), null);
});

test("an Apify call is recognised by its tool or provider, not by carried evidence", () => {
  assert.ok(isApifyCall({ name: "lookup_carrier_authority" }));
  assert.ok(isApifyCall({ name: "other", result: { provider: "apify" } }));
  // execute_current_skill and inspect_review_target return the case's refs, Apify runs included.
  assert.ok(!isApifyCall({ name: "execute_current_skill", evidenceRefs: ["apify://runs/abcde12345"] }));
  assert.ok(!isApifyCall({ name: "audit_accessorials", result: { provider: "internal" } }));
  assert.equal(apifyEvidence({ name: "audit_accessorials", result: {} }), null);
});

test("a confirmed carrier check reads as the registry's record with its run", () => {
  const e = apifyEvidence({ name: "lookup_carrier_authority", status: "completed", result: carrierResult })!;
  assert.equal(e.outcome, "confirmed");
  assert.equal(e.check, "FMCSA carrier check");
  assert.equal(e.runId, "hk6MfR4GuwJIg2EaC");
  assert.equal(e.carriers[0].authorityActive, false);
  assert.equal(apifyHeadline(e), "TN LOGISTICS INC · USDOT 3141592 · MC99574 · not authorized");
});

test("a registration in another company's name is a mismatch with both names", () => {
  const e = apifyEvidence({
    name: "lookup_carrier_authority",
    status: "completed",
    result: {
      ...carrierResult,
      identity_match: false,
      identity_mismatch: { reason: "registered_name_differs", expected_names: ["Sierra Haul LLC"], registered_names: ["TN LOGISTICS INC"] },
    },
  })!;
  assert.equal(e.outcome, "mismatch");
  assert.deepEqual([e.expectedNames, e.registeredNames], [["Sierra Haul LLC"], ["TN LOGISTICS INC"]]);
});

test("no match, unavailable, running and trimmed results each say so", () => {
  const base = { name: "lookup_carrier_authority" };
  assert.equal(apifyEvidence({ ...base, status: "completed", result: { available: false, error: "no_matching_source_records", run_id: "RUN12345" } })!.outcome, "not_found");
  assert.equal(apifyEvidence({ ...base, status: "completed", result: { available: false, error: "apify_not_configured" } })!.outcome, "unavailable");
  assert.equal(apifyEvidence({ ...base, status: "started", result: null })!.outcome, "running");
  const trimmed = apifyEvidence({ ...base, status: "completed", result: { summary: "Tool returned a large result" }, evidenceRefs: ["apify://runs/RUN12345"] })!;
  assert.equal(trimmed.outcome, "recorded");
  assert.equal(trimmed.runId, "RUN12345", "the run survives in evidence_refs when the result was trimmed");
});

test("a website scrape reports counts of unverified contact candidates", () => {
  const e = apifyEvidence({
    name: "inspect_company_website",
    status: "completed",
    result: { available: true, provider: "apify", run_id: "RUNWEB123", records: [{ url: "https://pacefreight.com/", emails: ["[redacted]", "[redacted]"], phones: ["[redacted]"] }] },
  })!;
  assert.equal(e.check, "Website contact scrape");
  assert.equal(apifyHeadline(e), "pacefreight.com · 2 emails, 1 phone");
});

test("cases with Apify calls are found without parsing every audit row", () => {
  const ids = apifyCaseIds([
    { kind: "tool_call", case_id: "CASE-A", summary: '{"version":1,"tool_name":"lookup_carrier_authority","status":"completed"}' },
    { kind: "tool_call", case_id: "CASE-B", summary: '{"tool_name":"inspect_company_website","status":"started"}' },
    { kind: "tool_call", case_id: "CASE-E", summary: '{"tool_name":"execute_current_skill"}', evidence_refs: ["apify://runs/RUN12345"] },
    { kind: "tool_call", case_id: "CASE-C", summary: '{"tool_name":"audit_accessorials"}' },
    { kind: "append_audit", case_id: "CASE-D", summary: "lookup_carrier_authority mentioned in prose" },
  ]);
  assert.deepEqual([...ids].sort(), ["CASE-A", "CASE-B"]);
});

test("cases keep their Apify mark after the audit window drops the call", () => {
  const tasks = [
    { case_id: "CASE-OLD", evidence_refs: ["doc:RC-1", "apify://runs/S6tAG2sKueTPFgWk1"] },
    { case_id: "CASE-JSON", evidence_refs: '["apify://runs/RUN12345"]' },
    { case_id: "CASE-NONE", evidence_refs: ["doc:POD-1"] },
  ];
  assert.deepEqual([...apifyCaseIds([], tasks)].sort(), ["CASE-JSON", "CASE-OLD"]);
});
