# Fully open models for W1 intake

**Requirement:** fully open models only, meaning open training data, open training code and open weights. Open-weight
models (Nemotron, Qwen, gpt-oss, DeepSeek) don't qualify. Candidates must also use a standard vLLM architecture with
safetensors weights (no `trust_remote_code`, no pickle). Every file is checked against the SHA-256 that Hugging Face
publishes (`infra/download_open_models.py`).

**Swap rule:** a model replaces the current one in its slot only if it matches it on these tests:
- `tests/slot_eval.py`: 60 labelled documents; the owner is known from the 7.3 table
- `tests/batch_bench.py`: 300 documents scored against the simulator's ground truth; it must keep 100% redaction recall

## Results

### Slot B: classify unlabeled items (baseline Qwen3-4B, open-weight: 60/60, all prompt variants)

| Model (org) | Result | Verdict |
|---|---|---|
| OLMoE-1B-7B-0125-Instruct (AI2) | plain 20/60, reason-first 17/60, few-shot 5/60, few-shot without JSON constraint 0/60. Understands the item ("update bank details") but can't pick from a fixed label set; ~1B active parameters | **rejected** |
| SmolLM3-3B (Hugging Face) | 40/60: invoices and payments 40/40, bank-change requests **0/20**, all labelled as payments, even for "We have changed banks… send all payments to the new account". Same with reason-first | **rejected**: misses the bank-change fraud case (P-004) |
| Apertus-8B-Instruct-2509 (Swiss AI) | pending download | |
| Olmo-3-7B-Instruct (AI2) | pending download | |

### Slot A: extraction + names/addresses (baseline Nemotron Nano, open-weight)

Fast lane, 300 docs: 35.5 docs/min, 100% redaction recall, invoice totals 132/132.

| Model | Result | Verdict |
|---|---|---|
| Olmo-3-7B-Instruct (AI2) | pending | |
| OLMo-2-1124-13B-Instruct (AI2, FP8 on the fly) | pending | |

### Slot C: independent cross-check (currently Qwen3-4B; gpt-oss parked for NaN output on GB10)

| Model | Result | Verdict |
|---|---|---|
| Apertus-8B-Instruct-2509 (Swiss AI) | pending | |

### Considered and not tested

- CrystalChat (LLM360/IFM): custom architecture needing `trust_remote_code`, pickle `.bin` weights, and no vLLM support.
- Amber, BLOOM, T5: outclassed by OLMo, too slow, or (T5) needing fine-tuning first.
