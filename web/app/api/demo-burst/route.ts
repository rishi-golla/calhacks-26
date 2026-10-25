import { demoBurstProxy } from "@/lib/demo-burst-proxy";

export const dynamic = "force-dynamic";
export async function GET(request: Request) {
  return demoBurstProxy(request);
}
export async function POST(request: Request) {
  return demoBurstProxy(request);
}
