import { NextRequest, NextResponse } from "next/server";
import { loadSnapshot } from "@/lib/server-snapshot";
export const dynamic = "force-dynamic";
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
    const tables = await loadSnapshot(
      request.headers.get("authorization") ?? undefined,
    );
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
