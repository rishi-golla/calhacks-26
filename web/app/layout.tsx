import type { Metadata } from "next";
import "./globals.css";
import "./theme.css";
export const metadata: Metadata = {
  title: "Golden Gate Freight · Your AI Back Office",
  description:
    "Live agent coordination, controlled financial operations, and a complete audit trail powered by SpacetimeDB.",
  robots: { index: false, follow: false },
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
