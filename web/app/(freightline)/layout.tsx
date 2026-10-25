import type { ReactNode } from "react";
import { DataProvider } from "@/components/data-provider";
import Shell from "@/components/shell/shell";

export default function FreightLineLayout({ children }: { children: ReactNode }) {
  return (
    <DataProvider>
      <Shell>{children}</Shell>
    </DataProvider>
  );
}
