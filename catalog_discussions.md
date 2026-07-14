# Catalog Discussions

Living notes on **what ModelArk collects and why**. Scope decisions are formalized in
`docs/decision_log.md` (see DEC-001, DEF-001, DEF-002); this file holds the supporting
research and the running candidate landscape.

## Current scope (DEC-001, DEC-010, DEC-011)

- **Domain:** language models remain the core, and the active scope now also includes audio/speech,
  world models, and image generation. Generative LLMs (`text-generation`),
  encoder/BERT-family (`fill-mask`), embeddings/retrieval (`sentence-similarity`,
  `feature-extraction`), classification/NER (`text-classification`, `token-classification`).
- **All geographies.** Lead with the Chinese frontier labs (now top of the open-weight
  leaderboards) while also fully covering Western labs; expand lab *diversity* (Arcee
  line), not just more from the same big labs.
- **Still deferred/excluded:** video and general vision-language coverage beyond the explicitly
  included categories. The older landscape notes below are historical research, not current policy.
- **Mechanism:** org allowlist × architecture-derived category filter. Hugging Face pipeline tags
  are retained as hints but do not define archive eligibility.
- **Archive policy:** full-precision/largest weights are the primary target; quantize
  on demand later.

## Approved org allowlist

**Chinese frontier:** `deepseek-ai`, `Qwen`, `moonshotai`, `zai-org`, `MiniMaxAI`,
`tencent`, `stepfun-ai`, `OpenGVLab`, `XiaomiMiMo`, `baidu`, `01-ai`, `OpenBMB`

**Western & other:** `meta-llama`, `mistralai`, `google`, `microsoft`, `nvidia`,
`allenai`, `ibm-granite`, `tiiuae`

**Diverse labs (Arcee line):** `arcee-ai`, `CohereLabs`, `NousResearch`, `ai21labs`,
`LiquidAI`, `LGAI-EXAONE`, `SakanaAI`, `bigcode`, `HuggingFaceTB`

**Encoder / embeddings:** `answerdotai` (ModernBERT), `google-bert`, `FacebookAI`
(RoBERTa/XLM-R), `BAAI` (BGE), `sentence-transformers`, `intfloat` (E5), `nomic-ai`,
`jinaai`, `mixedbread-ai`, `Snowflake` (arctic-embed), `Alibaba-NLP` (GTE)

> Exact repo IDs, params, sizes, and licenses are confirmed by `modelark discover`
> against each org (HF is the authority). Org handles below are best-known as of the
> 2026-06-27 research sweep and verified at discovery time.

---

## Catalog state — complete walk (2026-06-28, 400/org cap)

> Historical snapshot (the initial walk). The catalog has since grown to ~4.1k models — run
> `modelark discover` for live numbers.

41/41 orgs · **3,470 models cataloged** · **222 TB** of weights mapped (nothing downloaded) ·
1,745 excluded (logged in `catalog/exclusions/`).
Categories: generative-llm 2619 · embedding 360 · seq2seq 170 · encoder 155 · classifier 77 ·
reranker 54 · qa 23 · translation 12.
LLM variants: instruct 1702 · (none) 704 · base 161 · reasoning 52.
Biggest: `NousResearch/k2-merged-3.5T` 3468B · `deepseek-ai/DeepSeek-V4-Pro-Base` 1601B ·
`moonshotai/Kimi-K2-*` 1026–1058B.
Top exclusion reasons: domain:multimodal 728 · category:unknown 345 · domain:vision 238 ·
domain:audio 116 · domain:adapter 86 · domain:image-gen 84.

> History: the first *uncapped* walk hit HF's 1000-req/5-min limit at 16/41 orgs (INC-001);
> fixed with backoff + resume and re-run capped at 400/org.

### `category:unknown` bucket — resolved
The original unknown bucket was four things; all now handled in the classifier:
- **GGUF repos** → classified via the GGUF header's `general.architecture` (DEC-002 option 2).
- **Headless encoders / ColBERT** → base-encoder + `model_type` + ColBERT rules reclaim them.
- **Adapters / PEFT** → labeled `domain:adapter`, excluded (deltas, not standalone models).
- **Non-language** (video/timeseries/3D/audio) → mapped to their domains.
Residual `unknown` (~345) is now genuine research artifacts — training checkpoints, ablations,
benchmarks, custom-config repos — correctly logged-and-excluded rather than guessed.

---

## Landscape research (mid-2026 sweep)

> ⚠️ Names/versions/params are from mid-2026 web sources (some marketing-inflated;
> sources disagree on specs). Treat as a candidate map, not ground truth — discovery
> against HF is the source of truth.

### Text LLMs — Chinese frontier

| Lab | HF org | Flagship(s) reported | Scale | License |
|---|---|---|---|---|
| DeepSeek | `deepseek-ai` | V4-Pro, V4/V4-Flash, V3.2, R1, V3 | up to ~1.6T MoE / 49B active | MIT |
| Alibaba Qwen | `Qwen` | Qwen3.5 (0.8B–397B), Qwen3.6-27B/35B-A3B, Qwen3-Coder-480B-A35B, Qwen3-235B-A22B | dense + MoE | Apache 2.0 (Max tier closed) |
| Moonshot (Kimi) | `moonshotai` | Kimi K2.6, K2.7-Code | ~1T / 32B active | Modified MIT |
| Zhipu / Z.ai (GLM) | `zai-org` | GLM-5, GLM-5.1 | 744B / 40B active | MIT |
| MiniMax | `MiniMaxAI` | M2 / M2.5 / M3 | MoE | open weights (flagship closed) |
| Tencent Hunyuan | `tencent` | Hunyuan 3.0 | 295–406B MoE | custom/strict |
| StepFun | `stepfun-ai` | Step3, Step 3.5 Flash | MoE | open (varies) |
| Xiaomi | `XiaomiMiMo` | MiMo-V2-Pro | small→mid | mixed (flagship closed) |
| Baidu ERNIE | `baidu` | ERNIE 4.5 (open); 5.0 closed | open weights (4.5) | mixed |
| 01.AI / InternLM / OpenBMB | `01-ai` / `OpenGVLab` / `OpenBMB` | Yi, InternLM, MiniCPM | varied | mostly open |

### Text LLMs — Western & other

| Lab | HF org | Flagship(s) | Scale | License |
|---|---|---|---|---|
| Meta Llama 4 | `meta-llama` | Scout (109B/16E), Maverick (402B/128E), Behemoth (2T, training) | 17B active MoE | Llama Community (gated) |
| Mistral | `mistralai` | Large 3, Small 3.1, Nemo, Devstral 2, Magistral, Codestral | 7B–123B | Apache (most) |
| Google Gemma | `google` | Gemma 3 (1/4/12/27B), Gemma 4 (E2B/E4B/26B-MoE/31B) | dense+MoE | Gemma Terms |
| Microsoft Phi | `microsoft` | Phi-4 (14B), Phi-4-Reasoning, Phi-4-mini | ≤15B | MIT |
| NVIDIA Nemotron 3 | `nvidia` | Nano (4B, 30B-A3B), Super (120B-A12B) | Mamba-Transformer MoE, 1M ctx | NVIDIA Open |
| AI2 OLMo | `allenai` | OLMo 3 (7B/32B), OLMo Hybrid, Molmo | dense | Apache 2.0, fully open (data+code) |
| IBM Granite | `ibm-granite` | Granite 4.0/4.1 | small→mid | Apache 2.0 |
| Falcon / SmolLM / StarCoder | `tiiuae` / `HuggingFaceTB` / `bigcode` | Falcon, SmolLM2, StarCoder2 | — | mostly open |

### Diverse labs (the Arcee line)

| Lab | HF org | Known for | License posture |
|---|---|---|---|
| Arcee AI | `arcee-ai` | Trinity, model merging | open |
| Cohere | `CohereLabs` | Command, Aya (multilingual) | open-weights (research) |
| Nous Research | `NousResearch` | Hermes | open |
| AI21 | `ai21labs` | Jamba (hybrid SSM) | Jamba Open |
| Liquid AI | `LiquidAI` | LFM2.x | open |
| LG AI | `LGAI-EXAONE` | EXAONE 4.5 | open (terms) |
| Sakana AI | `SakanaAI` | evolutionary/merged models | open |

### Encoder / embedding family (the "lang types")

| Org | Models |
|---|---|
| `answerdotai` | ModernBERT |
| `google-bert` / `FacebookAI` | BERT, RoBERTa, XLM-R |
| `BAAI` | BGE embeddings, Aquila |
| `sentence-transformers` | SBERT/MiniLM/mpnet |
| `intfloat` | E5 |
| `nomic-ai` | nomic-embed |
| `jinaai` | jina-embeddings |
| `mixedbread-ai` | mxbai-embed |
| `Snowflake` | arctic-embed |
| `Alibaba-NLP` | GTE |

### Not catalogable (API-only / closed weights)
ERNIE 5.0, MiniMax & Xiaomi flagship tiers, Qwen-Max tier, Hunyuan flagship text —
weights not released. Reference-only (no bytes).

---

## Deferred modalities (DEF-002) — preserved for later

| Category | Hot open models | HF orgs |
|---|---|---|
| Vision-language | InternVL3 / InternVL-U, Qwen3-VL, Kimi-VL, Molmo, Pixtral, Gemma-V, Phi-4-Vision, Ovis2, SmolVLM2, GLM-5V | `OpenGVLab`,`Qwen`,`moonshotai`,`allenai`,`mistralai` |
| Image gen | FLUX.2 / FLUX.1, SD 3.5, Qwen-Image, Z-Image-Turbo, HunyuanImage-3.0, ERNIE-Image | `black-forest-labs`,`stabilityai`,`Qwen`,`tencent`,`baidu` |
| Video gen | Wan 2.1/2.2, HunyuanVideo, LTX-Video, CogVideoX, Mochi 1, SkyReels | `Wan-AI`,`tencent`,`Lightricks`,`THUDM`,`genmo` |
| Audio / speech | Whisper-v3, Voxtral TTS, Nemotron Speech, StepAudio 2.5, Bark, Kokoro, MusicGen | `openai`,`mistralai`,`nvidia`,`stepfun-ai`,`suno` |

## Future ideas (not scoped)
- **Local management web UI** for selecting downloads + library management (DEF-001).
  Keep architectural space (status lifecycle, JSONL export, query layer); don't build yet.
