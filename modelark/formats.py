"""Format, quant, and parameter-count classification for model files."""
from __future__ import annotations

import re

# safetensors dtype -> nominal bits per weight
DTYPE_BITS = {
    "F64": 64, "F32": 32, "F16": 16, "BF16": 16,
    "F8_E4M3": 8, "F8_E5M2": 8, "F8_E4M3FN": 8,
    "I64": 64, "I32": 32, "I16": 16, "I8": 8, "U8": 8, "BOOL": 8,
    "I4": 4, "U4": 4,
}
KNOWN_ST_DTYPES = set(DTYPE_BITS)

# GGUF quant label -> approximate bits per weight
GGUF_BITS = {
    "Q2_K": 2.6, "Q3_K_S": 3.4, "Q3_K_M": 3.9, "Q3_K_L": 4.3,
    "Q4_0": 4.5, "Q4_1": 5.0, "Q4_K_S": 4.6, "Q4_K_M": 4.8,
    "Q5_0": 5.5, "Q5_1": 6.0, "Q5_K_S": 5.5, "Q5_K_M": 5.7,
    "Q6_K": 6.6, "Q8_0": 8.5,
    "IQ1_S": 1.6, "IQ1_M": 1.8, "IQ2_XXS": 2.1, "IQ2_XS": 2.3, "IQ2_S": 2.5,
    "IQ2_M": 2.7, "IQ3_XXS": 3.1, "IQ3_XS": 3.3, "IQ3_S": 3.4, "IQ3_M": 3.7,
    "IQ4_XS": 4.3, "IQ4_NL": 4.5,
    "F16": 16, "BF16": 16, "F32": 32, "FP16": 16, "FP32": 32,
}
_GGUF_RE = re.compile(
    r"\b(IQ\d[A-Z0-9_]*|Q\d_K_[SML]|Q\d_K|Q\d_\d|BF16|FP?16|FP?32)\b"
)

PICKLE_EXTS = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")
AUX_EXTS = (
    ".json", ".txt", ".md", ".model", ".vocab", ".tiktoken", ".jinja",
    ".py", ".gitattributes", ".png", ".jpg", ".yaml", ".yml", ".tokenizer",
)


def parse_gguf_quant(filename: str) -> tuple[str | None, float | None]:
    m = _GGUF_RE.search(filename.upper())
    if not m:
        return None, None
    label = m.group(1).replace("FP", "F")
    return label, GGUF_BITS.get(label)


def classify_file(
    rfilename: str, repo_dtype: str | None = None, tags: tuple[str, ...] = ()
) -> tuple[str, str | None, float | None, str]:
    """Return (format, quant, quant_bits, safety) for one repo file."""
    p = rfilename.lower()
    stem = rfilename.rsplit("/", 1)[-1]
    tl = " ".join(tags).lower()

    if p.endswith(".safetensors"):
        if "gptq" in tl or "gptq" in p:
            return "safetensors", "gptq", 4.0, "safe"
        if "awq" in tl or "awq" in p:
            return "safetensors", "awq", 4.0, "safe"
        if repo_dtype:
            return "safetensors", repo_dtype.lower(), DTYPE_BITS.get(repo_dtype.upper()), "safe"
        return "safetensors", None, None, "safe"

    if p.endswith(".gguf"):
        q, bits = parse_gguf_quant(stem)
        return "gguf", q, bits, "safe"

    if p.endswith(PICKLE_EXTS) or "pytorch_model" in p:
        return "pytorch", (repo_dtype.lower() if repo_dtype else None), None, "pickle"

    if p.endswith(".onnx"):
        return "onnx", None, None, "safe"

    if p.endswith((".npz", ".npy")) or "/mlx" in p or stem.startswith("mlx"):
        return "mlx", None, None, "safe"

    if p.endswith(AUX_EXTS) or stem in ("tokenizer", "merges.txt", "vocab.json"):
        return "aux", None, None, "safe"

    return "other", None, None, "unknown"


def repo_dtype_from_info(info) -> str | None:
    """Dominant dtype across the repo, from safetensors parameter breakdown."""
    if info.safetensors and info.safetensors.parameters:
        return max(info.safetensors.parameters, key=info.safetensors.parameters.get)
    return None


def parse_params_b(info, repo_id: str) -> float | None:
    """Billions of parameters: prefer authoritative safetensors total, else parse name."""
    if info.safetensors and info.safetensors.total:
        return round(info.safetensors.total / 1e9, 3)
    name = repo_id.rsplit("/", 1)[-1]
    moe = re.search(r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*[bB]\b", name)
    if moe:
        return round(float(moe.group(1)) * float(moe.group(2)), 3)
    b = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", name)
    if b:
        return float(b.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[mM]\b", name)
    if m:
        return round(float(m.group(1)) / 1000, 4)
    return None


_VISION = {
    "image-classification", "object-detection", "image-segmentation",
    "image-to-text", "text-to-image", "image-to-image", "depth-estimation",
    "zero-shot-image-classification",
}
_AUDIO = {
    "automatic-speech-recognition", "text-to-speech", "audio-classification",
    "text-to-audio", "audio-to-audio",
}
_MULTI = {
    "image-text-to-text", "visual-question-answering", "video-text-to-text",
    "any-to-any", "document-question-answering",
}


def modality(pipeline_tag: str | None) -> str | None:
    if pipeline_tag in _VISION:
        return "vision"
    if pipeline_tag in _AUDIO:
        return "audio"
    if pipeline_tag in _MULTI:
        return "multimodal"
    if pipeline_tag:
        return "text"
    return None


# --- Category taxonomy (DEC-002): architecture-first, pipeline_tag/tags as hints ---

# Language pipeline tags -> our category
_PT_CATEGORY = {
    "text-generation": "generative-llm",
    "fill-mask": "encoder",
    "text2text-generation": "seq2seq",
    "summarization": "seq2seq",
    "translation": "translation",
    "sentence-similarity": "embedding",
    "feature-extraction": "embedding",
    "text-ranking": "reranker",
    "text-classification": "classifier",
    "zero-shot-classification": "classifier",
    "token-classification": "classifier",
    "question-answering": "qa",
    "table-question-answering": "qa",
}

# Non-text pipeline tags -> domain (out of language scope)
_PT_DOMAIN = {
    "image-text-to-text": "multimodal", "any-to-any": "multimodal",
    "visual-question-answering": "multimodal", "image-to-text": "multimodal",
    "visual-document-retrieval": "multimodal", "video-text-to-text": "multimodal",
    "automatic-speech-recognition": "audio", "text-to-speech": "audio",
    "text-to-audio": "audio", "audio-to-audio": "audio",
    "audio-text-to-text": "audio", "audio-classification": "audio",
    "text-to-image": "image-gen", "image-to-image": "image-gen", "image-to-3d": "image-gen",
    "text-to-video": "video-gen", "image-to-video": "video-gen",
    "image-feature-extraction": "vision", "zero-shot-image-classification": "vision",
    "image-classification": "vision", "object-detection": "vision",
    "image-segmentation": "vision", "depth-estimation": "vision", "mask-generation": "vision",
    "keypoint-detection": "vision", "video-classification": "video",
    "unconditional-image-generation": "image-gen", "text-to-3d": "image-gen",
    "time-series-forecasting": "timeseries", "tabular-classification": "tabular",
    "tabular-regression": "tabular", "reinforcement-learning": "other",
    "robotics": "other", "graph-ml": "other",
}

# GGUF general.architecture -> our category (GGUFs are overwhelmingly decoder LLMs)
_GGUF_EMBED = {"bert", "nomic-bert", "nomicbert", "jina-bert-v2", "gte", "new"}
_GGUF_SEQ2SEQ = {"t5", "t5encoder", "bart"}


def gguf_category(arch: str | None) -> str | None:
    if not arch:
        return None
    a = arch.lower()
    if a in _GGUF_EMBED or "bert" in a or "embed" in a:
        return "embedding"
    if a in _GGUF_SEQ2SEQ:
        return "seq2seq"
    return "generative-llm"


# config `model_type` -> classification (reliable even when architectures is absent)
_MT_LLM = {
    "llama", "llama4", "qwen2", "qwen3", "qwen2_moe", "qwen3_moe", "mistral", "mixtral",
    "ministral", "gemma", "gemma2", "gemma3", "gemma3_text", "phi", "phi3", "phimoe", "phi4",
    "falcon", "falcon_h1", "falcon_mamba", "gpt2", "gpt_neox", "gptj", "gpt_neo", "bloom",
    "mpt", "stablelm", "starcoder2", "gpt_bigcode", "cohere", "cohere2", "command_r", "dbrx",
    "deepseek_v2", "deepseek_v3", "olmo", "olmo2", "olmoe", "exaone", "exaone4", "internlm2",
    "internlm3", "baichuan", "mamba", "mamba2", "jamba", "recurrent_gemma", "granite",
    "granitemoe", "nemotron", "glm", "glm4", "glm4_moe", "chatglm", "minicpm", "minicpm3",
    "rwkv", "xglm", "opt", "persimmon", "solar", "yi", "arcee", "seed_oss", "smollm3", "aquila",
    "ernie4_5", "ernie4_5_moe", "hunyuan", "hunyuan_v1_dense",
}
_MT_ENCODER = {
    "bert", "roberta", "xlm-roberta", "xlm_roberta", "electra", "deberta", "deberta-v2",
    "deberta_v2", "distilbert", "albert", "mpnet", "longformer", "camembert", "ernie",
    "mobilebert", "fnet", "funnel", "bigbird", "big_bird", "canine", "convbert", "layoutlm",
    "modernbert", "nomic_bert", "megatron-bert", "flaubert", "xlm", "rembert", "esm",
}
_MT_TRANSLATION = {"marian", "m2m_100", "nllb", "fsmt"}
_MT_SEQ2SEQ = {
    "t5", "mt5", "umt5", "longt5", "bart", "mbart", "pegasus", "pegasus_x", "led",
    "blenderbot", "blenderbot-small", "prophetnet", "switch_transformers", "bigbird_pegasus", "plbart",
}
_MT_VISION = {"vit", "clip", "siglip", "deit", "beit", "convnext", "swin", "dinov2", "detr", "sam", "owlvit"}
_MT_AUDIO = {"whisper", "wav2vec2", "hubert", "speecht5", "musicgen", "bark", "encodec", "seamless_m4t", "wavlm"}


def _mt_classify(mt):
    if mt in _MT_LLM:
        return "text", "generative-llm"
    if mt in _MT_ENCODER or "bert" in mt:
        return "text", "encoder"
    if mt in _MT_TRANSLATION:
        return "text", "translation"
    if mt in _MT_SEQ2SEQ:
        return "text", "seq2seq"
    if mt in _MT_VISION:
        return "vision", None
    if mt in _MT_AUDIO:
        return "audio", audio_category(mt=mt)
    return None, None


# Audio-speech sub-categories (DEC-010): map audio signals to asr/tts/speech-lm/audio-gen.
_AUDIO_PT_CAT = {
    "automatic-speech-recognition": "asr",
    "text-to-speech": "tts",
    "text-to-audio": "audio-gen",
    "audio-to-audio": "audio-gen",
    "audio-text-to-text": "speech-lm",
    "audio-classification": "speech-lm",
}


def audio_category(pipeline_tag=None, al="", mt=""):
    """Sub-classify an audio model: asr | tts | speech-lm | audio-gen."""
    if pipeline_tag in _AUDIO_PT_CAT:
        return _AUDIO_PT_CAT[pipeline_tag]
    s = f"{al} {mt}".lower()
    if any(k in s for k in ("whisper", "wav2vec", "hubert", "wavlm", "seamless",
                            "conformer", "parakeet", "canary", "moonshine")):
        return "asr"
    if any(k in s for k in ("speecht5", "bark", "vits", "parler", "xtts", "kokoro",
                            "fastspeech", "tacotron", "styletts", "csm")):
        return "tts"
    if any(k in s for k in ("musicgen", "encodec", "audiocraft", "stableaudio",
                            "stable_audio", "audioldm", "dac", "musicgpt")):
        return "audio-gen"
    return "speech-lm"   # audio-understanding / speech LLMs + catch-all


# World models (DEC-010): NVIDIA Cosmos, DeepMind Genie, etc. They masquerade as
# video-gen/multimodal by pipeline_tag, so flag them by family name (override the tag).
_WORLD_RE = re.compile(r"(?:^|[-_/])(cosmos|genie|world-?model|world-?foundation|wham)(?:$|[-_./0-9])")

# Image generation (DEC-011): text-to-image diffusion + friends. One category (no sub-split).
_IMAGEGEN_PT = {"text-to-image", "image-to-image", "unconditional-image-generation",
                "text-to-3d", "image-to-3d"}

# Prompt/context compression (DEC-012): gisting, AutoCompressor, ICAE, xRAG, LLoCO, 500xCompressor.
# Bespoke archs (else fall to generative-llm/unknown) with no clean tags, so match by architecture
# family OR repo-name signal — HF metadata is too dirty/absent for this niche to do better.
_COMPRESSION_ARCH = ("autocompressor", "gist", "icae", "xmistral", "xmixtral")
_COMPRESSION_RE = re.compile(
    r"(?:^|[-_/])(?:auto-?compressor|icae|xrag|lloco|gisting"
    r"|gist-(?:finetune|llama|token|prompt|compress)"
    r"|prompt-?compress\w*|context-?compress\w*|500x-?compress\w*|llmlingua)(?:$|[-_./0-9])")


def classify_category(architecture, pipeline_tag, tags, library, repo_id, model_type=None):
    """Return (domain, category). domain='text' for language; category may be 'unknown'.

    Order: embedding/reranker signals → architecture suffix → config model_type →
    pipeline tag → unknown.
    """
    al = (architecture or "").lower()
    tagset = {t.lower() for t in (tags or [])}
    lib = (library or "").lower()
    rid = repo_id.lower()

    # Prompt/context compression (DEC-012): match by custom arch OR repo-name signal, before the
    # adapter/LLM/unknown fallthrough these would otherwise hit. Purpose over format (a compression
    # LoRA is 'compression', not 'adapter').
    if any(k in al for k in _COMPRESSION_ARCH) or _COMPRESSION_RE.search(rid):
        return "text", "compression"

    # Adapters / PEFT deltas — not standalone models; excluded but cleanly labeled.
    if lib == "peft" or {"peft", "lora", "adapter"} & tagset \
            or any(k in rid for k in ("-lora", "qlora", "qdora")):
        return "adapter", "adapter"

    # World models (DEC-010) — flag by family name; overrides their video-gen/multimodal tag.
    if _WORLD_RE.search(rid):
        return "world", "world-model"

    # Embeddings/rerankers/ColBERT: arch is often a head-less encoder, so trust lib/tag/pipeline first.
    if pipeline_tag == "text-ranking" or "cross-encoder" in tagset or "reranker" in tagset \
            or "rerank" in rid or "colbert" in al or "colbert" in tagset:
        return "text", "reranker"
    if lib == "sentence-transformers" or "sentence-transformers" in tagset \
            or pipeline_tag in ("sentence-similarity", "feature-extraction"):
        return "text", "embedding"

    # Architecture-first (authoritative when present).
    if al:
        if "forcausallm" in al:
            return "text", "generative-llm"
        if "formaskedlm" in al or "forpretraining" in al:
            return "text", "encoder"
        if "forquestionanswering" in al:
            return "text", "qa"
        if "fortokenclassification" in al or "forsequenceclassification" in al:
            return "text", "classifier"
        # Head-less base encoders (BertModel, LongformerModel, …) used standalone.
        if al.endswith("model") and any(f in al for f in (
                "bert", "roberta", "electra", "deberta", "longformer", "mpnet",
                "camembert", "albert", "distilbert", "xlmr", "ernie", "nomic")):
            return "text", "encoder"
        # Pure vision/multimodal model families -> out of language scope.
        if any(x in al for x in ("llava", "idefics", "mllama", "qwen2vl", "qwen3vl",
                                 "internvl", "clip", "siglip", "blip", "paligemma",
                                 "smolvlm", "pixtral", "visionencoder", "vit")):
            return "multimodal", None
        if any(x in al for x in ("whisper", "wav2vec", "hubert", "wavlm", "speecht5",
                                 "musicgen", "bark", "encodec", "seamless", "vits",
                                 "parler", "qwen2audio", "qwen2_5omni", "fastspeech", "dac")):
            return "audio", audio_category(pipeline_tag, al=al)
        if any(x in al for x in ("flux", "stablediffusion", "sd3", "sdxl", "pixart",
                                 "kandinsky", "kolors", "unet2dcondition", "latentconsistency")):
            return "image-gen", "image-gen"
        # True encoder-decoder families -> seq2seq / translation.
        _SEQ2SEQ = ("t5", "bart", "pegasus", "mbart", "marian", "m2m100", "nllb",
                    "mt5", "umt5", "longt5", "blenderbot", "prophetnet", "fsmt", "led")
        if any(x in al for x in _SEQ2SEQ):
            if any(x in al for x in ("marian", "m2m100", "nllb")) \
                    or pipeline_tag == "translation" or "translation" in tagset:
                return "text", "translation"
            return "text", "seq2seq"
        # Modern decoder LLMs that use a *ForConditionalGeneration head
        # (Mistral3, Llama4, Gemma3 — text-first even when multimodal-capable).
        if "forconditionalgeneration" in al or "lmheadmodel" in al:
            return "text", "generative-llm"

    # config model_type — reliable even when architectures is absent (older encoders).
    if model_type:
        d, c = _mt_classify(model_type.lower())
        if d:
            return d, c

    # Pipeline tag fallback.
    if pipeline_tag in _PT_CATEGORY:
        return "text", _PT_CATEGORY[pipeline_tag]
    if pipeline_tag in _AUDIO_PT_CAT:
        return "audio", _AUDIO_PT_CAT[pipeline_tag]
    if pipeline_tag in _IMAGEGEN_PT:
        return "image-gen", "image-gen"
    if pipeline_tag in _PT_DOMAIN:
        return _PT_DOMAIN[pipeline_tag], None

    return None, "unknown"


# Always-derived quant formats (AWQ/GPTQ/GGUF/etc. are never a base; fp8/fp4 are
# excluded here because they're often the *native* precision, not a derived copy).
_DERIVED_QUANT_RE = re.compile(
    r"(?:^|[-_./])(awq|gptq|gptqmodel|gguf|exl2|w4a16|w8a8|int4|int8|4bit|8bit|bnb|nf4|mlx)(?:$|[-_./])"
)
_R1_RE = re.compile(r"(?:^|[-_./])r1(?:$|[-_./])")
_BASE_SUFFIX_RE = re.compile(r"(?:^|[-_./])(base|truebase|pretrained?|pt)(?:$|[-_./])")


def parse_variant(repo_id, tags=()):
    """base | instruct | reasoning | finetune | quant.

    Discriminator is the *absence* of an instruct/chat suffix plus structured
    signals (base_model tags), NOT the presence of the word 'base' (see BOT-001).
    `quant` = a derived precision copy, kept distinct from a full-precision source
    during operator curation. `finetune` = derived but not instruct-suffixed
    (merges, domain tunes).
    """
    s = repo_id.lower()
    tagset = {t.lower() for t in (tags or [])}

    # Derived quantization (authoritative tag, else always-derived format in the name).
    if any(t.startswith("base_model:quantized") for t in tagset) or _DERIVED_QUANT_RE.search(s):
        return "quant"
    # Reasoning models.
    if any(k in s for k in ("reasoning", "thinking", "reasoner")) or _R1_RE.search(s):
        return "reasoning"
    # Explicit instruct/chat suffix in the NAME wins.
    if ("instruct" in s or "chat" in s or "-it" in s or s.endswith("-it")
            or "-sft" in s or "-dpo" in s or "-rlhf" in s):
        return "instruct"
    # Explicit base suffix overrides a stray 'conversational' tag (e.g. Qwen3-8B-Base).
    if _BASE_SUFFIX_RE.search(s):
        return "base"
    # Weaker instruct signal: the conversational tag.
    if "conversational" in tagset:
        return "instruct"
    # Derived finetune / merge (not a clean base).
    if (any(t.startswith(("base_model:finetune", "base_model:adapter", "base_model:merge"))
            for t in tagset)
            or any(k in s for k in ("slerp", "ties", "dare", "-merge", "frankenmerge", "distill"))):
        return "finetune"
    # Otherwise a clean foundation.
    return "base"
