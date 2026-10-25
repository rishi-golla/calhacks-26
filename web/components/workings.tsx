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
      <span className="wk-recheck ok" title="Recomputed from the inputs shown: same result">
        ✓
      </span>
    );
  return (
    <span className="wk-recheck bad" title="The dashboard recomputed this formula from the inputs shown and got a different result.">
      ≠ recomputes to {formatValue(r.computed, typeof r.computed === "boolean" ? "bool" : w.unit)}
    </span>
  );
}

/** One working: label, substituted equation (symbolic formula on hover), basis and sources. */
export function WorkingRow({ working, nested = false }: { working: Working; nested?: boolean }) {
  const w = working;
  const parts = w.formula ? substituteFormula(w) : [];
  return (
    <div className={`wk-row ${nested ? "nested" : ""}`}>
      <div className="wk-head">
        <span className="wk-label" title={w.basis && !isPolicyId(w.basis) ? w.basis : undefined}>
          {w.label}
        </span>
        {isPolicyId(w.basis) && <BasisChip basis={w.basis} />}
        <Recheck w={w} />
      </div>
      <div className="wk-eq fl-mono" title={w.formula || undefined}>
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

const brief = (value: unknown, unit: WorkingUnit) => {
  if (value === undefined) return "—";
  const numeric = typeof value === "number" || (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value)));
  return formatValue(value, numeric && unit !== "bool" ? unit : typeof value === "boolean" ? "bool" : "text");
};

/** One Validator check. Full form (failures): name, basis, expected vs actual and its working. Compact (passes): name only, detail on hover. */
export function CheckRow({ check, compact = false }: { check: Check | Record<string, unknown>; compact?: boolean }) {
  // Accepts raw rows too: older rows lack severity/basis/work.
  const c = parseCheck(check);
  if (!c) return null;
  const work = c.work;
  const unit = work?.unit ?? "text";
  const showValues = c.expected !== undefined || c.actual !== undefined;
  const hover = [c.basis, showValues ? `expected ${brief(c.expected, unit)} · actual ${brief(c.actual, unit)}` : ""].filter(Boolean).join("\n");
  if (compact)
    return (
      <div className={`wk-check compact ${c.ok ? "ok" : "fail"} ${c.severity}`} title={hover || undefined}>
        <div className="wk-check-head">
          <span className="wk-glyph" aria-label={c.ok ? "passed" : "failed"}>
            {c.ok ? "✓" : "✗"}
          </span>
          <span className="wk-check-name">{readableName(c.name)}</span>
        </div>
      </div>
    );
  return (
    <div className={`wk-check ${c.ok ? "ok" : "fail"} ${c.severity}`}>
      <div className="wk-check-head">
        <span className="wk-glyph" aria-label={c.ok ? "passed" : "failed"}>
          {c.ok ? "✓" : "✗"}
        </span>
        <span className="wk-check-name" title={c.name}>
          {readableName(c.name)}
        </span>
        {c.severity === "advisory" && <span className="fl-chip outline wk-chip wk-sev info">advisory</span>}
        <BasisChip basis={c.basis} />
      </div>
      {showValues && (
        <div className="wk-ea fl-mono">
          <span>
            <span className="wk-ea-k">expected</span> {brief(c.expected, unit)}
          </span>
          <span>
            <span className="wk-ea-k">actual</span> {brief(c.actual, unit)}
          </span>
        </div>
      )}
      {work && <WorkingRow working={work} nested />}
    </div>
  );
}

/** Failed checks in full, blocking ones first; everything that passed folded into one line. */
export function CheckList({ checks }: { checks: unknown }) {
  const list = parseChecks(checks);
  if (!list.length) return null;
  const failed = list.filter((c) => !c.ok).sort((a, b) => Number(a.severity === "advisory") - Number(b.severity === "advisory"));
  const passed = list.filter((c) => c.ok);
  return (
    <div className="wk-checks">
      {failed.map((c, i) => (
        <CheckRow key={`${c.name}:${i}`} check={c} />
      ))}
      {passed.length > 0 && (
        <details className="wk-advisories wk-passed">
          <summary>
            <span className="wk-passed-n">✓ {failed.length ? passed.length : `All ${passed.length}`}</span> passed
          </summary>
          {passed.map((c, i) => (
            <CheckRow key={`${c.name}:${i}`} check={c} compact />
          ))}
        </details>
      )}
    </div>
  );
}
