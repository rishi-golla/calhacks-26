import { demoCallProxy } from "@/lib/demo-call-proxy";

export const dynamic = "force-dynamic";
export async function GET(request: Request) {
  return demoCallProxy(request);
}
export async function POST(request: Request) {
  return demoCallProxy(request);
}
