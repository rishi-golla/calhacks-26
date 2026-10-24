# SpacetimeDB module (W2)

SpacetimeDB **2.x** (the API changed a lot in 2.0, so trust the current docs: https://spacetimedb.com/docs).
Recommended: TypeScript module so the dashboard shares types.

```bash
curl -sSf https://install.spacetimedb.com | sh   # installs the `spacetime` CLI
spacetime start                                   # local server
spacetime init --lang typescript module           # check `spacetime init --help` for 2.x flags
spacetime publish freightops                      # from spacetime/module
```

Tables and reducers are defined in the PRD (7.4, 7.7–7.9): case, task, agent, agent_config, ledger_entry,
hold, payment_proposal, approval, audit_event, call, precedent, llm_call, metric, delegation_edge,
change_set, cell_change, doc_read, review, agent_memory, instruction_version, flag, company, contact.

Invariants enforced in reducers: balanced journal entries, open period, requester != approver,
>$5k hold release needs a human approval, change_set + cell_change written atomically with data changes.

Python agents call reducers over HTTP via `common/spacetime_client.py`; confirm the 2.x HTTP path.
