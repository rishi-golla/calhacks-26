"use client";
/**
 * Shown work: every number an agent or the Validator reports, laid out the way
 * a person would work it on paper. Sums and differences are a tally (one line
 * per input, operator in the margin, a rule, the result), comparisons show both
 * sides and the gap, per-line sums are a small table with totals, and anything
 * else is the inputs then the substituted equation. Each input cites the
 * document it was read from. The dashboard also recomputes the formula and
 * says when the stated result does not follow from the stated inputs.
 */
import { EvidenceChip } from "./evidence";
import {
  formatValue,
  isPolicyId,
  layoutWorking,
  parseCheck,
  parseChecks,
  parseWorkings,
  prettyOperators,
  readableName,
  recheckWorking,
  resultFacts,
  type Check,
  type Operand,
  type Recheck as RecheckResult,
  type Working,
  type WorkingLayout,
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

function Recheck({ w, r }: { w: Working; r: RecheckResult }) {
  if (r.status === "unchecked") return null;
  // Recomputing the dashboard's own arithmetic proves nothing; say where the figure came from instead.
  if (r.status === "match" && w.origin === "dashboard")
    return (
      <span className="wk-recheck calc" title="The dashboard calculated this figure from the inputs shown.">
        dashboard calc
      </span>
    );
  if (r.status === "match")
    return (
      <span className="wk-recheck ok" title="The dashboard recomputed this from the inputs shown and got the same result.">
        ✓ recomputed
      </span>
    );
  return (
    <span className="wk-recheck bad" title="The dashboard recomputed this formula from the inputs shown and got a different result.">
      {r.computed === undefined ? "≠ does not follow from its inputs" : `≠ recomputes to ${formatValue(r.computed, typeof r.computed === "boolean" ? "bool" : w.unit)}`}
    </span>
  );
}

/** `doc:RC-LD-1` -> "RC": the document an input line was read from, as a short chip. */
function Source({ source }: { source?: string }) {
  if (!source) return <span />;
  const short = /^doc:([A-Z]+)-/.exec(source)?.[1] ?? (source.startsWith("mail:") ? "mail" : source);
  return <EvidenceChip evidenceRef={source} label={short} className="wk-src" />;
}

const boolTone = (value: unknown) => (value === true ? "yes" : value === false ? "no" : "");

/** The stated result: a number, yes/no, a list, or an object as label/value facts (never JSON). */
function Result({ w }: { w: Working }) {
  const facts = resultFacts(w.result, w.unit);
  if (facts)
    return (
      <span className="wk-facts">
        {facts.map((f) => (
          <span key={f.key} className="wk-fact">
            <span className="wk-fact-k">{f.label}</span> <strong className={`wk-result ${f.tone ?? ""}`}>{f.value}</strong>
          </span>
        ))}
      </span>
    );
  return <strong className={`wk-result ${w.unit === "bool" ? boolTone(w.result) : ""}`}>{formatValue(w.result, w.unit)}</strong>;
}

function Side({ o }: { o: Operand }) {
  return (
    <span className="wk-side">
      <span className="wk-side-k">{o.label}</span>
      <span className="wk-side-v">
        <span className="wk-num" title={o.key}>
          {o.value}
        </span>
        <Source source={o.source} />
      </span>
    </span>
  );
}

function Comparison({ left, op, right, holds, gap, w }: { left: Operand; op: string; right: Operand; holds: boolean | undefined; gap?: string; w: Working }) {
  return (
    <div className="wk-cmp fl-mono">
      <Side o={left} />
      <span className="wk-cmp-op">{op}</span>
      <Side o={right} />
      {holds === undefined ? (
        <Result w={w} />
      ) : (
        <span className={`wk-verdict ${holds ? "yes" : "no"}`}>
          {holds ? "✓" : "✗"}
          {gap && <span className="wk-gap">{gap}</span>}
        </span>
      )}
    </div>
  );
}

/** What the agent stated, shown beside a verdict the dashboard recomputed and disputes. */
function Stated({ w }: { w: Working }) {
  return (
    <div className="wk-stated fl-mono">
      <span className="wk-side-k">Agent stated</span> <Result w={w} />
    </div>
  );
}

function Body({ w, layout, disputed }: { w: Working; layout: WorkingLayout; disputed: boolean }) {
  switch (layout.kind) {
    case "tally":
    case "note": {
      const lines = layout.kind === "tally" ? layout.lines : [{ ...layout.line, op: "" as const }];
      return (
        <div className="wk-tally fl-mono">
          {lines.map((l, i) => (
            <div key={`${l.key}:${i}`} className="wk-line">
              <span className="wk-op">{l.op}</span>
              <span className="wk-in">{l.label}</span>
              <span className="wk-num" title={l.key}>
                {l.value}
              </span>
              <Source source={l.source} />
            </div>
          ))}
          {layout.kind === "note" ? (
            <span className="wk-reason">{layout.reason}</span>
          ) : (
            <>
              <span className="wk-rule" aria-hidden />
              <div className="wk-line total">
                <span className="wk-op">=</span>
                <span className="wk-in" />
                <span className="wk-num">
                  <Result w={w} />
                </span>
                <span />
              </div>
            </>
          )}
        </div>
      );
    }
    case "compare":
      return (
        <>
          <Comparison {...layout} w={w} />
          {disputed && layout.holds !== undefined && <Stated w={w} />}
        </>
      );
    case "table":
      return (
        <>
          <div className="wk-table-wrap" tabIndex={0} role="region" aria-label={`${w.label}: lines`}>
            <table className="wk-table fl-mono">
              <thead>
                <tr>
                  <th scope="col" />
                  {layout.columns.map((c) => (
                    <th key={c.key} scope="col">
                      {c.label}
                    </th>
                  ))}
                  {layout.net && <th scope="col">{layout.net.label}</th>}
                </tr>
              </thead>
              <tbody>
                {layout.rows.map((r) => (
                  <tr key={r.key}>
                    <th scope="row" title={r.key}>
                      {r.label}
                    </th>
                    {r.cells.map((c, i) => (
                      <td key={i}>{c}</td>
                    ))}
                    {r.net !== undefined && <td className="wk-net">{r.net}</td>}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th scope="row">Total</th>
                  {layout.totals.map((t, i) => (
                    <td key={i}>{t}</td>
                  ))}
                  {layout.net && (
                    <td className="wk-net">
                      <Result w={w} />
                    </td>
                  )}
                </tr>
              </tfoot>
            </table>
          </div>
          {layout.comparison && <Comparison {...layout.comparison} w={w} />}
          {layout.comparison && disputed && <Stated w={w} />}
          {!layout.net && !layout.comparison && (
            <div className="wk-eq fl-mono">
              <span className="wk-eqs">=</span> <Result w={w} />
            </div>
          )}
        </>
      );
    case "set":
      return (
        <div className="wk-set fl-mono">
          <span className="wk-side-k">{layout.title}</span>
          <span className="wk-docs">
            {layout.items.map((d) => (
              <span key={d.name} className={`wk-doc ${d.present ? "yes" : "no"}`} title={d.present ? "received" : "missing"}>
                {d.present ? "✓" : "✗"} {d.label}
              </span>
            ))}
          </span>
          <span className="wk-set-out">
            <span className="wk-side-k">Missing</span> <Result w={w} />
          </span>
        </div>
      );
    case "expression":
      return (
        <>
          {layout.inputs.length > 0 && (
            <dl className="wk-inputs fl-mono">
              {layout.inputs.map((o) => (
                <div key={o.key}>
                  <dt title={o.key}>{o.label}</dt>
                  <dd>
                    <span className="wk-num">{o.value}</span>
                    <Source source={o.source} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <div className="wk-eq fl-mono">
            {layout.parts.length > 0 && (
              <>
                <span className="wk-expr">
                  {layout.parts.map((p, i) =>
                    p.kind === "value" ? (
                      <span key={i} className="wk-val" title={p.key}>
                        {p.text}
                      </span>
                    ) : (
                      <span key={i} className={p.kind === "ident" ? "wk-ident" : "wk-opr"}>
                        {p.kind === "text" ? prettyOperators(p.text) : p.text}
                      </span>
                    ),
                  )}
                </span>
                <span className="wk-eqs">=</span>
              </>
            )}
            <Result w={w} />
          </div>
        </>
      );
  }
}

/** One working: label, basis and recheck, the laid-out calculation, the formula as published, and its sources. */
export function WorkingRow({ working, nested = false }: { working: Working; nested?: boolean }) {
  const w = working;
  const layout = layoutWorking(w);
  const recheck = recheckWorking(w);
  return (
    <div className={`wk-row ${nested ? "nested" : ""}`} data-layout={layout.kind}>
      <div className="wk-head">
        <span className="wk-label">{w.label}</span>
        <BasisChip basis={w.basis} />
        <Recheck w={w} r={recheck} />
      </div>
      <Body w={w} layout={layout} disputed={recheck.status === "mismatch"} />
      {w.formula && (
        <div className="wk-sym fl-mono" title={w.origin === "dashboard" ? "Formula the dashboard used" : "Formula as published by the agent"}>
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
  const facts = resultFacts(value, unit);
  if (facts) return facts.map((f) => `${f.label} ${f.value}`).join(" · ");
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
