import { createReadStream } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import path from "node:path";
import { Readable } from "node:stream";
import { NextRequest, NextResponse } from "next/server";
import { isAllowedFetchSite, isWithinDir, resolveEvidenceRequest } from "@/lib/evidence";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * Source directories, server-side only. `DOCS_DIR` / `MAIL_DIR` resolve against the web
 * process cwd; a relative `SIM_DATA_DIR` resolves against the repo root (the parent of
 * `web/`), which is how every Python producer of the same variable resolves it.
 */
function directory(kind: "docs" | "mail"): string {
  const explicit = kind === "docs" ? process.env.DOCS_DIR : process.env.MAIL_DIR;
  if (explicit) return path.resolve(process.cwd(), explicit);
  const repoRoot = path.resolve(process.cwd(), "..");
  const data = path.resolve(repoRoot, process.env.SIM_DATA_DIR || "data");
  return path.join(data, kind);
}

const MAX_BYTES = 25 * 1024 * 1024;
const NO_STORE = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };

type Located =
  | { ok: false; status: 400 | 403 | 404; error: string }
  | { ok: true; path: string; headers: Record<string, string> };

/**
 * Validate the ref and find its file without opening it. Never lists directories, never
 * follows a path outside the configured folder, never echoes a filesystem path in an error.
 */
async function locate(request: NextRequest, context: { params: Promise<{ ref: string }> }): Promise<Located> {
  if (process.env.EVIDENCE_PREVIEWS === "off") return { ok: false, status: 404, error: "document previews are disabled" };
  if (!isAllowedFetchSite(request.headers.get("sec-fetch-site")))
    return { ok: false, status: 403, error: "evidence is served to the dashboard only" };
  // Next has already percent-decoded the segment; anything still escaped fails validation.
  const { ref } = await context.params;
  const resolved = resolveEvidenceRequest(ref, request.nextUrl.searchParams.get("view"));
  if (!resolved.ok) return { ok: false, status: resolved.status, error: resolved.error };
  const { file } = resolved;

  const root = directory(file.dir);
  const target = path.resolve(root, file.file);
  if (path.dirname(target) !== root || !isWithinDir(root, target, path.sep))
    return { ok: false, status: 400, error: "invalid evidence reference" };

  try {
    // Symlinks must not lead out of the folder either.
    const [realRoot, realTarget] = await Promise.all([realpath(root), realpath(target)]);
    if (!isWithinDir(realRoot, realTarget, path.sep)) return { ok: false, status: 404, error: "document not available" };
    const info = await stat(realTarget);
    if (!info.isFile() || info.size > MAX_BYTES) return { ok: false, status: 404, error: "document not available" };
    return {
      ok: true,
      path: realTarget,
      headers: {
        "Content-Type": file.contentType,
        "Content-Disposition": file.disposition,
        "Content-Length": String(info.size),
        "Cache-Control": "private, max-age=60",
        "X-Content-Type-Options": "nosniff",
        // The PDF is framed by the dashboard itself; nobody else should frame it.
        "Content-Security-Policy": "frame-ancestors 'self'",
        "X-Frame-Options": "SAMEORIGIN",
      },
    };
  } catch {
    return { ok: false, status: 404, error: "document not available" };
  }
}

const fail = (status: 400 | 403 | 404, error: string) => NextResponse.json({ error }, { status, headers: NO_STORE });

/** GET /api/evidence/doc:<REF>?view=pdf|json and /api/evidence/mail:<MAIL-ID>: stream the file read-only. */
export async function GET(request: NextRequest, context: { params: Promise<{ ref: string }> }) {
  const located = await locate(request, context);
  if (!located.ok) return fail(located.status, located.error);
  const stream = Readable.toWeb(createReadStream(located.path)) as ReadableStream<Uint8Array>;
  return new NextResponse(stream, { status: 200, headers: located.headers });
}

/** Same checks and headers as GET without opening the file: lets the viewer probe before framing. */
export async function HEAD(request: NextRequest, context: { params: Promise<{ ref: string }> }) {
  const located = await locate(request, context);
  if (!located.ok) return new NextResponse(null, { status: located.status, headers: NO_STORE });
  return new NextResponse(null, { status: 200, headers: located.headers });
}
