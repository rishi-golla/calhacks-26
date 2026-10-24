# W1 intake data contract (for the data/sim side)

What the ASUS intake harness (`harness/`, branch `w1-harness`) expects from the data side.
PRD sections 7.2 and 7.3 remain the source of truth; this file describes how W1 reads those events.

## What W1 does with each event
ASUS is the intake layer (PRD v1.3). It reads business events from Redis, cleans them, works out
which agent owns them, and sends the first A2A task. It does not do the agents' work.

1. **Regex redaction**: bank account/routing numbers, cards, SSN, EIN, emails and phones become
   tokens like `[[ACCOUNT_1a2b3c4d]]`. Real values stay in a local SQLite vault on ASUS.
2. **Deterministic routing** by event type (7.3). Types that aren't routed stop here, with no model calls.
3. **Model A** (Nemotron Nano) extracts fields with a JSON schema per document type, and lists person
   names and street addresses, which then get tokenized too.
4. **Model B** (Qwen3-4B) classifies only items the table can't place (emails, unknown types).
5. **Model C** (gpt-oss-20b) checks for leftover personal data and mismatched amounts and sets
   `subject.needs_review` when it finds a problem.
6. Builds a `TaskEnvelope` (7.2, schema unchanged) and sends it over A2A to the owning agent via W3's
   `common.a2a_transport` with the router's credential (`A2A_TOKEN_ROUTER`, required in live mode),
   `idempotency_key = event:<event_id>` and `correlation_id = case_id`.

Typical time is about 6 s per event.

## Redis
- URL: `redis://<asus-host>:6379/0`
- W1 reads **only** stream `events`, consumer group `router`. Each entry has one field, `json`,
  holding the event as a JSON string: `XADD events * json '<event json>'`.
- `events` carries the routable 7.3 events plus TMS events (`load.tendered`, `load.dispatched`,
  `gps.ping`, `load.delivered`, `pod.received`).
- W1 does **not** read `raw` (raw arrivals) or `documents` (ingestion records). Those belong to the
  data side.
- W1 writes stream `triage` (items a person must classify or attach to a case: low-confidence
  classifications, no upstream `case_id`) and `events:dead_letter` (malformed entries, written atomically
  with the ack; fields `entry_id`, `reason`, `json`).
- W1 ACKs an entry only after a final outcome (accepted, duplicate, not routed, triage). Entries
  whose dispatch failed stay pending and are retried after 30 s.

## Event format
```json
{"event_id":"EVT-…","type":"carrier_invoice.received","ts":"ISO8601","sim_ts":"…",
 "case_hint":"LOAD-4821","priority":"P0|P1|P2|P3","payload":{},"source":"sim|demo|judge|voice"}
```
- `event_id`: required and unique. It is the dedup key forever: after an event is accepted,
  triaged or marked not-routed, republishing the same `event_id` does nothing. Use fresh IDs for
  each demo run.
- `type`: required.
- `priority`: defaults to P2. P0 is processed first within a batch.
- `case_id`: **required for anything routed**, as `event.case_id` or `payload.case_id` (1–36 chars), for
  a case ingest has already opened in SpacetimeDB. `case_hint` is not authoritative and is never turned into a
  case. A routable event without a case is still cleaned, then goes to `triage` for a person to attach it.
- `payload` must be a JSON object.

## Routing
| type | goes to (skill → agent) |
|---|---|
| `carrier_invoice.received`, `voice.inbound.dispute` | `process_carrier_invoice` → carrier_ap |
| `shipper_payment.received`, `bank.event` | `reconcile_customer_payment` → shipper_ar |
| `bank_change.requested` | `screen_bank_change` → risk |
| `gps.ping`, `load.tendered`, `load.dispatched`, `load.delivered` | not routed (recorded and acked; no model calls) |
| `pod.received` | `receive_document` → documents (wakes a parked document chase; otherwise a no-op) |
| `scenario.injected` | routed by `payload.generated_type`, which follows the same table. `generated_type: "load.tendered"` is therefore not routed. |
| `email.received`, unknown types, `scenario.injected` with an unknown `generated_type` | Model B picks from `process_carrier_invoice`, `reconcile_customer_payment`, `screen_bank_change`, `answer_payment_status`, or triage. Confidence < 0.6 goes to triage. |

## How the payload is read
- **Document text**: the first string found under `text`, `body`, `raw_text`, `document_text`,
  `content`, `transcript`. If present, `from`, `to`, `subject` and `date` are added as header lines.
  If none of these keys exists, the whole payload JSON is treated as the document. Put the email
  body and the PDF text in `text`. There is no PDF or OCR step on ASUS. Text is capped at 12k
  characters for extraction and 6k for classification.
- **Envelope `subject`** = the redacted payload (wins on conflicts) + the extracted fields that aren't
  null + reserved keys `event_type`, `doc_type`, `needs_review`, `review_notes`, `redacted_tokens`.
  Don't use the reserved names in payloads.
- `payload.evidence_refs` (list of strings) is passed through, e.g. `doc://LOAD-4821/invoice.pdf`.
  W1 adds `event:<event_id>`.

## Field names Model A extracts (use the same names in structured payloads)
| doc type (from event type) | fields |
|---|---|
| carrier_invoice | invoice_id, load_id, carrier_id, carrier_name, invoice_date, invoice_amount, contract_rate, line_items[{desc, amount}], detention_hours_claimed, receipt_attached |
| pod | load_id, carrier_id, delivered_at, receiver, signed_by, exceptions |
| shipper_payment | payment_id, shipper_id, shipper_name, amount, remittance_ref, invoices_applied[{invoice_id, amount}] |
| bank_event | amount, direction (credit/debit), counterparty, reference, posted_at |
| bank_change | carrier_id, carrier_name, requested_by, channel, effective_date, new_account, new_routing, next_payment_amount, urgency_language |
| voice_dispute | load_id, carrier_id, invoice_id, claim, amount_disputed |
| email | sender, subject, summary, load_id, invoice_id, carrier_id, shipper_id, amounts[{desc, amount}], intent |

These amount cross-checks set `needs_review` (useful for seeding "bad" cases on purpose):
- `invoice_amount`, `contract_rate`, `amount`, `amount_disputed` or `next_payment_amount` differs
  between the structured payload and what Model A reads from `text`
- `line_items` don't sum to `invoice_amount`
- `invoices_applied` don't sum to `amount`

## Personal data rules
- **Replaced by pattern in free text**:
  - emails
  - US phones (use 555-01xx)
  - SSN `###-##-####` and EIN `##-#######`
  - cards, only in 4-4-4-x or Amex 4-6-5 groupings that pass the Luhn check
  - routing numbers, only next to a keyword (routing/ABA/RTN/transit) and passing the ABA checksum.
    Real test numbers work: 121000358, 021000021, 011000015. An invalid routing number stays in
    clear text, and Model C will probably flag it.
  - account numbers, only after a keyword (account / acct / a/c)
- **Replaced by key name in structured payloads**, whatever the value. The whole value is tokenized
  if the key contains:
  - `routing` or `aba`
  - `iban`, `account` (so `new_account`, `account`, `bank_account`, `account_number`) or `acct`
  - `ssn`
  - `ein`, `tax_id` or `tin`
  - `card_no` and similar card-number keys
  - `email`
  - `phone`, `mobile`, `fax` or `tel`

  So a carrier's directory phone placed in a payload gets tokenized. Agents must use the verified
  directory (P-008) instead. Also, any key containing `account` gets tokenized whatever its value,
  so don't name non-sensitive fields `*account*` (e.g. a GL account code).
- **Person names and street addresses** are found by Model A and tokenized, including standalone
  first or last names elsewhere ("Thanks, Marcus"). Company names are kept when they look like a
  business (LLC, Inc, Co, Transport, Trucking, Logistics, Freight, Haul, Produce, Carriers, Express,
  Lines, Bank).
- **Not redacted**: load/case/carrier/shipper IDs, USDOT/MC numbers, cities, lanes, amounts, timestamps.
- Tokens are deterministic: the same value always gives the same token, so "same bank account as
  last time" is still detectable downstream.

## Testing without Redis (on the ASUS host only)
`POST http://127.0.0.1:8000/v1/ingest?dispatch=false` with one event as the body. It returns the
route, envelope, `needs_review`, extractor/verifier output and timings, without sending anything.
Leave off `?dispatch=false` to really dispatch.

## Open items
1. `sim/fixtures/hero_case.json` still has TBD carrier and shipper names and `usdot: null`. PRD
   section 10 says Sierra Haul LLC, USDOT 3141592, Bay Produce Co. There are no fixtures yet for
   the LOAD-4877 replay or the C-77 $9,400 bank change.
2. Carrier AP's `three_way_match` raises `KeyError` without `invoice_amount` and `contract_rate`.
   Fixed on main: Carrier AP now finishes invoice-less cases as `awaiting_invoice`, and PODs route to the
   documents agent instead.
3. Redis has no password and is reachable on the LAN. Proposal: set `requirepass` and put it in
   `REDIS_URL`.
