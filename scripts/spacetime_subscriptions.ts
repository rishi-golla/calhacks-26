/** Real generated-client subscription latency check. Use a disposable W2 test database. */
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { DbConnection } from '../web/module_bindings';

async function main() {
  const database = process.argv[2];
  const server = process.argv[3] || 'http://127.0.0.1:3000';
  if (!database?.startsWith('freightops-test-')) throw new Error('Provide a disposable freightops-test-* database');
  const directory = resolve('data/w2', database);
  const identities = JSON.parse(await readFile(resolve(directory, 'identities.json'), 'utf8'));
  const pending = new Map<string, { start: number; done: (latency: number) => void }>();
  let conn: DbConnection | undefined;
  try {
    await new Promise<void>((ready, fail) => {
      const timeout = setTimeout(() => fail(new Error('Subscription did not become ready in 15 seconds')), 15000);
      conn = DbConnection.builder().withUri(server).withDatabaseName(database)
        .onConnect(client => {
          const observe = (name: string) => {
            const operation = pending.get(name);
            if (operation) { pending.delete(name); operation.done(performance.now() - operation.start); }
          };
          client.db.metric.onInsert((_ctx, row) => observe(row.name));
          client.db.metric.onUpdate((_ctx, _before, row) => observe(row.name));
          client.subscriptionBuilder().onApplied(() => { clearTimeout(timeout); ready(); })
            .onError(ctx => { clearTimeout(timeout); fail(new Error(String(ctx.event))); })
            .subscribe(['SELECT * FROM "metric"', 'SELECT * FROM "case"']);
        })
        .onConnectError((_ctx, error) => { clearTimeout(timeout); fail(error); }).build();
    });
    const latencies: number[] = [];
    for (let index = 0; index < 12; index++) {
      const name = `w2-subscription-${crypto.randomUUID()}`;
      const start = performance.now();
      let timer: ReturnType<typeof setTimeout>;
      const update = new Promise<number>((done, fail) => {
        pending.set(name, { start, done: value => { clearTimeout(timer); done(value); } });
        timer = setTimeout(() => fail(new Error('Committed reducer did not reach the subscription')), 5000);
      });
      const response = await fetch(`${server}/v1/database/${database}/call/set_metric`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${identities.evaluator.token}` },
        body: JSON.stringify([JSON.stringify({ write_id: crypto.randomUUID(), name, value: index })]),
      });
      if (!response.ok) throw new Error(`set_metric HTTP ${response.status}`);
      latencies.push(await update);
    }
    const sorted = [...latencies].sort((a, b) => a - b);
    const report = { database, samples: sorted.length, median_ms: +sorted[Math.floor(sorted.length / 2)].toFixed(1),
      max_ms: +sorted.at(-1)!.toFixed(1), under_500ms: sorted.every(value => value < 500),
      visible_cases: Array.from(conn!.db.case.iter()).length };
    await writeFile(resolve(directory, 'subscriptions.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
    if (!report.under_500ms) throw new Error('Subscription updates exceeded 500 ms');
  } finally { conn?.disconnect(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
