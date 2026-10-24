import type { NextConfig } from "next";

const farm = process.env.BROWSERFARM_URL ?? "http://127.0.0.1:9300";

const nextConfig: NextConfig = {
  devIndicators: false,
  async rewrites() {
    return [{ source: "/farm/:path*", destination: `${farm}/:path*` }];
  },
};

export default nextConfig;
