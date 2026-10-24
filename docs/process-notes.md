# Process notes (Notability track)

Real notes from sketch to build. Append as you go; timestamps in PDT.

## Sat Oct 24

- **~00:00** PRD v1.2 landed (`docs/PRD.md`). Decided: fictional home brokerage, real counterparties, unmasked contacts, demo-only outreach.
- **~00:30** Repo scaffolded against the section 14 layout. Verified the two APIs that bit us in older examples:
  - `a2a-sdk` 1.2.3 is the protobuf-based 1.x API (`a2a.types` are `a2a_pb2` messages; servers mount
    `create_agent_card_routes` + `create_jsonrpc_routes` on Starlette/FastAPI; `AgentCard.supported_interfaces`
    replaces `url`). No `a2a.server.apps` module anymore.
  - SpacetimeDB 2.10 TypeScript modules use `schema({...})` + `table({name}, {cols})` + `spacetimedb.reducer(...)`
    exports. Local server is `127.0.0.1:3000`.
- **Open:** confirm the HTTP reducer-call path (`POST /v1/database/freightops/call/<reducer>`) against the 2.x docs by C0 (W2).

## Decisions log

| When | Decision | Why |
|---|---|---|
| Sat 00:30 | One root `pyproject.toml`, packages = top-level dirs | Hackathon speed; `from common.policy import check` everywhere |
| Sat 00:30 | Single Python Docker image, service picked by `command:` | One build, eight agents |
| Sat 00:30 | Allowlist enforcement lives in `integrations/outreach.py` and is imported by the Voice Desk and email tool | One choke point for P-011 / T16 |
