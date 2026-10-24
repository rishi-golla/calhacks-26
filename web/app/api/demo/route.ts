import { NextRequest, NextResponse } from "next/server";
const kinds = new Set([
  "phone",
  "email",
  "bank_change",
  "short_pay",
  "scenario",
]);
const enabled = () => process.env.DEMO_GATEWAY_ENABLED === "true";
const targets = () => ({
  phone: (process.env.DEMO_ALLOWLIST_PHONES || "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean),
  email: (process.env.DEMO_ALLOWLIST_EMAILS || "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean),
});
export async function GET() {
  return NextResponse.json({
    enabled: enabled(),
    targets: targets(),
    mode: "demo",
  });
}
export async function POST(request: NextRequest) {
  // Next can canonicalize nextUrl.hostname to localhost even when the browser
  // used 127.0.0.1. The actual Host header retains the address the user opened.
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  let sameOrigin = false;
  try {
    const parsedOrigin = new URL(origin || "");
    sameOrigin =
      Boolean(host) &&
      parsedOrigin.host === host &&
      parsedOrigin.protocol === request.nextUrl.protocol;
  } catch {
    /* Missing, opaque or malformed origins are rejected. */
  }
  if (!sameOrigin)
    return NextResponse.json(
      { error: "Same-origin request required" },
      { status: 403 },
    );
  if (!enabled())
    return NextResponse.json(
      {
        error:
          "W4 demo gateway is not enabled. Configure it after the real demo handler is ready.",
      },
      { status: 503 },
    );
  try {
    const body = await request.json();
    if (!kinds.has(body.kind) || !["phone", "email"].includes(body.channel))
      return NextResponse.json(
        { error: "Unsupported preset or channel" },
        { status: 400 },
      );
    if (
      !targets()[body.channel as "phone" | "email"].includes(body.demo_target)
    )
      return NextResponse.json(
        {
          error: "Choose a team destination from the configured demo allowlist",
        },
        { status: 400 },
      );
    if (
      body.kind === "scenario" &&
      (typeof body.scenario !== "string" ||
        body.scenario.trim().length < 10 ||
        body.scenario.length > 4000)
    )
      return NextResponse.json(
        { error: "Scenario must contain 10–4,000 characters" },
        { status: 400 },
      );
    const response = await fetch(
      `${(process.env.GATEWAY_URL || "http://localhost:9000").replace(/\/$/, "")}/demo/${body.kind}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          case_id: body.case_id || undefined,
          scenario: body.scenario,
          channel: body.channel,
          demo_target: body.demo_target,
        }),
        signal: AbortSignal.timeout(20000),
        cache: "no-store",
      },
    );
    const result = await response.json();
    if (!response.ok)
      return NextResponse.json(
        {
          error:
            result.error ||
            result.detail ||
            `Gateway returned HTTP ${response.status}`,
        },
        { status: response.status },
      );
    // The repository's original W4 stub returns only {queued: kind}; never claim a call was placed.
    const stub =
      Object.keys(result).length === 1 && result.queued === body.kind;
    return NextResponse.json({
      ...result,
      stub,
      message: stub
        ? "Gateway stub acknowledged the preset. No case or outreach has been confirmed."
        : "Gateway accepted the request. Follow the resulting case and call record for confirmation.",
    });
  } catch {
    return NextResponse.json(
      {
        error:
          "W4 gateway could not be reached. No outreach has been confirmed.",
      },
      { status: 502 },
    );
  }
}
