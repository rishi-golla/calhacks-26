import { createReadStream } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import path from "node:path";
import { Readable } from "node:stream";
import { NextRequest, NextResponse } from "next/server";
import { isWithinDir, resolveEvidenceRequest } from "@/lib/evidence";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** Source directories, server-side only, resolved against the web process cwd. */
function directory(kind: "docs" | "mail"): string {
  const data = process.env.SIM_DATA_DIR || "../data";
  const configured = kind === "docs" ? process.env.DOCS_DIR || path.join(data, "docs") : process.env.MAIL_DIR || path.join(data, "mail");
  return path.resolve(process.cwd(), configured);
}

const MAX_BYTES = 25 * 1024 * 1024;

const fail = (status: 400 | 404, error: string) =>
  NextResponse.json({ error }, { status, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });

/**
 * Read-only access to the source document an evidence ref points at.
 * GET /api/evidence/doc:<REF>?view=pdf|json and /api/evidence/mail:<MAIL-ID>.
 * Never lists directories, never follows a path outside the configured folder,
 * and never echoes a filesystem path in an error.
 */
export async function GET(request: NextRequest, context: { params: Promise<{ ref: string }> }) {
  // Next has already percent-decoded the segment; anything still escaped fails validation.
  const { ref } = await context.params;
  const resolved = resolveEvidenceRequest(ref, request.nextUrl.searchParams.get("view"));
  if (!resolved.ok) return fail(resolved.status, resolved.error);
  const { file } = resolved;

  const root = directory(file.dir);
  const target = path.resolve(root, file.file);
  if (path.dirname(target) !== root || !isWithinDir(root, target, path.sep)) return fail(400, "invalid evidence reference");

  let real: string;
  let size: number;
  try {
    // Symlinks must not lead out of the folder either.
    const [realRoot, realTarget] = await Promise.all([realpath(root), realpath(target)]);
    if (!isWithinDir(realRoot, realTarget, path.sep)) return fail(404, "document not available");
    const info = await stat(realTarget);
    if (!info.isFile() || info.size > MAX_BYTES) return fail(404, "document not available");
    real = realTarget;
    size = info.size;
  } catch {
    return fail(404, "document not available");
  }

  const stream = Readable.toWeb(createReadStream(real)) as ReadableStream<Uint8Array>;
  return new NextResponse(stream, {
    status: 200,
    headers: {
      "Content-Type": file.contentType,
      "Content-Disposition": file.disposition,
      "Content-Length": String(size),
      "Cache-Control": "private, max-age=60",
      "X-Content-Type-Options": "nosniff",
      // The PDF is framed by the dashboard itself; nobody else should frame it.
      "Content-Security-Policy": "frame-ancestors 'self'",
      "X-Frame-Options": "SAMEORIGIN",
    },
  });
}

/** Same checks and headers as GET, no body: lets the viewer probe for a PDF before framing it. */
export async function HEAD(request: NextRequest, context: { params: Promise<{ ref: string }> }) {
  const response = await GET(request, context);
  await response.body?.cancel();
  return new NextResponse(null, { status: response.status, headers: response.headers });
}
