"""Can this PC run this GGUF model? A cautious, explainable ESTIMATE from the file's own header and the detected hardware.

Three results (never "Compatible" just because the file ends in .gguf or its header parses):
  compatible  - architecture is on the known-good list, memory estimate fits with headroom
  warnings    - it may work, but something is tight or unknown (the reasons say what)
  unsupported - it cannot work as a chat brain here (reasons say why); activation is refused

Every number is an ESTIMATE (labelled so). The real check is the load-and-generate test; simulated hardware in tests proves only this logic."""
from __future__ import annotations

from typing import Any

COMPATIBLE, WARNINGS, UNSUPPORTED = "compatible", "warnings", "unsupported"
LABELS = {COMPATIBLE: "Compatible", WARNINGS: "Compatible with Warnings", UNSUPPORTED: "Unsupported"}

from jarvis.brains.archs import LLAMA_CPP_COMMIT, RUNTIME_KNOWN_ARCHITECTURES
from jarvis.models.gguf_vocab import KNOWN_PRE_TOKENIZERS, KNOWN_TOKENIZER_MODELS, NO_VOCAB_TOKENIZER_MODELS

# Text-generation architectures Project JARVIS treats as chat models. Every name is checked (by a test) to exist in the runtime's own table
# (brains/archs.py, read from llama.cpp's source at the commit the pinned runtime vendors). Being in the table is necessary, not sufficient: only a real
# load on the user's PC proves a specific file works.
SUPPORTED_CHAT_ARCHITECTURES = frozenset({
    "llama", "falcon", "grok", "gpt2", "gptj", "gptneox", "mpt", "baichuan", "starcoder", "starcoder2", "refact", "bloom", "stablelm", "qwen", "qwen2",
    "qwen2moe", "qwen3", "qwen3moe", "qwen3next", "phi2", "phi3", "phimoe", "plamo", "plamo2", "codeshell", "orion", "internlm2", "minicpm", "minicpm3",
    "gemma", "gemma2", "gemma3", "gemma3n", "mamba", "mamba2", "xverse", "command-r", "cohere2", "dbrx", "olmo", "olmo2", "olmoe", "openelm", "arctic",
    "deepseek", "deepseek2", "chatglm", "glm4", "glm4moe", "bitnet", "jais", "nemotron", "nemotron_h", "exaone", "exaone4", "granite", "granitemoe",
    "granitehybrid", "smollm3", "hunyuan-moe", "hunyuan-dense", "lfm2", "lfm2moe", "gpt-oss", "arcee", "deci", "ernie4_5", "ernie4_5-moe", "falcon-h1",
    "llama4", "mistral3", "seed_oss", "jamba", "smallthinker", "apertus", "dots1", "minimax-m2", "bailingmoe", "bailingmoe2", "rwkv6", "rwkv7",
})
# Valid GGUF files in the runtime's table that are NOT usable as Jarvis's chat brain, with the reason shown to the person.
NON_CHAT_ARCHITECTURES = {
    **{a: "an embedding or encoder model (it produces vectors, not replies)" for a in ("bert", "nomic-bert", "nomic-bert-moe", "jina-bert-v2", "jina-bert-v3", "neo-bert",
                                                                                   "modern-bert", "eurobert", "gemma-embedding", "llama-embed", "t5encoder")},
    **{a: "a vision, image or document-reading model; Jarvis has no image input" for a in ("clip", "cogvlm", "hunyuan_vl", "qwen3vl", "qwen3vlmoe", "qwen2vl", "paddleocr",
                                                                                         "deepseek2-ocr")},
    **{a: "a speech or audio component" for a in ("wavtokenizer-dec", "pockettts", "qwen3tts")},
    **{a: "a diffusion language model, which the runtime's chat path does not drive like a normal chat model" for a in ("dream", "llada", "llada-moe", "rnd1")},
    **{a: "a small helper ('draft') model for speeding up another model, not a standalone chat model" for a in ("eagle3", "dflash", "gemma4-assistant")},
}
# Extra, specific wording for architectures people ask about. Facts come from the runtime source at LLAMA_CPP_COMMIT (src/models/deepseek*.cpp, llama-vocab.cpp).
ARCH_NOTES = {
    "deepseek": "DeepSeek (first-generation mixture-of-experts) has a loader in the bundled runtime. Not loaded or tested by Project JARVIS.",
    "deepseek2": "DeepSeek-V2 / V2-Lite / V3 / R1 (mixture-of-experts with multi-head latent attention) has a loader in the bundled runtime (src/models/deepseek2.cpp). "
                 "These models are very large (the full V3/R1 files are hundreds of GB), so the memory check usually decides; the cache memory shown is a rough guess because "
                 "this attention type caches differently. Not loaded or tested by Project JARVIS.",
}
ARCH_WARNINGS = {
    "deepseek32": "DeepSeek-V3.2 (sparse attention) appears in the bundled runtime's source, but Project JARVIS has not classified or tested it; it may need features the pinned runtime handles differently.",
    "deepseek4": "DeepSeek-V4 appears in the bundled runtime's source, but Project JARVIS has not classified or tested it.",
}
# Architectures whose loader reads attention.kv_lora_rank (latent attention; grep of src/models/*.cpp at LLAMA_CPP_COMMIT): their cache is NOT 2 x layers x kv_heads x head_dim.
MLA_ARCHITECTURES = frozenset({"deepseek2", "deepseek32", "glm-dsa", "glm5-next", "minicpm3", "plm", "kimi-linear", "kimi-k3", "bailingmoe3", "dots3note", "hy-v4"})

KV_BYTES = 2                    # f16 key/value cache (llama-cpp-python's default)
OVERHEAD_GB = 1.0               # compute buffers + runtime, a rough fixed allowance (ESTIMATE)
FALLBACK_KV_GB_PER_1K_CTX_PER_B = 0.06   # only when the header lacks the shape fields: very rough, flagged as such


def _gb(x: float) -> float:
    return round(x / 1e9, 2)


def estimate_memory(size_bytes: int, gguf: dict, n_ctx: int) -> dict:
    """Weights (the file is memory-mapped, so ~its size) + KV cache + fixed overhead. Returns the parts and whether the KV figure is computed
    from the header's own layer/head fields ('header') or only guessed ('rough')."""
    layers, kv_heads, heads = gguf.get("block_count"), gguf.get("head_count_kv") or gguf.get("head_count"), gguf.get("head_count")
    hd = gguf.get("key_length") or (gguf.get("embedding_length") // heads if gguf.get("embedding_length") and heads else None)
    if layers and kv_heads and hd and gguf.get("architecture") not in MLA_ARCHITECTURES:
        kv = 2 * layers * n_ctx * kv_heads * hd * KV_BYTES
        basis = "header"
    else:
        billions = max(size_bytes / 1e9 / 0.55, 0.5)          # ~0.55 GB per billion parameters at 4-bit: only used for the rough fallback
        kv = FALLBACK_KV_GB_PER_1K_CTX_PER_B * billions * (n_ctx / 1024) * 1e9
        basis = "rough"
    total = size_bytes + kv + OVERHEAD_GB * 1e9
    return {"weights_gb": _gb(size_bytes), "kv_cache_gb": _gb(kv), "overhead_gb": OVERHEAD_GB, "total_gb": _gb(total), "kv_basis": basis,
            "n_ctx": n_ctx, "kind": "ESTIMATE"}


def assess(gguf: dict, size_bytes: int, hw: Any, runtime: dict | None = None, n_ctx: int | None = None, *, architecture_expected: str | None = None,
           from_catalog: bool = False) -> dict:
    """gguf = jarvis.models.gguf.summarize(...). hw = jarvis.hardware.Hardware (or any object with the same attributes; None values mean unknown)."""
    reasons: list[dict] = []
    add = lambda sev, text: reasons.append({"severity": sev, "text": text})      # noqa: E731
    arch = gguf.get("architecture")
    facts = {"architecture": arch, "quantization": gguf.get("quantization"), "size_gb": _gb(size_bytes), "model_context_tokens": gguf.get("context_length"),
             "has_chat_template": bool(gguf.get("has_chat_template")), "name": gguf.get("name")}
    if not arch:
        add("block", "The file does not declare an architecture, so it cannot be matched to anything the runtime can run.")
    elif arch in NON_CHAT_ARCHITECTURES:
        add("block", f"Architecture '{arch}' is {NON_CHAT_ARCHITECTURES[arch]}, not a chat model. It cannot be a brain.")
    elif arch not in RUNTIME_KNOWN_ARCHITECTURES:
        add("block", f"Architecture '{arch}' is not one the bundled runtime (llama.cpp {LLAMA_CPP_COMMIT}) knows how to load, so it cannot run here. It may need a newer "
                     "version of Project JARVIS. Nothing was loaded.")
    elif arch not in SUPPORTED_CHAT_ARCHITECTURES:
        add("warn", ARCH_WARNINGS.get(arch) or f"The bundled runtime knows architecture '{arch}', but Project JARVIS has not classified it as a chat model, so it may not "
                    "behave as one. A test is needed before relying on it.")
    if arch in ARCH_NOTES:
        add("info", ARCH_NOTES[arch])
    gtype = gguf.get("general_type")
    if gtype and gtype != "model":
        add("block", f"The file says it is a '{gtype}' file, not a standalone model (an adapter, vision projector or calibration file). It cannot be a brain.")
    if gguf.get("tensor_count") == 0:
        add("block", "The file contains no model weights (zero tensors). It is a vocabulary-only or empty file.")
    if (gguf.get("split_count") or 0) > 1:
        add("block", f"This is one part of a model split into {gguf['split_count']} files. Project JARVIS imports single-file models only; merge the parts first.")
    tm, pre = gguf.get("tokenizer_model"), gguf.get("tokenizer_pre")
    if not from_catalog:
        if tm is None:
            add("warn", "The file does not state which tokenizer it uses, so whether the runtime can load it is unknown.")
        elif tm in NO_VOCAB_TOKENIZER_MODELS:
            add("block", f"The file has no usable vocabulary (tokenizer '{tm}'), so it cannot read or write text.")
        elif tm not in KNOWN_TOKENIZER_MODELS:
            add("block", f"The file uses tokenizer '{tm}', which the bundled runtime (llama.cpp {LLAMA_CPP_COMMIT}) does not know; loading it would fail.")
        elif tm == "gpt2":                       # byte-pair encoding: the runtime also needs the 'pre-tokenizer' name
            if not pre:
                add("warn", "The file names no pre-tokenizer. The runtime would use a default one and its own loader warns that generation quality will be degraded.")
            elif pre not in KNOWN_PRE_TOKENIZERS:
                add("block", f"The file uses pre-tokenizer '{pre}', which the bundled runtime does not know; its loader refuses such files ('unknown pre-tokenizer type').")
            elif pre == "deepseek-r1-qwen":
                add("info", "DeepSeek-R1 distilled model (a Qwen/Llama model trained on DeepSeek-R1 output): a supported kind. It writes its reasoning first; Jarvis hides that part.")
        if arch == "deepseek2" and gguf.get("kv_lora_rank") is None:
            add("block", "A DeepSeek-V2/V3 file must state its attention.kv_lora_rank; this one does not, and the runtime requires it.")
    if architecture_expected and arch and arch != architecture_expected:
        add("block", f"The file declares architecture '{arch}', but this entry names '{architecture_expected}'. It is not the model it claims to be.")
    rt = runtime or {}
    if runtime is not None and rt.get("available") is False:
        add("block", "The local AI runtime (llama-cpp-python) is not available in this installation, so no model can be loaded: " + str(rt.get("error", "unknown reason")))
    mctx = gguf.get("context_length")
    ctx = int(n_ctx or min(mctx or 4096, 4096))
    if mctx and ctx > mctx:
        ctx = int(mctx)
        add("info", f"The context window was limited to the model's own maximum of {mctx} tokens.")
    est = estimate_memory(size_bytes, gguf, ctx)
    if est["kv_basis"] == "rough":
        add("info" if from_catalog else "warn", "The cache memory is only a rough guess: " + ("the catalog does not list the layer/head shape." if from_catalog
                                                                                            else "the file does not state its layer/head shape."))
    if not gguf.get("quantization"):
        add("info", "The quantization (compression) level is not stated in the file.")
    if not gguf.get("has_chat_template") and not from_catalog:
        add("warn", "The file has no built-in chat template; replies may be badly formatted until a template is chosen. Jarvis cannot add one for you yet.")
    if gguf.get("expert_count"):
        add("info", "This is a mixture-of-experts model: all experts are loaded in memory even though only some run per word, so it is larger in RAM than its speed suggests.")
    total, avail = getattr(hw, "ram_gb", None), getattr(hw, "ram_available_gb", None)
    vram, gpu = getattr(hw, "vram_gb", None), getattr(hw, "gpu_name", None)
    need = est["total_gb"]
    gpu_backend = rt.get("gpu_offload_supported") is True
    mode = "cpu"
    if gpu and gpu_backend and vram is not None and vram >= need:
        mode = "gpu"
    if total is None:
        add("warn", "Total RAM could not be detected, so whether this fits is unknown.")
    elif mode == "cpu" and need > total:
        add("block", f"Needs about {need} GB of memory (estimate) but this PC has {total:.1f} GB in total. Loading it would very likely fail or freeze Windows.")
    elif mode == "cpu" and need > 0.85 * total:
        add("warn", f"Needs about {need} GB (estimate) of this PC's {total:.1f} GB. That leaves very little for Windows and other programs; expect slowness.")
    if mode == "cpu" and total is not None and avail is not None and need > avail and need <= total:
        add("warn", f"Only {avail:.1f} GB of memory is free right now (estimate needs {need} GB). Close other programs before activating.")
    if avail is None and total is not None:
        add("info", "Currently free memory could not be read.")
    if gpu and not gpu_backend:
        add("info", f"A GPU ({gpu}) was detected, but the installed runtime has no GPU support, so the CPU would be used. Not verified either way.")
    elif mode == "gpu":
        add("info", f"Estimated to fit in the GPU's {vram:.1f} GB (estimate; GPU speed is not verified until a test measures it).")
    if getattr(hw, "avx2", None) is False:
        add("warn", "This CPU does not report AVX2 support; whether the bundled runtime works without it is unverified.")
    if size_bytes < 50_000_000:
        add("warn", "The file is very small for a language model; it may be a stub or a fragment.")
    level = UNSUPPORTED if any(r["severity"] == "block" for r in reasons) else WARNINGS if any(r["severity"] == "warn" for r in reasons) else COMPATIBLE
    if level == COMPATIBLE:
        add("info", "Architecture is on the known-good list and the memory estimate fits with headroom. This is an estimate, not a test; use Test Brain for proof.")
    tier = {COMPATIBLE: "expected", WARNINGS: "uncertain", UNSUPPORTED: "unsupported"}[level]
    support = {"tier": tier, "verified_on_this_pc": False,
               "text": {"expected": "Expected to work (architecture and tokenizer are known to the runtime). NOT verified: it has not been loaded here.",
                        "uncertain": "May work; see the warnings. NOT verified: it has not been loaded here.",
                        "unsupported": "Cannot work on this PC as a chat brain."}[tier]}
    return {"level": level, "label": LABELS[level], "support": support, "reasons": reasons, "estimate": est, "mode_estimate": mode if level != UNSUPPORTED else "none",
            "facts": facts, "estimates_are_not_measurements": True,
            "can_activate": level != UNSUPPORTED}
