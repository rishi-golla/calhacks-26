const VIEWS = [
  "Agent network",
  "Live task feed",
  "Case drill-down",
  "Approvals inbox",
  "Ledger & holds",
  "Scoreboard",
  "Harness panel",
  "Demo console",
  "Counterparty panel",
  "Audit spreadsheets (blame / diff / history)",
  "Validator & learning",
  "Flags & human inbox",
];

export default function Home() {
  return (
    <main className="mx-auto max-w-3xl p-10 font-sans">
      <h1 className="text-2xl font-semibold">FreightOps dashboard</h1>
      <p className="mt-2 text-sm text-gray-500">
        Golden Gate Freight · autonomous back office. Views to build (W2), fed by SpacetimeDB subscriptions:
      </p>
      <ol className="mt-6 list-decimal space-y-1 pl-6">
        {VIEWS.map((v) => (
          <li key={v}>{v}</li>
        ))}
      </ol>
    </main>
  );
}
