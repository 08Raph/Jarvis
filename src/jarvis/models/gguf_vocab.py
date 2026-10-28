"""Tokenizer names the bundled llama.cpp runtime accepts (data only; see provenance comment)."""
# Tokenizer facts, read from the same commit's src/llama-vocab.cpp. An unknown tokenizer MODEL, or a non-empty BPE pre-tokenizer name that is not in
# KNOWN_PRE_TOKENIZERS, makes llama.cpp's loader THROW ("unknown tokenizer" / "unknown pre-tokenizer type"), so such a file cannot load. A BPE file with
# NO pre-tokenizer name loads with the 'default' one and the loader warns that generation quality will be degraded.
KNOWN_TOKENIZER_MODELS = frozenset((
    "bert", "gemma4", "gpt2", "hybriddna", "llama", "no_vocab",
    "none", "plamo2", "rwkv", "t5", "test", "whitespace",
))
KNOWN_PRE_TOKENIZERS = frozenset((
    "a.x-4.0", "afmoe", "bailingmoe", "bailingmoe2", "bloom", "chameleon",
    "chatglm-bpe", "codeshell", "cohere2moe", "command-r", "dbrx", "deepseek-coder",
    "deepseek-llm", "deepseek-r1-qwen", "deepseek-v3", "default", "exaone", "exaone-moe",
    "exaone4", "f2llmv2", "falcon", "falcon-h1", "falcon3", "gemma4",
    "gigachat", "glm4", "glm5", "gpt-2", "gpt-4o", "gpt3-finnish",
    "granite-docling", "granite-embed-multi-311m", "granite-embed-multi-97m", "grok-2", "hunyuan", "hunyuan-dense",
    "hy_v4", "jais", "jais-2", "jina-de", "jina-es", "jina-v1-en",
    "jina-v2-code", "jina-v2-de", "jina-v2-es", "jina-v5-nano", "joyai-llm", "kanana2",
    "kimi-k2", "kormo", "laguna", "lfm2", "llada-moe", "llama-bpe",
    "llama-v3", "llama3", "llama4", "megrez", "mellum", "mellum2",
    "midm-2.0", "minerva-7b", "minicpm5", "minimax-m2", "modern-bert", "mpt",
    "olmo", "phi-2", "pixtral", "poro-chat", "qwen2", "qwen35",
    "refact", "roberta-bpe", "sarvam-moe", "seed-coder", "smaug-bpe", "smollm",
    "solar-open", "spark2_5", "stablelm2", "starcoder", "superbpe", "talkie",
    "tekken", "tiny_aya", "trillion", "ufakzeka", "viking", "whitespace",
    "youtu",
))
# Tokenizer models that mean "no usable vocabulary for chat".
NO_VOCAB_TOKENIZER_MODELS = frozenset(("none", "no_vocab", "test"))
