import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import {
  compactValue,
  evidenceUrl,
  extractedFields,
  isSafeEvidenceId,
  isWithinDir,
  parseEvidenceRef,
  resolveEvidenceRequest,
} from "./evidence";

test("parseEvidenceRef recognizes doc and mail refs", () => {
  assert.deepEqual(parseEvidenceRef("doc:RC-LD-1"), { kind: "doc", id: "RC-LD-1", previewable: true });
  assert.deepEqual(parseEvidenceRef("mail:MAIL-ce01618854"), { kind: "mail", id: "MAIL-ce01618854", previewable: true });
  assert.deepEqual(parseEvidenceRef(" doc:INV_2026.10.24 "), { kind: "doc", id: "INV_2026.10.24", previewable: true });
  assert.deepEqual(parseEvidenceRef("doc:POD-7#p2"), { kind: "doc", id: "POD-7", previewable: true, page: 2 });
});

test("parseEvidenceRef leaves other sources without a preview", () => {
  for (const ref of ["fixture:clean_invoice", "call://CALL-1", "audit://AE-9", "source:fmcsa", "invoice:INV-1.pdf#p1", "", "doc:", "mail:"])
    assert.deepEqual(parseEvidenceRef(ref), { kind: "other", id: ref.trim(), previewable: false }, ref);
  assert.equal(parseEvidenceRef(undefined).previewable, false);
  assert.equal(parseEvidenceRef(42).kind, "other");
});

test("parseEvidenceRef rejects traversal and separators", () => {
  for (const ref of [
    "doc:../etc/passwd",
    "doc:..",
    "doc:a/../../b",
    "doc:a\\b",
    "doc:.hidden",
    "doc:RC..1",
    "mail:../../x",
    "doc:/etc/passwd",
    "doc:%2e%2e",
    "doc:a b",
    `doc:${"A".repeat(122)}`,
  ])
    assert.equal(parseEvidenceRef(ref).previewable, false, ref);
  assert.equal(isSafeEvidenceId("A".repeat(121)), true);
  assert.equal(isSafeEvidenceId("A".repeat(122)), false);
});

test("resolveEvidenceRequest maps views to files and status codes", () => {
  assert.deepEqual(resolveEvidenceRequest("doc:RC-1", "pdf"), {
    ok: true,
    file: { dir: "docs", file: "RC-1.pdf", contentType: "application/pdf", disposition: 'inline; filename="RC-1.pdf"' },
  });
  const json = resolveEvidenceRequest("doc:RC-1", "json");
  assert.ok(json.ok && json.file.file === "RC-1.json" && json.file.contentType.startsWith("application/json"));
  const pdfDefault = resolveEvidenceRequest("doc:RC-1", null);
  assert.ok(pdfDefault.ok && pdfDefault.file.file === "RC-1.pdf");
  const mail = resolveEvidenceRequest("mail:MAIL-1", null);
  assert.ok(mail.ok && mail.file.dir === "mail" && mail.file.file === "MAIL-1.eml" && mail.file.contentType.startsWith("text/plain"));
  assert.deepEqual(resolveEvidenceRequest("doc:RC-1", "exe"), { ok: false, status: 400, error: "invalid view" });
  assert.deepEqual(resolveEvidenceRequest("mail:MAIL-1", "pdf"), { ok: false, status: 400, error: "invalid view" });
  assert.equal((resolveEvidenceRequest("fixture:x", "pdf") as { status: number }).status, 404);
});

test("resolveEvidenceRequest returns 400 for traversal without leaking paths", () => {
  for (const ref of ["doc:../../etc", "doc:..%2F..%2Fetc", "doc:a/b", "doc:a\\b", "mail:..", "doc:RC\u0000", "call://x", "doc:" + "a".repeat(300)]) {
    const r = resolveEvidenceRequest(ref, "pdf");
    assert.equal(r.ok, false, ref);
    if (!r.ok) {
      assert.equal(r.status, 400, ref);
      assert.ok(!r.error.includes("/") && !r.error.includes("etc"), r.error);
    }
  }
});

test("isWithinDir blocks sibling prefixes and parent escapes", () => {
  const root = path.resolve("/srv/data/docs");
  assert.equal(isWithinDir(root, path.resolve(root, "RC-1.pdf"), path.sep), true);
  assert.equal(isWithinDir(root, root, path.sep), true);
  assert.equal(isWithinDir(root, path.resolve(root, "../mail/x.eml"), path.sep), false);
  assert.equal(isWithinDir(root, path.resolve("/srv/data/docs-evil/x.pdf"), path.sep), false);
  assert.equal(isWithinDir(root, path.resolve(root, "../../../etc/passwd"), path.sep), false);
  assert.equal(isWithinDir("", "/x", "/"), false);
});

test("evidenceUrl encodes the ref and drops page anchors", () => {
  assert.equal(evidenceUrl("doc:RC-1", "pdf"), "/api/evidence/doc%3ARC-1?view=pdf");
  assert.equal(evidenceUrl("doc:POD-7#p2", "json"), "/api/evidence/doc%3APOD-7?view=json");
  assert.equal(evidenceUrl("mail:MAIL-1", "eml"), "/api/evidence/mail%3AMAIL-1?view=eml");
});

test("extracted fields skip bookkeeping keys and render nested values compactly", () => {
  const fields = extractedFields({ type: "rate_confirmation", ref: "RC-1", pdf: "RC-1.pdf", total: 2000, carrier: { name: "Test Carrier", mc: "" } });
  assert.deepEqual(fields.map(([k]) => k), ["total", "carrier"]);
  assert.equal(compactValue(fields[1][1]), "{name: Test Carrier}");
  assert.equal(compactValue([1, true, null]), "[1, yes, —]");
  assert.equal(compactValue(1160.5), "1,160.5");
  assert.deepEqual(extractedFields(null), []);
});
