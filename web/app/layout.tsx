import type { Metadata } from "next";
import "./globals.css";
import "./freightline.css";
import "./mono.css";
import { fontClassNames } from "./fonts";
export const metadata: Metadata = {
  title: "FreightLine · Autonomous back office",
  description:
    "Live agent coordination, controlled financial operations, and a complete audit trail powered by SpacetimeDB.",
  robots: { index: false, follow: false },
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={fontClassNames}>
      <body>{children}</body>
    </html>
  );
}
