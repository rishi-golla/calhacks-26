"use client";
/**
 * Apify provenance in the UI: the Apify logomark on every tool call, chip and
 * evidence ref that went through Apify, and a finding card that says what the
 * agent confirmed (see `lib/apify.ts`).
 */
import { ExternalLink } from "lucide-react";
import { APIFY_OUTCOME, apifyHeadline, apifyRunUrl, type ApifyEvidence } from "@/lib/apify";
import "./apify.css";

/** Apify's logomark (apify.com/img/apify-logo/logomark-32x32.svg), in its brand colours. */
export function ApifyMark({ size = 12, title, className = "", x, y }: { size?: number; title?: string; className?: string; x?: number; y?: number }) {
  return (
    <svg
      x={x}
      y={y}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className={`apify-mark ${className}`}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      {title && <title>{title}</title>}
      <path d="M18.3512 0H31.5152C31.7829 0 32 0.217074 32 0.484848V20.6025C32 21.0844 31.3733 21.2712 31.1094 20.868L17.9455 0.750323C17.7345 0.427859 17.9659 0 18.3512 0Z" fill="#246DFF" />
      <path d="M13.6488 0H0.484848C0.217074 0 0 0.217074 0 0.484848V20.6025C0 21.0844 0.626717 21.2712 0.890559 20.868L14.0545 0.750323C14.2655 0.427859 14.0341 0 13.6488 0Z" fill="#20A34E" />
      <path d="M15.7745 16.1069L0.820235 31.1736C0.51656 31.4796 0.733277 32 1.16436 32H30.848C31.2773 32 31.4948 31.4832 31.1947 31.1762L16.4653 16.1095C16.2761 15.916 15.9651 15.9148 15.7745 16.1069Z" fill="#F86606" />
    </svg>
  );
}

/** "Apify run ↗" for an `apify://runs/<id>` ref, linking to the run in Apify Console. */
export function ApifyRunLink({ runId, className = "" }: { runId: string; className?: string }) {
  return (
    <a
      className={`apify-run ${className}`}
      href={apifyRunUrl(runId)}
      target="_blank"
      rel="noreferrer"
      title={`Apify run ${runId} (opens in Apify Console)`}
      onClick={(e) => e.stopPropagation()}
    >
      <ApifyMark size={11} />
      <span className="fl-mono">run {runId}</span>
      <ExternalLink size={10} aria-hidden />
    </a>
  );
}

export function ApifyOutcomeChip({ evidence }: { evidence: ApifyEvidence }) {
  const o = APIFY_OUTCOME[evidence.outcome];
  return <span className={`fl-chip outline ${o.tone} apify-outcome`}>{o.label}</span>;
}

/** What the agent learned from one Apify call: outcome, the public record, and links to the run and the source. */
export function ApifyFinding({ evidence }: { evidence: ApifyEvidence }) {
  const c = evidence.carriers[0];
  const source = c?.sourceUrl || evidence.sourceUrl;
  return (
    <div className={`apify-finding ${evidence.outcome}`}>
      <div className="apify-finding-head">
        <ApifyMark size={14} title="Apify" />
        <span className="apify-finding-check">Apify · {evidence.check}</span>
        <ApifyOutcomeChip evidence={evidence} />
      </div>
      {evidence.outcome !== "running" && <p className="apify-finding-line">{apifyHeadline(evidence)}</p>}
      {evidence.outcome === "mismatch" && (evidence.expectedNames.length > 0 || evidence.registeredNames.length > 0) && (
        <p className="apify-finding-note">
          Case carrier <strong>{evidence.expectedNames.join(" / ") || "—"}</strong>, but this registration belongs to{" "}
          <strong>{evidence.registeredNames.join(" / ") || "another company"}</strong>.
        </p>
      )}
      {c && c.authorityActive === false && evidence.outcome === "confirmed" && (
        <p className="apify-finding-note">Registered, but operating authority is not active.</p>
      )}
      {c?.outOfService && <p className="apify-finding-note">Out-of-service order on file.</p>}
      <div className="apify-finding-links">
        {evidence.runId && <ApifyRunLink runId={evidence.runId} />}
        {source && /^https:\/\//.test(source) && (
          <a className="apify-run" href={source} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
            <span>{/safer\.fmcsa/.test(source) ? "FMCSA SAFER" : "source"}</span>
            <ExternalLink size={10} aria-hidden />
          </a>
        )}
        {evidence.cached && <span className="fl-micro fl-mute">cached run</span>}
      </div>
    </div>
  );
}
