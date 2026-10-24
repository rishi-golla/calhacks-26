"use client";
import { useMemo, useState } from "react";
import DataGrid, { type Column as GridColumn } from "react-data-grid";
import { Download, GitCommitHorizontal, FileText } from "lucide-react";
import { agentName, date, exportCsv, json, newest, str, type Row } from "@/lib/data";
import { useLive } from "./live-provider";
import { cellValue, chronologicalChanges, compareSnapshots } from "@/lib/audit";
import {
  AgentLabel,
  agentColumn,
  Badge,
  DataTable,
  Detail,
  Empty,
  ExternalLink,
  FlagButton,
  JsonDetail,
  Panel,
  timeColumn,
  type FlagHandler,
} from "./ui";
import "react-data-grid/lib/styles.css";
const sheets = [
  "ap_subledger",
  "ar_subledger",
  "gl",
  "holds",
  "cash_forecast",
  "payments",
];
type Cell = { row: string; column: string };
export default function AuditWorkspace({ onFlag }: { onFlag: FlagHandler }) {
  const { data } = useLive();
  const [sheet, setSheet] = useState("holds");
  const [caseId, setCaseId] = useState("");
  const [tab, setTab] = useState("blame");
  const [cell, setCell] = useState<Cell | null>(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const sets = useMemo(
    () =>
      newest(
        data.change_set.filter((row) => !caseId || row.case_id === caseId),
      ),
    [data.change_set, caseId],
  );
  const setMap = useMemo(
    () => new Map(data.change_set.map((row) => [str(row.id), row])),
    [data.change_set],
  );
  const changes = useMemo(
    () =>
      data.cell_change.filter(
        (row) =>
          row.sheet === sheet &&
          (!caseId || setMap.get(str(row.change_set_id))?.case_id === caseId),
      ),
    [data.cell_change, sheet, caseId, setMap],
  );
  const orderedChanges = useMemo(
    () => chronologicalChanges(changes, sets),
    [changes, sets],
  );
  const rows = useMemo(() => {
    const byRow = new Map<string, Row>();
    for (const change of orderedChanges) {
      const key = str(change.row_key);
      const row = byRow.get(key) ?? { row_key: key };
      row[str(change.column)] = cellValue(change.after);
      byRow.set(key, row);
    }
    return [...byRow.values()];
  }, [orderedChanges]);
  const columns = useMemo<GridColumn<Row>[]>(
    () =>
      [
        "row_key",
        ...new Set(
          changes
            .map((change) => str(change.column))
            .filter((key) => key !== "row_key"),
        ),
      ].map((key) => ({
        key,
        name: key.replaceAll("_", " "),
        frozen: key === "row_key",
        resizable: true,
        minWidth: key === "row_key" ? 160 : 130,
        renderCell: ({ row }) => (
          <span className={typeof row[key] === "number" ? "numeric" : ""}>
            {str(row[key])}
          </span>
        ),
      })),
    [changes],
  );
  const history = [...orderedChanges]
    .reverse()
    .filter(
      (change) =>
        cell && change.row_key === cell.row && change.column === cell.column,
    );
  const latest = history[0];
  const blame = latest ? setMap.get(str(latest.change_set_id)) : undefined;
  const docs = data.doc_read.filter(
    (doc) =>
      blame &&
      (doc.change_set_id === blame.id || doc.task_id === blame.task_id),
  );
  const fromId = from || str(sets[1]?.id, "");
  const toId = to || str(sets[0]?.id, "");
  const diffError =
    fromId && toId && setMap.get(fromId)?.case_id !== setMap.get(toId)?.case_id
      ? "Select two change sets from the same case to compare their history."
      : "";
  const diff = useMemo(() => {
    if (!fromId || !toId || diffError) return [];
    return compareSnapshots(fromId, toId, data.change_set, data.cell_change);
  }, [fromId, toId, diffError, data.change_set, data.cell_change]);
  return (
    <div className="stack">
      <div className="toolbar">
        <div className="segmented" aria-label="Audit sheets">
          {sheets.map((name) => (
            <button
              key={name}
              className={sheet === name ? "selected" : ""}
              onClick={() => {
                setSheet(name);
                setCell(null);
              }}
            >
              {name.replaceAll("_", " ")}
            </button>
          ))}
        </div>
        <select
          aria-label="Filter audit by case"
          value={caseId}
          onChange={(event) => {
            setCaseId(event.target.value);
            setCell(null);
            setFrom("");
            setTo("");
          }}
        >
          <option value="">All cases</option>
          {data.case.map((row) => (
            <option key={str(row.case_id)}>{str(row.case_id)}</option>
          ))}
        </select>
        <button
          className="button"
          disabled={!rows.length}
          onClick={() => exportCsv(`${sheet}.csv`, rows)}
        >
          <Download size={14} /> Export CSV
        </button>
      </div>
      <Panel
        title={sheet.replaceAll("_", " ")}
        eyebrow="Versioned operational books"
        action={
          <span className="muted small">
            {rows.length} rows · select any cell to trace it
          </span>
        }
      >
        {rows.length ? (
          <DataGrid
            aria-label={`${sheet} audit spreadsheet`}
            className="rdg-light audit-grid"
            columns={columns}
            rows={rows}
            rowKeyGetter={(row) => str(row.row_key)}
            onCellClick={({ row, column }) => {
              if (column.key !== "row_key") {
                setCell({ row: str(row.row_key), column: column.key });
                setTab("blame");
              }
            }}
            onSelectedCellChange={(args) => {
              if (args.row && args.column.key !== "row_key")
                setCell({
                  row: str(args.row.row_key),
                  column: args.column.key,
                });
            }}
            defaultColumnOptions={{ resizable: true }}
          />
        ) : (
          <Empty title="The books are ready for their first change">
            Atomic reducers publish cell changes here. This grid is
            reconstructed from those recorded changes.
          </Empty>
        )}
      </Panel>
      <Panel
        title={cell ? `${cell.row} / ${cell.column}` : "Trace every number"}
        action={
          cell ? (
            <FlagButton
              onFlag={onFlag}
              target={{
                ref: `cell:${sheet}:${cell.row}:${cell.column}`,
                caseId: blame ? str(blame.case_id) : undefined,
              }}
            />
          ) : undefined
        }
      >
        <div className="tabs" role="tablist" aria-label="Audit views">
          {["blame", "history", "diff", "log"].map((name) => (
            <button
              role="tab"
              aria-selected={tab === name}
              key={name}
              onClick={() => setTab(name)}
            >
              {name}
            </button>
          ))}
        </div>
        {tab === "blame" &&
          (blame ? (
            <div className="audit-blame">
              <div className="stack">
                <div className="commit">
                  <GitCommitHorizontal size={20} />
                  <code>{str(blame.id)}</code>
                  <Badge value={blame.action} />
                </div>
                <h3>{str(blame.summary)}</h3>
                <div className="detail-grid">
                  <Detail label="Author">
                    <AgentLabel id={blame.agent} />
                  </Detail>
                  <Detail label="Task">{str(blame.task_id)}</Detail>
                  <Detail label="Policy">{str(blame.policy_ids)}</Detail>
                  <Detail label="Instruction version">
                    {str(blame.instr_version)}
                  </Detail>
                  <Detail label="Recorded">{date(blame.ts)}</Detail>
                  <Detail label="Case">{str(blame.case_id)}</Detail>
                </div>
                <Detail label="Value change">
                  <span className="before">
                    {str(cellValue(latest.before))}
                  </span>{" "}
                  →{" "}
                  <span className="after">{str(cellValue(latest.after))}</span>
                </Detail>
                <details>
                  <summary>Integrity & provenance</summary>
                  <JsonDetail
                    value={{
                      hash: blame.hash,
                      parent_id: blame.parent_id,
                      memory_ids: blame.memory_ids,
                    }}
                  />
                </details>
              </div>
              <div className="source-docs">
                <span className="eyebrow">Source documents</span>
                {docs.length ? (
                  docs.map((doc) => (
                    <div className="doc" key={str(doc.id)}>
                      <FileText size={20} />
                      <div>
                        <ExternalLink href={doc.doc_ref}>
                          {str(doc.doc_ref)}
                        </ExternalLink>
                        <p>
                          Read by {agentName(doc.agent)} · page {str(doc.page)} ·{" "}
                          {str(doc.span)}
                        </p>
                        <details>
                          <summary>Extracted facts</summary>
                          <JsonDetail value={json(doc.extracted_json, {})} />
                        </details>
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="muted">
                    No source document reads are attached to this change set or
                    task yet.
                  </p>
                )}
              </div>
            </div>
          ) : (
            <Empty title="Select a value in the spreadsheet">
              Blame follows the value back to its author, task, policy and
              source document.
            </Empty>
          ))}
        {tab === "history" && (
          <DataTable
            rows={history.map((change) => ({
              ...change,
              ...setMap.get(str(change.change_set_id)),
              before: cellValue(change.before),
              after: cellValue(change.after),
            }))}
            columns={[
              { key: "change_set_id", title: "Change set" },
              { ...agentColumn, title: "Author" },
              { key: "before" },
              { key: "after" },
              timeColumn(),
            ]}
            emptyTitle="No cell selected"
            emptyText="Select a spreadsheet cell to see all versions of its value."
          />
        )}
        {tab === "diff" && (
          <div className="stack padded">
            <div className="toolbar">
              <label>
                From
                <select
                  value={fromId}
                  onChange={(event) => setFrom(event.target.value)}
                >
                  <option value="">Choose change set</option>
                  {sets.map((set) => (
                    <option key={str(set.id)} value={str(set.id)}>
                      {str(set.id)} · {str(set.case_id)} ·{" "}
                      {str(set.summary).slice(0, 40)}
                    </option>
                  ))}
                </select>
              </label>
              <span>→</span>
              <label>
                To
                <select
                  value={toId}
                  onChange={(event) => setTo(event.target.value)}
                >
                  <option value="">Choose change set</option>
                  {sets.map((set) => (
                    <option key={str(set.id)} value={str(set.id)}>
                      {str(set.id)} · {str(set.case_id)} ·{" "}
                      {str(set.summary).slice(0, 40)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="button"
                disabled={!diff.length}
                onClick={() => exportCsv(`diff-${fromId}-${toId}.csv`, diff)}
              >
                <Download size={14} /> Export diff
              </button>
            </div>
            {diffError ? (
              <p className="notice warning">{diffError}</p>
            ) : (
              <DataTable
                rows={diff}
                columns={[
                  { key: "sheet" },
                  { key: "row_key", title: "Row" },
                  { key: "column" },
                  {
                    key: "before",
                    format: (value) => (
                      <span className="before">{str(value)}</span>
                    ),
                  },
                  {
                    key: "after",
                    format: (value) => (
                      <span className="after">{str(value)}</span>
                    ),
                  },
                ]}
                emptyTitle="No differences to display"
                emptyText="Choose two versions of the same case. Values are compared from their recorded parent chains."
              />
            )}
          </div>
        )}
        {tab === "log" && (
          <DataTable
            rows={sets}
            table="change_set"
            onFlag={onFlag}
            columns={[
              { key: "id", title: "Commit" },
              { key: "case_id", title: "Case" },
              { ...agentColumn, title: "Author" },
              { key: "action" },
              { key: "summary" },
              { key: "parent_id", title: "Parent" },
              timeColumn(),
            ]}
            emptyTitle="No change sets recorded"
          />
        )}
      </Panel>
    </div>
  );
}
