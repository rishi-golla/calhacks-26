# FreightLine SpacetimeDB architecture

Generated with the built-in imagegen tool from the checked-out repository on October 25, 2026. The diagram describes implemented interfaces and the documented Maincloud deployment; running service health was not checked.

The exact generation prompt is in `spacetimedb-imagegen-prompt.txt`.

## Source references

- `docs/TEAM-CLOUD-HANDOFF.md`: ASUS/Mac deployment, cloud outbox bridge, PostgreSQL runtime and recall sync.
- `spacetime/module/CONTRACT.md`: reducer contracts, authorization, financial controls, atomic history, approvals, outbox, settlement and imported history.
- `spacetime/module/spacetimedb/src/schema.ts`: public and private table inventory.
- `common/spacetime_client.py`: Python policy preflight, HTTP writes, SQL reads and error semantics.
- `common/review_worker.py`: polling, claim leases, A2A delivery, retries and acknowledgement.
- `common/runtime.py` and `common/runtime_store.py`: execution persistence and database mirroring.
- `common/config.py`: nine agents and delegation bounds.
- `common/source_repository.py`: authoritative source-record reads.
- `common/agent_recall.py`: Elasticsearch mirror and authoritative database rereads.
- `harness/ingest/dispatch.py`: first-hop enqueue and durable retry payloads.
- `ingest/main.py` and `ingest/mirror.py`: source ingestion and database mirroring.
- `web/lib/live-queries.ts`: subscription profiles, history windows and per-case queries.

## Important interpretation

SpacetimeDB owns operational state and transaction enforcement. Agent execution and external side effects run in services outside the database. PostgreSQL holds execution checkpoints, Redis carries intake streams, and Elasticsearch supplies derived recall ranking. Delivery acknowledgement does not establish execution completion. The public operational tables reflect this hackathon module's access model; private approval secrets, write receipts and chain heads are separate.
