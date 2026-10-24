# What is real, what is simulated, what is demo-routed

Required for the Hyphenate judges and the README (PRD R8). Keep this current.

| Thing | Status | Source / mechanism |
|---|---|---|
| Home brokerage "Golden Gate Freight" | **Fictional** | Used in UI, voice persona, documents |
| Carriers (~300) and shippers (~40) | **Real companies** | Apify FMCSA SAFER actor; Apify website / Maps scrapes. Stored with `source_url`, `scraped_at` |
| Contacts (name, title, phone, email) | **Real, shown unmasked** | Enrichment waterfall: FMCSA → our contact-data key → context.dev → website scrape. Each row has `source`, `source_url`, `confidence`, `fetched_at` |
| Loads, rate cons, invoices, BOLs, PODs, GPS pings, emails | **Synthetic** | Generated against real counterparties; PDFs carry a "SIMULATED" footer; planted errors in `sim/answer_key.json` |
| Bank | **Sandbox** | Capital One Nessie |
| Business credit, OFAC, IDV | **Sandbox** | CRS |
| TMS / QuickBooks | **Simulated** behind the same tool interfaces | Postgres tables in `sim/schema.sql` |
| LLM inference | **Real, open-source models on the ASUS box** | `harness/` routes each call; `MOCK_HARNESS=1` for dev |
| Voice calls | **Real Cartesia calls, demo-routed** | Voice Desk resolves the real number as `intended_to`, dials only a `DEMO_ALLOWLIST` teammate as `routed_to` (PRD R5, T16) |
| Email | **Demo-routed** | Sent only to the team inbox; replies resume the A2A task |
| Money movement | **None** | Payments are proposals + sandbox transfers only |
