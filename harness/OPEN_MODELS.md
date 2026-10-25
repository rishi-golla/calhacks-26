# Fully open models for W1 intake

**Requirement:** fully open models only, meaning open training data, open training code and open weights. Open-weight
models (Nemotron, Qwen, gpt-oss, DeepSeek) don't qualify. Candidates must also use a standard vLLM architecture with
safetensors weights (no `trust_remote_code`, no pickle). Every file is checked against the SHA-256 that Hugging Face
publishes (`infra/download_open_models.py`).

**Not the live lineup.** On 2026-10-24 the team chose speed + accuracy: live intake runs Nemotron Nano (personal data)
and Qwen3-4B FP8 (classify, audit), 67 docs/min on 120 docs with recall 100% and 84/84 invoices (see config.toml).
The fully open lineup below is kept as a tested alternative.

**Fully open lineup (tested):** Olmo-3-7B finds names/addresses; Apertus-8B classifies and audits
for leftover personal data. Money is read and compared by exact code (`ingest/amounts.py`, `amount_checks`), because
models misread and misjudge numbers. Fully open run on 300 docs: recall 100%, invoice totals and line items 132/132,
40.6 docs/min.

**Swap rule:** a model replaces the current one in its slot only if it matches it on these tests:
- `tests/slot_eval.py`: 60 labelled documents; the owner is known from the 7.3 table
- `tests/batch_bench.py`: 300 documents scored against the simulator's ground truth; it must keep 100% redaction recall

## Results

### Slot B: classify unlabeled items (baseline Qwen3-4B, open-weight: 60/60, all prompt variants)

| Model (org) | Result | Verdict |
|---|---|---|
| OLMoE-1B-7B-0125-Instruct (AI2) | plain 20/60, reason-first 17/60, few-shot 5/60, few-shot without JSON constraint 0/60. Understands the item ("update bank details") but can't pick from a fixed label set; ~1B active parameters | **rejected** |
| SmolLM3-3B (Hugging Face) | 40/60: invoices and payments 40/40, bank-change requests **0/20**, all labelled as payments, even for "We have changed banks… send all payments to the new account". Same with reason-first | **rejected**: misses the bank-change fraud case (P-004) |
| Apertus-8B-Instruct-2509 (Swiss AI) | original prompt 53/60 (7 bank changes → payment); with reason-first, a sharper bank-change rule, or both: **60/60**, p50 2.4–2.7 s (Qwen3-4B 1.4 s) | **passes**: in the classify slot, with the reason-first prompt adopted for all models |
| Olmo-3-7B-Instruct (AI2) | pending download | |

### Slot A: extraction + names/addresses (baseline Nemotron Nano, open-weight)

Fast lane, 300 docs: 35.5 docs/min, 100% redaction recall, invoice totals 132/132.

| Model | Result | Verdict |
|---|---|---|
| Olmo-3-7B-Instruct (AI2) | asked for amounts: invoice totals **0/132** (decimal shifts such as $1,769.44 → 17694.4, or null), unchanged by prompt wording, field names or decoding mode. Finds names/addresses correctly. With amounts moved to exact code (parser 132/132 totals, line items, payments) it only finds personal data: recall 100%, 132/132 | **passes** with amounts read by code |
| OLMo-2-1124-13B-Instruct (AI2, FP8 on the fly) | pending | |

### Slot C: independent cross-check (was Qwen3-4B; gpt-oss parked for NaN output on GB10)

| Model | Result | Verdict |
|---|---|---|
| Apertus-8B-Instruct-2509 (Swiss AI) | 300 docs with dispatch: **redaction recall 100%** (0/1,215), invoice totals 132/132, 0/83 false flags, math errors 4/4; verify p50 4.2 s (Qwen3-4B 2.0 s), throughput 29.1 vs 35.5 docs/min | **passes**: in the cross-check slot |

### Considered and not tested

- CrystalChat (LLM360/IFM): custom architecture needing `trust_remote_code`, pickle `.bin` weights, and no vLLM support.
- Amber, BLOOM, T5: outclassed by OLMo, too slow, or (T5) needing fine-tuning first.
