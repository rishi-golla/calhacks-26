"use client";
/**
 * Shown work: every number an agent or the Validator reports, with the
 * formula, the inputs substituted in, the result, the rule it applies and the
 * documents it came from. The dashboard also recomputes simple formulas and
 * says when the stated result does not follow from the stated inputs.
 */
import { EvidenceChip } from "./evidence";
import {
  formatValue,
  isPolicyId,
  parseCheck,
  parseChecks,
  parseWorkings,
  readableName,
  recheckWorking,
  substituteFormula,
  type Check,
  type Working,
  type WorkingUnit,
} from "@/lib/workings";

export function BasisChip({ basis }: { basis: string }) {
  if (!basis) return null;
  if (isPolicyId(basis))
    return (
      <span className="fl-chip outline info wk-chip" title={`Policy ${basis}`}>
        {basis}
      </span>
    );
  return (
    <span className="wk-principle" title={`Basis: ${basis}`}>
      {basis}
    </span>
  );
}

function Recheck({ w }: { w: Working }) {
  const r = recheckWorking(w);
  if (r.status === "unchecked") return null;
  if (r.status === "match")
    return (
      <span className="wk-recheck ok" title="The dashboard recomputed this from the inputs shown and got the same result.">
        ✓ recomputed
      </span>
    );
  return (
    <span className="wk-recheck bad" title="The dashboard recomputed this formula from the inputs shown and got a different result.">
      ≠ recomputes to {formatValue(r.computed, typeof r.computed === "boolean" ? "bool" : w.unit)}
    </span>
  );
}

/** One working: label, substituted equation, symbolic formula, basis and sources. */
export function WorkingRow({ working, nested = false }: { working: Working; nested?: boolean }) {
  const w = working;
  const parts = w.formula ? substituteFormula(w) : [];
  return (
    <div className={`wk-row ${nested ? "nested" : ""}`}>
      <div className="wk-head">
        <span className="wk-label">{w.label}</span>
        <BasisChip basis={w.basis} />
        <Recheck w={w} />
      </div>
      <div className="wk-eq fl-mono">
        {parts.length > 0 && (
          <>
            <span className="wk-expr">
              {parts.map((p, i) =>
                p.kind === "value" ? (
                  <span key={i} className="wk-val" title={p.key}>
                    {p.text}
                  </span>
                ) : (
                  <span key={i} className={p.kind === "ident" ? "wk-ident" : "wk-opr"}>
                    {p.text}
                  </span>
                ),
              )}
            </span>
            <span className="wk-eqs">=</span>
          </>
        )}
        <strong className={`wk-result ${w.unit === "bool" ? (w.result === true ? "yes" : w.result === false ? "no" : "") : ""}`}>{formatValue(w.result, w.unit)}</strong>
      </div>
      {w.formula && (
        <div className="wk-sym fl-mono" title="Formula as published by the agent">
          {w.formula}
        </div>
      )}
      {w.evidence_refs.length > 0 && (
        <div className="wk-refs">
          {w.evidence_refs.map((r) => (
            <EvidenceChip key={r} evidenceRef={r} />
          ))}
        </div>
      )}
    </div>
  );
}

/** `findings.workings` as shown work. Accepts the raw (unvalidated) value; renders nothing when empty. */
export function Workings({ items, title = "Shown work" }: { items: unknown; title?: string | null }) {
  const list = parseWorkings(items);
  if (!list.length) return null;
  return (
    <section className="wk-block" aria-label={title ?? "Shown work"}>
      {title && (
        <div className="wk-title">
          {title} <span className="wk-count">{list.length}</span>
        </div>
      )}
      <ol className="wk-list">
        {list.map((w, i) => (
          <li key={`${w.label}:${i}`}>
            <WorkingRow working={w} />
          </li>
        ))}
      </ol>
    </section>
  );
}

const compact = (value: unknown, unit: WorkingUnit) => {
  if (value === undefined) return "—";
  const numeric = typeof value === "number" || (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value)));
  return formatValue(value, numeric && unit !== "bool" ? unit : typeof value === "boolean" ? "bool" : "text");
};

/** One Validator check: result, name, severity, basis, expected vs actual, and its working when present. */
export function CheckRow({ check }: { check: Check | Record<string, unknown> }) {
  // Accepts raw rows too: older rows lack severity/basis/work.
  const c = parseCheck(check);
  if (!c) return null;
  const work = c.work;
  const unit = work?.unit ?? "text";
  const tone = c.severity === "advisory" ? "info" : c.ok ? "warn" : "bad";
  const showValues = c.expected !== undefined || c.actual !== undefined;
  return (
    <div className={`wk-check ${c.ok ? "ok" : "fail"} ${c.severity}`}>
      <div className="wk-check-head">
        <span className="wk-glyph" aria-label={c.ok ? "passed" : "failed"}>
          {c.ok ? "✓" : "✗"}
        </span>
        <span className="wk-check-name" title={c.name}>
          {readableName(c.name)}
        </span>
        <span className={`fl-chip outline wk-chip wk-sev ${tone}`}>{c.severity}</span>
        <BasisChip basis={c.basis} />
      </div>
      {showValues && (
        <div className="wk-ea fl-mono">
          <span>
            <span className="wk-ea-k">expected</span> {compact(c.expected, unit)}
          </span>
          <span>
            <span className="wk-ea-k">actual</span> {compact(c.actual, unit)}
          </span>
        </div>
      )}
      {work && <WorkingRow working={work} nested />}
    </div>
  );
}

/** Checks with issues first and advisories folded into a collapsed group. */
export function CheckList({ checks }: { checks: unknown }) {
  const list = parseChecks(checks);
  if (!list.length) return null;
  const issues = list.filter((c) => c.severity !== "advisory");
  const advisories = list.filter((c) => c.severity === "advisory");
  return (
    <div className="wk-checks">
      {issues.map((c, i) => (
        <CheckRow key={`${c.name}:${i}`} check={c} />
      ))}
      {advisories.length > 0 && (
        <details className="wk-advisories">
          <summary>
            Advisories <span className="wk-count">{advisories.length}</span>
            {advisories.some((c) => !c.ok) && <span className="wk-adv-note">{advisories.filter((c) => !c.ok).length} not met</span>}
          </summary>
          {advisories.map((c, i) => (
            <CheckRow key={`${c.name}:${i}`} check={c} />
          ))}
        </details>
      )}
    </div>
  );
}
