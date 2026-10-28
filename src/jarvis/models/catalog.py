"""Built-in list of FREE, open-weight chat models the app can fetch, with the license facts that were checked.
Nothing is bundled in the installer: the user's PC downloads a model from its publisher after explicit consent.

Verification notes (see docs/LOCAL_MODEL.md): the license and size below were read from the publisher's Hugging Face page
during development. The exact file name and SHA-256 are resolved from the host's own metadata at download time (HTTPS),
not hard-coded here, because they could not be fetched from the authoring sandbox."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from jarvis.net.policy import NetworkPolicy

HF_HOSTS = ("huggingface.co", "hf.co")


@dataclass(frozen=True)
class CatalogModel:
    id: str
    title: str
    repo: str                 # Hugging Face repository holding the GGUF files
    file_regex: str           # which file in the repo to fetch
    license_spdx: str
    license_url: str
    approx_size_bytes: int    # from the model page; the real size is read from host metadata and bounded
    min_ram_gb: float
    context_tokens: int
    note: str = ""
    no_think_suffix: bool = False   # Qwen3: " /no_think" turns off the long hidden "thinking" phase
    quant: str = "Q4_K_M"
    params_b: float | None = None        # billions of parameters (publisher's figure)
    metadata_status: str = "publisher-page-read-during-development"   # or "from-publisher-knowledge-NOT-fetched": size/licence unconfirmed
    verified_hardware: tuple[str, ...] = ()   # configurations where this model was actually loaded AND answered; empty = none yet
    redistribution: str = "Not bundled; the user's PC downloads it from the publisher after consent."
    sampling: tuple = ()          # publisher-recommended sampling for the non-thinking mode, as ((name, value), ...); used by sampling_profile="publisher"
    auto_recommend: bool = True   # False = fetchable on request (and benchmarked) but never chosen by the setup wizard / selector
    thinking_control: str = "suffix"   # how the hidden "thinking" phase is switched off: "suffix" (" /no_think"), "empty_think" (ChatML + empty think block), "none"
    architecture: str = ""        # GGUF general.architecture the file must declare (Qwen3 dense models: "qwen3", per the llama.cpp converter); "" = not checked


BUILTIN: tuple[CatalogModel, ...] = (
    CatalogModel(
        id="qwen3-1.7b-q4_k_m", title="Qwen3 1.7B (4-bit)", repo="ggml-org/Qwen3-1.7B-GGUF",
        file_regex=r"(?i)q4_k_m.*\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(1.28e9),
        min_ram_gb=4.0, context_tokens=4096, no_think_suffix=True,
        params_b=1.7, sampling=(("top_p", 0.8), ("top_k", 20), ("min_p", 0.0)),
        architecture="qwen3",
        note="General small chat model by the Qwen team (Alibaba Cloud). Converted to GGUF by the llama.cpp organisation."),
    CatalogModel(
        id="qwen3-4b-q4_k_m", title="Qwen3 4B (4-bit)", repo="ggml-org/Qwen3-4B-GGUF",
        file_regex=r"(?i)q4_k_m.*\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(2.5e9),
        min_ram_gb=6.0, context_tokens=8192, no_think_suffix=True, params_b=4.0, sampling=(("top_p", 0.8), ("top_k", 20), ("min_p", 0.0)),
        metadata_status="from-publisher-knowledge-NOT-fetched",
        architecture="qwen3",
        note="Mid-size Qwen3 model. Size and licence are NOT confirmed here (the publisher's page could not be reached from the build "
             "sandbox); the real size and file are read from the host at download time and the licence must be checked then."),
    CatalogModel(
        id="qwen3-8b-q4_k_m", title="Qwen3 8B (4-bit)", repo="Qwen/Qwen3-8B-GGUF",
        file_regex=r"(?i)^Qwen3-8B-Q4_K_M\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(5.03e9),
        min_ram_gb=10.0, context_tokens=8192, no_think_suffix=True, params_b=8.0, auto_recommend=False,
        architecture="qwen3",
        note="Larger Qwen3 model published by the Qwen team (Alibaba Cloud) itself, for machines with a capable GPU or plenty of RAM. Install it by choosing it yourself: the setup wizard never selects it for you. Repository, "
             "file name, size (5.03 GB, rounded) and the Apache-2.0 tag were read from the publisher's Hugging Face page (a summarised web page, "
             "not the API). The exact size and SHA-256 are read from the host at download time; the file is refused if the host gives no "
             "checksum, unless you explicitly confirm an unchecked download."),
)


CANDIDATES: tuple[CatalogModel, ...] = (
    CatalogModel(
        id="qwen3.5-4b-q4_k_m", title="Qwen3.5 4B (4-bit, benchmark candidate)", repo="unsloth/Qwen3.5-4B-GGUF",
        file_regex=r"(?i)^Qwen3\.5-4B-Q4_K_M\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(2.74e9),
        min_ram_gb=6.0, context_tokens=8192, no_think_suffix=False, params_b=4.0, thinking_control="none",
        sampling=(("top_p", 0.8), ("top_k", 20), ("min_p", 0.0), ("presence_penalty", 1.5)), auto_recommend=False,
        note="Qwen team (Alibaba Cloud) model, GGUF conversion by Unsloth (third party). Size and licence read from the publisher's pages in "
             "Stage 5; whether the bundled llama.cpp runtime can run its architecture is checked by the Stage 5 probe. Not recommended "
             "automatically: it is benchmarked, not adopted."),
)


def get(model_id: str) -> CatalogModel:
    for m in BUILTIN + CANDIDATES:
        if m.id == model_id:
            return m
    raise KeyError(model_id)


def resolve_file(policy: NetworkPolicy, model: CatalogModel, base: str = "https://huggingface.co",
                 hosts: tuple[str, ...] = HF_HOSTS) -> dict:
    """Ask the host for the repo's file list (with checksums) and pick the file. Returns
    {'name','url','size_bytes','sha256'|None}. Goes through NetworkPolicy ('model_download' must be enabled)."""
    api = f"{base}/api/models/{model.repo}?blobs=true"
    with policy.open("model_download", api, timeout=30, allowed_host_suffixes=hosts) as r:
        data = json.loads(r.read(5_000_000).decode("utf-8"))
    # The host's answer is untrusted input: anything that is not the expected shape is reported as ONE clean error (LookupError), never as a raw
    # TypeError/AttributeError, and a size that is present but not a plain non-negative whole number is refused instead of being guessed.
    if not isinstance(data, dict) or not isinstance(data.get("siblings", []), list):
        raise LookupError(f"the host's file list for {model.repo} is not in the expected format; nothing was downloaded")
    rx = re.compile(model.file_regex)
    cands = [s for s in data.get("siblings", []) if isinstance(s, dict) and isinstance(s.get("rfilename"), str)
             and rx.search(s["rfilename"]) and "/" not in s["rfilename"]]
    if len(cands) != 1:
        raise LookupError(f"expected exactly one matching file in {model.repo}, found {len(cands)}; "
                          "the publisher may have changed the repository")
    c = cands[0]
    lfs = c.get("lfs")
    if lfs is not None and not isinstance(lfs, dict):
        raise LookupError(f"the host's information for {c['rfilename']} is not in the expected format; nothing was downloaded")
    lfs = lfs or {}
    sha = lfs.get("sha256") or lfs.get("oid")
    if sha and not re.fullmatch(r"[0-9a-f]{64}", str(sha).lower()):
        sha = None
    size = _host_size(lfs.get("size") or c.get("size"), c["rfilename"])
    name = c["rfilename"]
    rev = data.get("sha")        # the repository commit the host listed (provenance only: the file URL still follows the branch)
    rev = str(rev).lower() if isinstance(rev, str) and re.fullmatch(r"[0-9a-fA-F]{40}", rev) else None
    return {"name": name, "size_bytes": size, "sha256": str(sha).lower() if sha else None, "revision": rev,
            "url": f"{base}/{model.repo}/resolve/main/{name}"}


def _host_size(value, name: str) -> int:
    """0 = the host gave no size (callers then rely on the catalog estimate). A given size must be a whole number of bytes >= 0: a boolean, a float,
    text that is not digits, or a negative number means the answer cannot be trusted, so nothing is downloaded."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not value.isascii()) \
            or (isinstance(value, str) and not value.isdigit()) or (isinstance(value, int) and value < 0):
        raise LookupError(f"the host reported an unusable size for {name}; nothing was downloaded")
    return int(value)
