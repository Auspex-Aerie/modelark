# Deferred Artifact Support

This is the public product backlog for repositories removed from the operator's migrated acceptance
cart on 2026-07-16 because ModelArk could not yet build a safe, restorable archive manifest for
them. It is not a permanent blacklist: the operator wants some of these repositories, and
`DEF-030` records the commitment to revisit them with full support rather than silently discard
them.

The snapshot contained 54 repositories: 50 pickle-only under the safe public policy and four with
only unsupported artifacts. They were removed from that fill so whole-plan admission could proceed;
their repository IDs and observed catalog formats remain here for future implementation and
curation.

## Completion contract

A repository leaves this backlog only after refreshed discovery finds a supported safe release, or
its artifact family receives the complete ModelArk contract:

1. Canonical manifest selection and relative-path preservation.
2. An explicit storage action and exact capacity/workspace budgeting.
3. Format-specific verification evidence that never executes untrusted model code.
4. Atomic restore with final canonical hash verification.
5. Mixed-format, missing-copy, nested-path, and installed-wheel tests.

Pickle additionally requires the deeper hygiene tracked by `DEF-011`: local opcode scanning, Hugging
Face scan evidence, quarantine/refusal for flagged blobs, and an optional trusted conversion path.
Until then, `DEC-040` keeps `exclude.pickle_only=true` as the public default; explicit private inert
storage remains an opt-in, not full support. Unsupported families remain governed by `DEF-027`.

## Reason codes

Every reason shown for a row applies; codes are cumulative, not alternatives.

| Code | Meaning |
|---|---|
| P1 | No safetensors or GGUF weight manifest was present in the catalog snapshot. |
| P2 | PyTorch pickle weights were present. |
| P3 | The safe public `exclude.pickle_only=true` policy therefore refused acquisition. |
| P4 | Scanner, HF-scan reconciliation, quarantine, and trusted conversion are not fully implemented. |
| U1 | No safetensors, GGUF, or opted-in pickle weight manifest was present. |
| U2 | Only auxiliary or unsupported artifact formats were observed. |
| U3 | ModelArk has no canonical manifest/storage rule for the observed weight family. |
| U4 | Verification, restore, capacity, and mixed-cart coverage for that family are incomplete. |

## Pickle-only snapshot

| Repository | Catalog formats observed | Reasons |
|---|---|---|
| `BAAI/JudgeLM-13B-v1.0` | aux, pytorch | P1, P2, P3, P4 |
| `BAAI/JudgeLM-7B-v1.0` | aux, pytorch | P1, P2, P3, P4 |
| `BAAI/bge-large-zh-v1.5` | aux, pytorch | P1, P2, P3, P4 |
| `BAAI/bge-m3` | aux, onnx, other, pytorch | P1, P2, P3, P4 |
| `Hannibal046/xrag-7b` | aux, pytorch | P1, P2, P3, P4 |
| `Hannibal046/xrag-moe` | aux, pytorch | P1, P2, P3, P4 |
| `NousResearch/CodeLlama-34b-hf` | aux, pytorch | P1, P2, P3, P4 |
| `allenai/digital-socrates-13b` | aux, pytorch | P1, P2, P3, P4 |
| `allenai/scibert_scivocab_uncased` | aux, other, pytorch | P1, P2, P3, P4 |
| `allenai/specter2_base` | aux, pytorch | P1, P2, P3, P4 |
| `allenai/unifiedqa-t5-small` | aux, other, pytorch | P1, P2, P3, P4 |
| `coqui/XTTS-v2` | aux, other, pytorch | P1, P2, P3, P4 |
| `deepseek-ai/deepseek-llm-67b-base` | aux, pytorch | P1, P2, P3, P4 |
| `deepseek-ai/deepseek-llm-7b-base` | aux, pytorch | P1, P2, P3, P4 |
| `facebook/contriever-msmarco` | aux, pytorch | P1, P2, P3, P4 |
| `facebook/dpr-question_encoder-single-nq-base` | aux, other, pytorch | P1, P2, P3, P4 |
| `facebook/hf-seamless-m4t-medium` | aux, pytorch | P1, P2, P3, P4 |
| `facebook/hubert-large-ls960-ft` | aux, other, pytorch | P1, P2, P3, P4 |
| `facebook/musicgen-medium` | aux, pytorch | P1, P2, P3, P4 |
| `facebook/wav2vec2-large-xlsr-53` | aux, other, pytorch | P1, P2, P3, P4 |
| `facebook/wav2vec2-lv-60-espeak-cv-ft` | aux, pytorch | P1, P2, P3, P4 |
| `facebook/wav2vec2-xls-r-300m` | aux, pytorch | P1, P2, P3, P4 |
| `google/electra-base-discriminator` | aux, other, pytorch | P1, P2, P3, P4 |
| `google/electra-small-discriminator` | aux, other, pytorch | P1, P2, P3, P4 |
| `google/mt5-small` | aux, onnx, other, pytorch | P1, P2, P3, P4 |
| `google/muril-large-cased` | aux, pytorch | P1, P2, P3, P4 |
| `google/tapas-large-finetuned-sqa` | aux, other, pytorch | P1, P2, P3, P4 |
| `hexgrad/Kokoro-82M` | aux, other, pytorch | P1, P2, P3, P4 |
| `jayelm/llama-7b-gist-1` | aux, pytorch | P1, P2, P3, P4 |
| `microsoft/Orca-2-13b` | aux, other, pytorch | P1, P2, P3, P4 |
| `microsoft/codebert-base` | aux, other, pytorch | P1, P2, P3, P4 |
| `microsoft/deberta-large-mnli` | aux, pytorch | P1, P2, P3, P4 |
| `microsoft/deberta-xlarge-mnli` | aux, other, pytorch | P1, P2, P3, P4 |
| `microsoft/tapex-base-finetuned-wikisql` | aux, pytorch | P1, P2, P3, P4 |
| `microsoft/unixcoder-base` | aux, pytorch | P1, P2, P3, P4 |
| `nvidia/Minitron-4B-Base` | aux, other, pytorch | P1, P2, P3, P4 |
| `nvidia/Minitron-8B-Base` | aux, other, pytorch | P1, P2, P3, P4 |
| `nvidia/Nemotron-Mini-4B-Instruct` | aux, other, pytorch | P1, P2, P3, P4 |
| `nvidia/bigvgan_v2_22khz_80band_256x` | aux, other, pytorch | P1, P2, P3, P4 |
| `nvidia/bigvgan_v2_44khz_128band_512x` | aux, other, pytorch | P1, P2, P3, P4 |
| `princeton-nlp/AutoCompressor-2.7b-6k` | aux, pytorch | P1, P2, P3, P4 |
| `princeton-nlp/AutoCompressor-Llama-2-7b-6k` | aux, pytorch | P1, P2, P3, P4 |
| `suno/bark` | aux, mlx, pytorch | P1, P2, P3, P4 |
| `suno/bark-small` | aux, mlx, pytorch | P1, P2, P3, P4 |
| `tiiuae/falcon-40b-instruct` | aux, pytorch | P1, P2, P3, P4 |
| `zai-org/LongAlign-6B-64k` | aux, pytorch | P1, P2, P3, P4 |
| `zai-org/LongAlign-7B-64k` | aux, pytorch | P1, P2, P3, P4 |
| `zai-org/LongAlign-7B-64k-base` | aux, pytorch | P1, P2, P3, P4 |
| `zai-org/chatglm2-6b` | aux, other, pytorch | P1, P2, P3, P4 |
| `zai-org/chatglm3-6b-base` | aux, other, pytorch | P1, P2, P3, P4 |

## Unsupported-only snapshot

| Repository | Catalog formats observed | Reasons |
|---|---|---|
| `HuggingFaceTB/SmolLM-360M-Instruct-ONNX-fp16` | aux, onnx | U1, U2, U3, U4 |
| `NousResearch/ByteDance-Seed-OSS-36B-Alternate-Tokenizer` | aux | U1, U2, U3, U4 |
| `arcee-ai/Trinity-Tokenizer` | aux, other | U1, U2, U3, U4 |
| `nvidia/parakeet-tdt-0.6b-v2` | aux, other | U1, U2, U3, U4 |

## Re-entry workflow

When the operator chooses a repository from this backlog:

1. Refresh its Hub metadata and file inventory; a safe upstream revision may already resolve it.
2. Prefer mapping to an official safetensors/GGUF release when identity and provenance are clear.
3. Otherwise implement the missing family contract above—never add a one-off filename exception.
4. Exercise the repository in planning, acquisition, physical verification, and restore tests.
5. Record the implementing decision and update this snapshot without erasing its historical reason.
