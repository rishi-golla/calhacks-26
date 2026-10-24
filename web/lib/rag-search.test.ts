import assert from "node:assert/strict";
import test from "node:test";
import { emptySnapshot } from "./data";
import {
  buildSearchChunks,
  rankBm25,
  reciprocalRankFuse,
  type SearchChunk,
} from "./rag-search";

test("search chunks keep case relations and redact secret-like values", () => {
  const snapshot = emptySnapshot();
  snapshot.task = [{ task_id: "task-1", case_id: "CASE-1" }];
  snapshot.invoice = [
    {
      id: "INV-1",
      carrier_id: "carrier-1",
      amount: 9200,
      status: "on_hold",
      case_id: "CASE-1",
      approval_secret: "should-not-appear",
    },
  ];
  snapshot.doc_read = [
    {
      id: "DOC-1",
      task_id: "task-1",
      extracted_json: '{"amount":9200,"api_token":"hidden"}',
    },
  ];

  const chunks = buildSearchChunks(snapshot);
  const invoice = chunks.find((chunk) => chunk.ref === "invoice:INV-1");
  const document = chunks.find((chunk) => chunk.ref === "doc_read:DOC-1");

  assert.equal(invoice?.case_id, "CASE-1");
  assert.match(invoice?.text ?? "", /amount: 9200/);
  assert.doesNotMatch(invoice?.text ?? "", /should-not-appear|approval_secret/);
  assert.equal(document?.case_id, "CASE-1");
  assert.match(document?.text ?? "", /amount/);
  assert.doesNotMatch(document?.text ?? "", /api_token|hidden/);
});

test("BM25 retrieves matching rows and rank fusion boosts the selected case", () => {
  const chunks: SearchChunk[] = [
    {
      ref: "invoice:INV-1",
      table: "invoice",
      id: "INV-1",
      case_id: "CASE-1",
      title: "INV-1",
      text: "table: invoice\nid: INV-1\nstatus: on_hold\namount: 9200",
    },
    {
      ref: "audit_event:AE-1",
      table: "audit_event",
      id: "AE-1",
      case_id: "CASE-2",
      title: "Validator flagged mismatch",
      text: "table: audit_event\nsummary: validator flagged mismatch",
    },
  ];
  const keyword = rankBm25(chunks, "invoice on hold");
  const fused = reciprocalRankFuse(keyword, [], "CASE-1");

  assert.equal(keyword[0].chunk.ref, "invoice:INV-1");
  assert.equal(fused[0].chunk.ref, "invoice:INV-1");
});
