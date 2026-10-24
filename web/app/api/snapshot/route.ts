import { NextRequest, NextResponse } from "next/server";
import { TABLES, emptySnapshot, normalize, type Row } from "@/lib/data";
export const dynamic = "force-dynamic";
type SqlResult = {
  schema: { elements: { name: string | { some: string } }[] };
  rows: unknown[][];
};
/** Emergency read-only polling. No service token is used or exposed by this route. */
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
    // SpacetimeDB 2.10.2 accepts exactly one SQL statement per HTTP request.
    const results = await Promise.all(
      TABLES.map(async (name) => {
        const response = await fetch(
          `${base.replace(/\/$/, "")}/v1/database/${encodeURIComponent(database)}/sql`,
          {
            method: "POST",
            headers,
            body: `SELECT * FROM "${name}"`,
            cache: "no-store",
            signal: AbortSignal.timeout(4500),
          },
        );
        if (!response.ok)
          throw new Error(`Snapshot table ${name}: HTTP ${response.status}`);
        const statements = (await response.json()) as SqlResult[];
        if (!Array.isArray(statements) || statements.length !== 1)
          throw new Error("Unexpected SQL snapshot shape");
        return [name, statements[0]] as const;
      }),
    );
    const tables = emptySnapshot();
    for (const [name, result] of results) {
      const columns = result.schema.elements.map((column) =>
        typeof column.name === "string" ? column.name : column.name.some,
      );
      tables[name] = result.rows.map(
        (values) =>
          normalize(
            Object.fromEntries(columns.map((column, i) => [column, values[i]])),
          ) as Row,
      );
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
