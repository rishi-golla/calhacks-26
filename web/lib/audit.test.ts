import assert from "node:assert/strict";
import test from "node:test";
import {
  cellKey,
  chronologicalChanges,
  compareSnapshots,
  snapshotAt,
} from "./audit";
import { money, newest, normalize, safeUrl, type Row } from "./data";
const sets: Row[] = [
  {
    id: "first",
    parent_id: "",
    case_id: "a",
    ts: "2026-10-24T08:00:00.000Z",
    sequence: 1,
  },
  {
    id: "second",
    parent_id: "first",
    case_id: "a",
    ts: "2026-10-24T08:00:00.000Z",
    sequence: 2,
  },
  {
    id: "other",
    parent_id: "",
    case_id: "b",
    ts: "2026-10-24T08:00:00.000Z",
    sequence: 1,
  },
];
const changes: Row[] = [
  {
    change_set_id: "second",
    sheet: "holds",
    row_key: "h1",
    column: "amount",
    before: "300",
    after: "150",
  },
  {
    change_set_id: "first",
    sheet: "holds",
    row_key: "h1",
    column: "amount",
    before: "null",
    after: "300",
  },
  {
    change_set_id: "other",
    sheet: "holds",
    row_key: "h2",
    column: "amount",
    before: "null",
    after: "9400",
  },
];
test("latest audit value uses sequence when timestamps tie", () => {
  assert.equal(newest(sets)[0].id, "second");
  assert.equal(
    chronologicalChanges(
      changes.filter((row) => row.row_key === "h1"),
      sets,
    ).at(-1)?.after,
    "150",
  );
});
test("revision snapshot follows only its ancestor chain", () => {
  const first = snapshotAt("first", sets, changes);
  const second = snapshotAt("second", sets, changes);
  assert.equal(first.size, 1);
  assert.equal(second.size, 1);
  assert.equal(first.get(cellKey(changes[0]))?.after, "300");
  assert.equal(second.get(cellKey(changes[0]))?.after, "150");
});
test("diff compares before and after state, including reverse comparison", () => {
  assert.deepEqual(compareSnapshots("first", "second", sets, changes), [
    {
      sheet: "holds",
      row_key: "h1",
      column: "amount",
      before: 300,
      after: 150,
    },
  ]);
  assert.equal(
    compareSnapshots("second", "first", sets, changes)[0].after,
    300,
  );
  assert.deepEqual(compareSnapshots("second", "second", sets, changes), []);
});
test("corrupt cycles stop without pulling another case into the snapshot", () => {
  const cyclic = [{ ...sets[0], parent_id: "second" }, sets[1]];
  assert.equal(snapshotAt("second", cyclic, changes).size, 1);
  const crossCase = [{ ...sets[0], parent_id: "other" }, sets[2]];
  assert.equal(snapshotAt("first", crossCase, changes).size, 1);
});
test("SDK boundary retains USD amounts and normalizes generated field names", () => {
  assert.deepEqual(
    normalize({ caseId: "a", exposureUsd: 9400, instrVersion: 2n }),
    { case_id: "a", exposure_usd: 9400, instr_version: 2 },
  );
  assert.equal(money(9400), "$9,400.00");
});
test("source links refuse javascript and data URLs", () => {
  assert.equal(safeUrl("javascript:alert(1)"), undefined);
  assert.equal(safeUrl("data:text/html,bad"), undefined);
  assert.equal(
    safeUrl("https://example.com/source.pdf"),
    "https://example.com/source.pdf",
  );
});
