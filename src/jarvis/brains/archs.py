"""Architecture names the bundled runtime's llama.cpp knows. READ FROM THE SOURCE, not typed from memory.

Source: ggml-org/llama.cpp commit 0c1e57098, file src/llama-arch.cpp, table LLM_ARCH_NAMES (153 names, the "(unknown)" placeholder excluded).
That commit is the one llama-cpp-python v0.3.36 vendors (vendor/llama.cpp), the version this project pins. If the pin changes, regenerate this list.
"Known to llama.cpp" means the loader has code for the name. It does NOT mean a given file loads, nor that the model is a chat model (the table also
holds embedding, vision, speech and draft-model architectures); brains/compat.py classifies them."""

LLAMA_CPP_COMMIT = "0c1e57098"
RUNTIME_KNOWN_ARCHITECTURES = frozenset((
    "clip", "llama", "llama4", "deci", "falcon", "grok",
    "gpt2", "gptj", "gptneox", "mpt", "baichuan", "starcoder",
    "refact", "bert", "modern-bert", "nomic-bert", "nomic-bert-moe", "neo-bert",
    "jina-bert-v2", "jina-bert-v3", "eurobert", "bloom", "stablelm", "qwen",
    "qwen2", "qwen2moe", "qwen2vl", "qwen3", "qwen3moe", "qwen3next",
    "qwen3vl", "qwen3vlmoe", "qwen35", "qwen35moe", "qwen4exp", "phi2",
    "phi3", "phimoe", "plamo", "plamo2", "plamo3", "codeshell",
    "orion", "internlm2", "minicpm", "minicpm3", "gemma", "gemma2",
    "gemma3", "gemma3n", "gemma4", "gemma4-assistant", "gemma-embedding", "starcoder2",
    "mamba", "mamba2", "maple", "jamba", "falcon-h1", "xverse",
    "command-r", "cohere2", "cohere2moe", "dbrx", "olmo", "olmo2",
    "olmoe", "muse-glimmer", "openelm", "arctic", "deepseek", "deepseek2",
    "deepseek2-ocr", "deepseek32", "deepseek4", "chatglm", "glm4", "glm4moe",
    "glm-dsa", "bitnet", "t5", "t5encoder", "jais", "jais2",
    "nemotron", "nemotron_h", "nemotron_h_moe", "exaone", "exaone4", "exaone-moe",
    "rwkv6", "rwkv6qwen2", "rwkv7", "arwkv7", "granite", "granitemoe",
    "granitehybrid", "graniteswitch", "granite_swa", "chameleon", "wavtokenizer-dec", "plm",
    "bailingmoe", "bailingmoe2", "bailingmoe3", "dots1", "dots3note", "arcee",
    "afmoe", "laguna", "ernie4_5", "ernie4_5-moe", "hunyuan-moe", "hunyuan-dense",
    "hunyuan_vl", "hy_v3", "hy_v4", "smollm3", "gpt-oss", "lfm2",
    "lfm2moe", "dream", "smallthinker", "llada", "llada-moe", "seed_oss",
    "grovemoe", "apertus", "minimax-01", "hrm_text", "minimax-m2", "minimax-m3",
    "cogvlm", "rnd1", "pangu-embedded", "mistral3", "eagle3", "dflash",
    "mistral4", "paddleocr", "mimo2", "step35", "spark2_5", "llama-embed",
    "maincoder", "kimi-linear", "kimi-k3", "glm5-next", "talkie", "mellum",
    "nanbeige", "qwen3tts", "pockettts",
))
