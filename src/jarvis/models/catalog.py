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
    auto_recommend: bool = True   # False = fetchable on request (and benchmarked) but never chosen by the setup wizard / selector
    thinking_control: str = "suffix"   # how the hidden "thinking" phase is switched off: "suffix" (" /no_think"), "empty_think" (ChatML + empty think block), "none"


BUILTIN: tuple[CatalogModel, ...] = (
    CatalogModel(
        id="qwen3-1.7b-q4_k_m", title="Qwen3 1.7B (4-bit)", repo="ggml-org/Qwen3-1.7B-GGUF",
        file_regex=r"(?i)q4_k_m.*\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(1.28e9),
        min_ram_gb=4.0, context_tokens=4096, no_think_suffix=True,
        params_b=1.7,
        note="General small chat model by the Qwen team (Alibaba Cloud). Converted to GGUF by the llama.cpp organisation."),
    CatalogModel(
        id="qwen3-4b-q4_k_m", title="Qwen3 4B (4-bit)", repo="ggml-org/Qwen3-4B-GGUF",
        file_regex=r"(?i)q4_k_m.*\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(2.5e9),
        min_ram_gb=6.0, context_tokens=8192, no_think_suffix=True, params_b=4.0,
        metadata_status="from-publisher-knowledge-NOT-fetched",
        note="Mid-size Qwen3 model. Size and licence are NOT confirmed here (the publisher's page could not be reached from the build "
             "sandbox); the real size and file are read from the host at download time and the licence must be checked then."),
    CatalogModel(
        id="qwen3-8b-q4_k_m", title="Qwen3 8B (4-bit)", repo="ggml-org/Qwen3-8B-GGUF",
        file_regex=r"(?i)q4_k_m.*\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(5.0e9),
        min_ram_gb=10.0, context_tokens=8192, no_think_suffix=True, params_b=8.0,
        metadata_status="from-publisher-knowledge-NOT-fetched",
        note="Larger Qwen3 model for machines with a capable GPU or plenty of RAM. Size and licence NOT confirmed here (see the 4B note)."),
)


CANDIDATES: tuple[CatalogModel, ...] = (
    CatalogModel(
        id="qwen3.5-4b-q4_k_m", title="Qwen3.5 4B (4-bit, benchmark candidate)", repo="unsloth/Qwen3.5-4B-GGUF",
        file_regex=r"(?i)^Qwen3\.5-4B-Q4_K_M\.gguf$", license_spdx="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", approx_size_bytes=int(2.74e9),
        min_ram_gb=6.0, context_tokens=8192, no_think_suffix=False, params_b=4.0, thinking_control="empty_think",
        auto_recommend=False,
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
    rx = re.compile(model.file_regex)
    cands = [s for s in data.get("siblings", []) if isinstance(s, dict) and rx.search(s.get("rfilename", ""))
             and "/" not in s.get("rfilename", "")]
    if len(cands) != 1:
        raise LookupError(f"expected exactly one matching file in {model.repo}, found {len(cands)}; "
                          "the publisher may have changed the repository")
    c = cands[0]
    lfs = c.get("lfs") or {}
    sha = lfs.get("sha256") or lfs.get("oid")
    if sha and not re.fullmatch(r"[0-9a-f]{64}", str(sha).lower()):
        sha = None
    size = int(lfs.get("size") or c.get("size") or 0)
    name = c["rfilename"]
    return {"name": name, "size_bytes": size, "sha256": str(sha).lower() if sha else None,
            "url": f"{base}/{model.repo}/resolve/main/{name}"}
