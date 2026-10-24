import { json, num, str, timestamp, type Row } from "./data";
export const cellValue = (value: unknown) => json<unknown>(value, value);
export const cellKey = (row: Row) =>
  JSON.stringify([row.sheet, row.row_key, row.column]);
/** Sequence breaks timestamp ties; a reducer may write several commits in one millisecond. */
export function chronologicalChanges(changes: Row[], sets: Row[]) {
  const byId = new Map(sets.map((set) => [str(set.id), set]));
  return [...changes].sort((a, b) => {
    const left = byId.get(str(a.change_set_id));
    const right = byId.get(str(b.change_set_id));
    return (
      timestamp(left?.ts) - timestamp(right?.ts) ||
      num(left?.sequence) - num(right?.sequence)
    );
  });
}
/** Follow the selected version's ancestry. Never include newer or unrelated commits. */
export function snapshotAt(id: string, sets: Row[], changes: Row[]) {
  const byId = new Map(sets.map((set) => [str(set.id), set]));
  const changesBySet = new Map<string, Row[]>();
  for (const change of changes) {
    const key = str(change.change_set_id);
    const list = changesBySet.get(key) ?? [];
    list.push(change);
    changesBySet.set(key, list);
  }
  const chain: string[] = [];
  const seen = new Set<string>();
  let current = id;
  const caseId = byId.get(id)?.case_id;
  while (
    current &&
    byId.has(current) &&
    !seen.has(current) &&
    byId.get(current)?.case_id === caseId
  ) {
    seen.add(current);
    chain.push(current);
    current = str(byId.get(current)?.parent_id, "");
  }
  const values = new Map<string, Row>();
  for (const setId of chain.reverse())
    for (const change of changesBySet.get(setId) ?? [])
      values.set(cellKey(change), change);
  return values;
}
export function compareSnapshots(
  from: string,
  to: string,
  sets: Row[],
  changes: Row[],
): Row[] {
  const a = snapshotAt(from, sets, changes);
  const b = snapshotAt(to, sets, changes);
  return [...new Set([...a.keys(), ...b.keys()])].flatMap((key) => {
    const before = a.get(key);
    const after = b.get(key);
    if (
      JSON.stringify(cellValue(before?.after)) ===
      JSON.stringify(cellValue(after?.after))
    )
      return [];
    const source = after ?? before!;
    return [
      {
        sheet: source.sheet,
        row_key: source.row_key,
        column: source.column,
        before: before ? cellValue(before.after) : null,
        after: after ? cellValue(after.after) : null,
      },
    ];
  });
}
