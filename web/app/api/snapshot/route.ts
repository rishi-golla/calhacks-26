import { NextRequest, NextResponse } from "next/server";
import { emptySnapshot, normalize, type Row } from "@/lib/data";
import {
  caseQueries,
  isLiveProfile,
  isSafeCaseId,
  liveWindowMinutes,
  subscriptionPlan,
  windowCutoff,
  type PlannedQuery,
} from "@/lib/live-queries";
export const dynamic = "force-dynamic";
type SqlResult = {
  schema: { elements: { name: string | { some: string } }[] };
  rows: unknown[][];
};
/** Cases whose full history one request may add (see `?case=`). */
const MAX_CASES = 4;
/**
 * Emergency read-only polling. No service token is used or exposed by this route.
 *
 * Reads the same rows the live subscription holds (lib/live-queries):
 * `?profile=app|ops` picks the table policy (default `ops`, the superset), and
 * `?case=<id>[,<id>]` adds those cases' full history.
 */
export async function GET(request: NextRequest) {
  const base = (
    process.env.SPACETIME_URL ||
    process.env.NEXT_PUBLIC_SPACETIME_URL ||
    "http://localhost:3000"
  )
    .replace(/^ws:/, "http:")
    .replace(/^wss:/, "https:");
  const database =
    process.env.SPACETIME_DB ||
    process.env.NEXT_PUBLIC_SPACETIME_DB ||
    "freightops";
  try {
    const headers: Record<string, string> = { "Content-Type": "text/plain" };
    const authorization = request.headers.get("authorization");
    if (authorization?.startsWith("Bearer "))
      headers.Authorization = authorization;
    const params = request.nextUrl.searchParams;
    const profileParam = params.get("profile");
    const profile = isLiveProfile(profileParam) ? profileParam : "ops";
    const cutoff = windowCutoff(Date.now(), liveWindowMinutes());
    const plan = subscriptionPlan(profile, cutoff);
    const cases = (params.get("case") ?? "")
      .split(",")
      .filter(isSafeCaseId)
      .slice(0, MAX_CASES);
    const queries: PlannedQuery[] = [
      ...plan.base,
      ...plan.windowed,
      ...cases.flatMap((id) => caseQueries(profile, id, cutoff)),
    ];
    const optional = new Set(plan.optional);
    // SpacetimeDB 2.10.2 accepts exactly one SQL statement per HTTP request.
    const results = await Promise.all(
      [...queries, ...optional].map(async (query) => {
        const { table: name, sql } = query;
        const response = await fetch(
          `${base.replace(/\/$/, "")}/v1/database/${encodeURIComponent(database)}/sql`,
          {
            method: "POST",
            headers,
            body: sql,
            cache: "no-store",
            signal: AbortSignal.timeout(4500),
          },
        );
        // An older module has no optional table: read it as empty.
        if (!response.ok && optional.has(query))
          return [name, { schema: { elements: [] }, rows: [] } as SqlResult] as const;
        if (!response.ok)
          throw new Error(`Snapshot table ${name}: HTTP ${response.status}`);
        const statements = (await response.json()) as SqlResult[];
        if (!Array.isArray(statements) || statements.length !== 1)
          throw new Error("Unexpected SQL snapshot shape");
        return [name, statements[0]] as const;
      }),
    );
    const tables = emptySnapshot();
    // A table can be read by several queries (its window plus a case's
    // history); every table those queries touch is keyed by `id`.
    const seen = new Map<string, Set<string>>();
    for (const [name, result] of results) {
      const columns = result.schema.elements.map((column) =>
        typeof column.name === "string" ? column.name : column.name.some,
      );
      const ids = seen.get(name) ?? new Set<string>();
      seen.set(name, ids);
      for (const values of result.rows) {
        const row = normalize(
          Object.fromEntries(columns.map((column, i) => [column, values[i]])),
        ) as Row;
        const id = typeof row.id === "string" ? row.id : "";
        if (id && ids.has(id)) continue;
        if (id) ids.add(id);
        tables[name].push(row);
      }
    }
    return NextResponse.json(
      { tables, source: "fallback", captured_at: new Date().toISOString() },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      {
        error: `SpacetimeDB is not reachable or its published schema differs. Check ${database} on ${base}, then reconnect.`,
      },
      { status: 503 },
    );
  }
}
