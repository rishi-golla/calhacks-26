"use client";
/**
 * Evidence chips and the source viewer.
 *
 * <EvidenceChip evidenceRef="doc:RC-LD-1" /> renders a cited source. `doc:` and
 * `mail:` refs open <EvidenceViewer>, a right-side modal drawer that shows the
 * PDF (browser-native viewer), the ground-truth fields, or the raw email, all
 * read through the read-only /api/evidence route. Extracted fields sit in a
 * <DecryptBlock>: cipher text that decrypts under the cursor where Chrome's
 * html-in-canvas is on, plain fields elsewhere. Other sources render as a
 * plain chip. A missing file is an inline note, never a crash.
 *
 * The drawer is a native <dialog> opened with showModal(): it sits in the top
 * layer (escapes overflow and transforms of the panel that holds the chip) and
 * is portaled into the nearest themed root (trace drawer, `.fl`, `.agent-inbox`) so it keeps
 * that surface's tokens.
 */
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ExternalLink, FileText, Mail, X } from "lucide-react";
import { DecryptBlock } from "@/components/decrypt-block";
import { compactValue, documentKindLabel, evidenceUrl, extractedFields, parseEvidenceRef } from "@/lib/evidence";

/** Nearest themed ancestor; the trace drawer first so the agent accent carries into the viewer. */
const THEMED_ROOT = ".ct-details, .fl, .agent-inbox";

export function EvidenceChip({ evidenceRef, className = "" }: { evidenceRef: string; className?: string }) {
  const parsed = parseEvidenceRef(evidenceRef);
  const [host, setHost] = useState<Element | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  if (!parsed.previewable)
    return (
      <span className={`ev-chip static ${className}`} title="no preview for this source">
        {evidenceRef}
      </span>
    );
  const Icon = parsed.kind === "mail" ? Mail : FileText;
  return (
    <>
      <button
        ref={button}
        type="button"
        className={`ev-chip ${className}`}
        aria-haspopup="dialog"
        title={parsed.kind === "mail" ? "Open the source email" : "Open the source document"}
        onClick={(e) => {
          // Chips live inside clickable rows; the chip owns this click.
          e.stopPropagation();
          setHost(e.currentTarget.closest(THEMED_ROOT) ?? document.body);
        }}
      >
        <Icon size={11} aria-hidden />
        {evidenceRef}
      </button>
      {host && (
        <EvidenceViewer
          evidenceRef={evidenceRef}
          container={host}
          onClose={() => {
            setHost(null);
            button.current?.focus();
          }}
        />
      )}
    </>
  );
}

/** A row of chips; renders nothing for an empty list. */
export function EvidenceChips({ refs, className = "" }: { refs: readonly string[]; className?: string }) {
  if (!refs.length) return null;
  return (
    <span className={`ev-chips ${className}`}>
      {[...new Set(refs)].map((r) => (
        <EvidenceChip key={r} evidenceRef={r} />
      ))}
    </span>
  );
}

type Load<T> = { state: "loading" } | { state: "ready"; value: T } | { state: "missing" } | { state: "error" };

async function fetchSource<T>(url: string, as: "json" | "text" | "head", signal: AbortSignal): Promise<Load<T>> {
  try {
    const res = await fetch(url, { method: as === "head" ? "HEAD" : "GET", signal, cache: "no-store" });
    if (res.status === 404) return { state: "missing" };
    if (!res.ok) return { state: "error" };
    if (as === "head") return { state: "ready", value: true as T };
    return { state: "ready", value: (as === "json" ? await res.json() : await res.text()) as T };
  } catch (err) {
    if ((err as Error).name === "AbortError") return { state: "loading" };
    return { state: "error" };
  }
}

const MAIL_LIMIT = 60_000;

export function EvidenceViewer({ evidenceRef, onClose, container }: { evidenceRef: string; onClose: () => void; container?: Element | null }) {
  const parsed = parseEvidenceRef(evidenceRef);
  const dialog = useRef<HTMLDialogElement>(null);
  const [record, setRecord] = useState<Load<Record<string, unknown>>>({ state: "loading" });
  const [pdf, setPdf] = useState<Load<boolean>>({ state: "loading" });
  const [mail, setMail] = useState<Load<string>>({ state: "loading" });

  useEffect(() => {
    const d = dialog.current;
    // No close() in cleanup: under StrictMode the queued close event would dismiss the remounted drawer.
    if (d && !d.open) d.showModal();
  }, []);

  useEffect(() => {
    const ctl = new AbortController();
    if (parsed.kind === "doc") {
      fetchSource<Record<string, unknown>>(evidenceUrl(evidenceRef, "json"), "json", ctl.signal).then((r) => !ctl.signal.aborted && setRecord(r));
      fetchSource<boolean>(evidenceUrl(evidenceRef, "pdf"), "head", ctl.signal).then((r) => !ctl.signal.aborted && setPdf(r));
    } else if (parsed.kind === "mail") {
      fetchSource<string>(evidenceUrl(evidenceRef, "eml"), "text", ctl.signal).then((r) => !ctl.signal.aborted && setMail(r));
    }
    return () => ctl.abort();
  }, [evidenceRef, parsed.kind]);

  const kind = documentKindLabel(record.state === "ready" ? record.value.type : undefined, parsed);
  const pdfUrl = evidenceUrl(evidenceRef, "pdf");
  const body: ReactNode =
    parsed.kind === "doc" ? (
      <DocBody record={record} pdf={pdf} pdfUrl={pdfUrl} jsonUrl={evidenceUrl(evidenceRef, "json")} page={parsed.page} id={parsed.id} />
    ) : parsed.kind === "mail" ? (
      <MailBody mail={mail} url={evidenceUrl(evidenceRef, "eml")} />
    ) : (
      <p className="ev-note">No preview for this source.</p>
    );

  const node = (
    <dialog
      ref={dialog}
      className="ev-viewer"
      aria-label={`Source ${evidenceRef}`}
      onClose={onClose}
      onClick={(e: MouseEvent<HTMLDialogElement>) => {
        e.stopPropagation();
        if (e.target === e.currentTarget) dialog.current?.close();
      }}
      // Keep Escape and shortcuts from reaching the page's own window listeners (which close the node panel).
      onKeyDown={(e) => e.stopPropagation()}
    >
      <div className="ev-head">
        <div className="ev-head-text">
          <span className="ev-kicker">
            {parsed.kind === "mail" ? <Mail size={12} aria-hidden /> : <FileText size={12} aria-hidden />}
            Source · {kind}
          </span>
          <span className="ev-ref">{evidenceRef}</span>
        </div>
        <button type="button" className="ev-close" onClick={() => dialog.current?.close()} aria-label="Close source viewer" autoFocus>
          <X size={15} />
        </button>
      </div>
      <div className="ev-body">{body}</div>
    </dialog>
  );
  return container ? createPortal(node, container) : node;
}

const NOT_HERE = "Document not available on this dashboard host.";

function DocBody({
  record,
  pdf,
  pdfUrl,
  jsonUrl,
  page,
  id,
}: {
  record: Load<Record<string, unknown>>;
  pdf: Load<boolean>;
  pdfUrl: string;
  jsonUrl: string;
  page?: number;
  id: string;
}) {
  if (record.state === "loading" || pdf.state === "loading") return <p className="ev-loading">Loading source…</p>;
  const fields = record.state === "ready" ? extractedFields(record.value) : [];
  const nothing = pdf.state !== "ready" && record.state !== "ready";
  if (nothing)
    return (
      <p className="ev-note">
        {pdf.state === "missing" && record.state === "missing" ? NOT_HERE : "The source could not be loaded right now."}
        <span className="ev-note-sub">
          The agent cited <code>{id}</code>; its PDF and extracted fields live in the documents folder of the host that ran the agents.
        </span>
      </p>
    );
  return (
    <>
      <div className="ev-toolbar">
        {pdf.state === "ready" && (
          <a href={pdfUrl} target="_blank" rel="noreferrer" className="ev-link">
            Open PDF <ExternalLink size={11} aria-hidden />
          </a>
        )}
        {record.state === "ready" && (
          <a href={jsonUrl} target="_blank" rel="noreferrer" className="ev-link">
            Raw JSON <ExternalLink size={11} aria-hidden />
          </a>
        )}
        {page ? <span className="ev-page">cited page {page}</span> : null}
      </div>
      {pdf.state === "ready" ? (
        <iframe className="ev-pdf" src={page ? `${pdfUrl}#page=${page}` : pdfUrl} title={`PDF ${id}`} />
      ) : (
        <p className="ev-note">{pdf.state === "missing" ? "PDF not available on this dashboard host; extracted fields below." : "The PDF could not be loaded."}</p>
      )}
      {record.state === "ready" ? (
        <details className="ev-fields" open={pdf.state !== "ready"}>
          <summary>
            Extracted fields <span className="ev-count">{fields.length}</span>
          </summary>
          {fields.length ? (
            <DecryptBlock bgFrom=".ev-fields">
              <dl className="ev-kv">
                {fields.map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{compactValue(v)}</dd>
                  </div>
                ))}
              </dl>
            </DecryptBlock>
          ) : (
            <p className="ev-empty">No fields recorded.</p>
          )}
        </details>
      ) : (
        <p className="ev-empty">{record.state === "missing" ? "No extracted fields on this host." : "Extracted fields could not be loaded."}</p>
      )}
    </>
  );
}

function MailBody({ mail, url }: { mail: Load<string>; url: string }) {
  if (mail.state === "loading") return <p className="ev-loading">Loading message…</p>;
  if (mail.state !== "ready")
    return (
      <p className="ev-note">
        {mail.state === "missing" ? NOT_HERE : "The message could not be loaded right now."}
        <span className="ev-note-sub">Raw email lives in the mail folder of the host that received it.</span>
      </p>
    );
  const long = mail.value.length > MAIL_LIMIT;
  return (
    <>
      <div className="ev-toolbar">
        <a href={url} target="_blank" rel="noreferrer" className="ev-link">
          Open raw message <ExternalLink size={11} aria-hidden />
        </a>
        {long && <span className="ev-page">showing the first {MAIL_LIMIT.toLocaleString()} characters</span>}
      </div>
      <pre className="ev-mail">{long ? `${mail.value.slice(0, MAIL_LIMIT)}\n…` : mail.value}</pre>
    </>
  );
}
