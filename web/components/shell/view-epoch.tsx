"use client";
/**
 * "Start fresh" controls. The epoch is shared (one `view_epoch` row for every
 * viewer; the simulation keeps its own); "Show earlier activity" is this
 * viewer's choice only. Scoping itself happens once, in the data provider.
 */
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Eraser } from "lucide-react";
import { OBSERVER_HINT, useOps } from "../data-provider";
import { fmtSince } from "@/lib/view-epoch";

/** A role refusal reads as a quiet hint, not the reducer's raw error. */
const errorText = (e: unknown) => {
  const message = String(e instanceof Error ? e.message : e).replace(/^Error: /, "");
  return /provision|role|not allowed|unauthori[sz]ed|forbidden|permission/i.test(message) ? OBSERVER_HINT : message;
};

/** Why the epoch can't be moved from here, or "" when it can. */
const unavailable = (support: ReturnType<typeof useOps>["viewEpoch"]["support"]) =>
  support === "absent"
    ? "needs module update"
    : support === "pending"
      ? "checking database…"
      : support === "read-only"
        ? "needs the live database"
        : support === "observer"
          ? OBSERVER_HINT
          : "";

/**
 * "Clear" in the work queue header: one click moves the shared Start fresh
 * point to now, so everything already in the queue drops out for every viewer
 * (cases still running carry over). Nothing is deleted; for a few seconds the
 * button offers Undo, which puts the previous point back exactly.
 */
export function ClearQueue({ count }: { count: number }) {
  const { viewEpoch } = useOps();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [undo, setUndo] = useState<{ previous: number | null } | null>(null);
  useEffect(() => {
    if (!undo) return;
    const t = window.setTimeout(() => setUndo(null), 8_000);
    return () => window.clearTimeout(t);
  }, [undo]);
  const reason = unavailable(viewEpoch.support);
  const act = async (action: () => Promise<void>, after: typeof undo) => {
    setBusy(true);
    setError("");
    try {
      await action();
      setUndo(after);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  if (undo)
    return (
      <span className="fl-micro fh-clear-done" role="status">
        Cleared ·{" "}
        <button type="button" className="fl-textbtn" disabled={busy} onClick={() => act(() => viewEpoch.restore(undo.previous), null)}>
          {busy ? "Undoing…" : "Undo"}
        </button>
      </span>
    );
  if (!count) return null;
  return (
    <>
      <button
        type="button"
        className="fl-btn quiet small fh-clear"
        disabled={busy || Boolean(reason)}
        onClick={() => act(viewEpoch.set, { previous: viewEpoch.epoch?.since ?? null })}
        title={reason || "Hide everything in the queue for everyone (Start fresh from now). Nothing is deleted."}
      >
        <Eraser size={12} aria-hidden /> {busy ? "Clearing…" : "Clear"}
      </button>
      {error && (
        <span className="fl-micro" role="alert" title={error}>
          Couldn’t clear
        </span>
      )}
    </>
  );
}

/** Top-bar control on /demo: "Start fresh", or the epoch with Undo (and Start fresh again) once one is set. */
export function StartFresh() {
  const { viewEpoch } = useOps();
  const { epoch, support } = viewEpoch;
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      setOpen(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  if (epoch)
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
        <EpochNote
          toggle={viewEpoch.showEarlier}
          undo={support === "ready" ? () => run(viewEpoch.clear) : undefined}
          restart={support === "ready" ? () => run(viewEpoch.set) : undefined}
          busy={busy}
        />
        {error && (
          <span className="fl-micro" role="alert">
            {error}
          </span>
        )}
      </span>
    );

  const ready = support === "ready";
  const reason = unavailable(support);
  return (
    <div ref={ref} style={{ position: "relative", display: "inline-flex", alignItems: "center", gap: 8 }}>
      <button
        className="fl-btn quiet small"
        onClick={() => setOpen((v) => !v)}
        disabled={!ready}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={ready ? "Hide activity before now for everyone. Nothing is deleted." : reason}
      >
        <Eraser size={14} /> Start fresh
      </button>
      {open && ready && (
        <div className="fl-popover" role="dialog" aria-label="Start fresh">
          <p style={{ margin: "0 0 12px", fontSize: 13 }}>Hide everything before now? Nothing is deleted.</p>
          <div style={{ display: "flex", gap: 6 }}>
            <button className="fl-btn small" onClick={() => run(viewEpoch.set)} disabled={busy}>
              <Eraser size={12} /> {busy ? "Starting…" : "Start fresh"}
            </button>
            <button className="fl-btn quiet small" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
          </div>
          {error && (
            <p className="fl-micro" style={{ margin: "10px 0 0" }} role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * "Since 05:58 · Show earlier", or with this viewer's opt-out
 * "All activity · Hide earlier". Renders nothing without an
 * epoch. `toggle` shows the viewer switch; `undo` adds the shared Undo;
 * `restart` adds "Start fresh" again, moving the point to now in one click.
 */
export function EpochNote({
  toggle = true,
  undo,
  restart,
  busy = false,
  style,
}: {
  toggle?: boolean;
  undo?: () => void;
  restart?: () => void;
  busy?: boolean;
  style?: CSSProperties;
}) {
  const { viewEpoch, now } = useOps();
  const { epoch, showEarlier, setShowEarlier } = viewEpoch;
  if (!epoch) return null;
  const at = fmtSince(epoch.since, now);
  return (
    <span className="fl-micro fl-epoch" style={style} role="status">
      {showEarlier ? "All activity" : `Since ${at}`}
      {toggle && (
        <>
          {" · "}
          <button type="button" className="fl-textbtn" onClick={() => setShowEarlier(!showEarlier)} title={showEarlier ? `Hide activity before ${at} for you` : "Show activity before the epoch, for you only"}>
            {showEarlier ? "Hide earlier" : "Show earlier"}
          </button>
        </>
      )}
      {undo && (
        <>
          {" · "}
          <button type="button" className="fl-textbtn" onClick={undo} disabled={busy} title="Remove the epoch for everyone">
            {busy ? "Undoing…" : "Undo"}
          </button>
        </>
      )}
      {restart && (
        <>
          {" · "}
          <button type="button" className="fl-textbtn" onClick={restart} disabled={busy} title="Move the start to now for everyone. Nothing is deleted.">
            Start fresh
          </button>
        </>
      )}
    </span>
  );
}
