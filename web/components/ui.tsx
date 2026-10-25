"use client";
import { Flag, ArrowUpRight, Inbox, X, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { agentName, date, money, safeUrl, str, type Row } from "@/lib/data";
export type FlagTarget = { ref: string; caseId?: string };
export type FlagHandler = (target: FlagTarget) => void;
export function Badge({ value }: { value: unknown }) {
  const text = str(value, "unknown");
  return (
    <span
      className={`badge badge-${text.replace(/[^a-z_]/gi, "").toLowerCase()}`}
    >
      {text.replaceAll("_", " ")}
    </span>
  );
}
export function Empty({
  title = "Nothing here yet",
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Inbox size={28} strokeWidth={1.4} />
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Panel({
  title,
  eyebrow,
  action,
  children,
  className = "",
}: {
  title: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-head">
        <div>
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
export function FlagButton({
  target,
  onFlag,
}: {
  target: FlagTarget;
  onFlag: FlagHandler;
}) {
  return (
    <button
      className="icon-button flag-button"
      aria-label={`Flag ${target.ref}`}
      title="Send feedback to the Validator"
      onClick={() => onFlag(target)}
    >
      <Flag size={14} />
    </button>
  );
}
export function ExternalLink({
  href,
  children,
}: {
  href: unknown;
  children: ReactNode;
}) {
  const url = safeUrl(href);
  return url ? (
    <a href={url} target="_blank" rel="noreferrer" className="external-link">
      {children}
      <ArrowUpRight size={12} />
    </a>
  ) : (
    <span>{children}</span>
  );
}
export function Detail({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="detail">
      <span>{label}</span>
      <div>{children}</div>
    </div>
  );
}
export function JsonDetail({ value }: { value: unknown }) {
  return (
    <pre className="json-detail">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}
export function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="dialog"
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="Close dialog"
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Submit({
  busy,
  children,
  disabled = false,
}: {
  busy: boolean;
  children: ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      className="button primary"
      type="submit"
      disabled={busy || disabled}
    >
      {busy && <LoaderCircle size={15} className="spin" />}
      {children}
    </button>
  );
}
export type Column = {
  key: string;
  title?: string;
  format?: (value: unknown, row: Row) => ReactNode;
};
function columnMinWidth(key: string): number {
  if (
    [
      "summary",
      "note",
      "title",
      "detail",
      "route_reason",
      "evidence_refs",
    ].includes(key)
  )
    return 240;
  if (
    key === "id" ||
    key.endsWith("_id") ||
    key.endsWith("_ref") ||
    key === "approver"
  )
    return 180;
  if (key === "ts" || key.endsWith("_at")) return 160;
  return 128;
}
export const timeColumn = (key = "ts", title = "Updated"): Column => ({
  key,
  title,
  format: (value) => date(value),
});
export const moneyColumn = (key = "amount", title = "Amount"): Column => ({
  key,
  title,
  format: (value) => <span className="numeric">{money(value)}</span>,
});
export const statusColumn = (key = "status", title = "Status"): Column => ({
  key,
  title,
  format: (value) => <Badge value={value} />,
});
export const agentColumn: Column = {
  key: "agent",
  title: "Agent",
  format: (value) => agentName(value),
};
export function DataTable({
  rows,
  columns,
  table,
  onFlag,
  onRow,
  emptyTitle,
  emptyText,
}: {
  rows: Row[];
  columns: Column[];
  table?: string;
  onFlag?: FlagHandler;
  onRow?: (row: Row) => void;
  emptyTitle?: string;
  emptyText?: string;
}) {
  if (!rows.length) return <Empty title={emptyTitle}>{emptyText}</Empty>;
  return (
    <div
      className="table-scroll"
      tabIndex={0}
      role="region"
      aria-label={`${table?.replaceAll("_", " ") || "Data"} table, scroll horizontally for more columns`}
    >
      <table
        style={{
          minWidth:
            columns.reduce(
              (width, column) => width + columnMinWidth(column.key),
              0,
            ) + (onFlag || onRow ? 66 : 0),
        }}
      >
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={{ minWidth: columnMinWidth(column.key) }}
              >
                {column.title ?? column.key.replaceAll("_", " ")}
              </th>
            ))}
            {(onFlag || onRow) && (
              <th scope="col" className="row-actions">
                <span className="sr-only">Actions</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={str(
                row.event_id ?? row.id ?? row.task_id ?? row.case_id ?? row.name,
                String(i),
              )}
            >
              {columns.map((column, index) => (
                <td
                  key={column.key}
                  style={{ minWidth: columnMinWidth(column.key) }}
                >
                  {onRow && index === 0 ? (
                    <button
                      className="text-button table-link"
                      onClick={() => onRow(row)}
                    >
                      {column.format
                        ? column.format(row[column.key], row)
                        : str(row[column.key])}
                    </button>
                  ) : column.format ? (
                    column.format(row[column.key], row)
                  ) : (
                    str(row[column.key])
                  )}
                </td>
              ))}
              {(onFlag || onRow) && (
                <td className="row-actions">
                  {onRow && (
                    <button
                      className="icon-button"
                      aria-label={`Open ${str(row.id ?? row.task_id ?? row.case_id)}`}
                      onClick={() => onRow(row)}
                    >
                      <ArrowUpRight size={15} />
                    </button>
                  )}
                  {onFlag && (
                    <FlagButton
                      onFlag={onFlag}
                      target={{
                        ref: `${table}:${str(row.id ?? row.task_id ?? row.case_id ?? row.name, String(i))}`,
                        caseId: row.case_id ? str(row.case_id) : undefined,
                      }}
                    />
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function useClock() {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
