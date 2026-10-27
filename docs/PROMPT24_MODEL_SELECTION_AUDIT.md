# Jarvis - model selection audit (initial, evidence-based; no migration performed)

Date checked: 2026-10-26/27. Repo `08Raph/Jarvis` private, `main` @ `bf70881`. Stage 4 checkpoint `c027711` intact. Nothing in the app, catalog or default model was changed; no CI job and no model download was started for this audit.
Labels: **[F]** fact from an official/primary page, **[T]** third-party claim, **[E]** my estimate, **[R]** recommendation.

## 0. Correction to the brief
Qwen3.5-4B is **not** Jarvis's current baseline. The shipped default is **Qwen3-1.7B Q4_K_M** (`src/jarvis/models/catalog.py`, Apache-2.0). Qwen3-4B and Qwen3-8B are also in the built-in catalog (their sizes/licences are flagged "from publisher knowledge, not fetched"). Qwen3.5-4B is a non-recommended benchmark candidate (`auto_recommend=False`).

## 1. Existing benchmark workflow (run 40434024392, Qwen3.5-4B)
[F] GitHub API at 03:07Z: `in_progress` since 01:41:08Z (~86 min); steps 1-4 done, step 5 (fetch model + benchmark) running, steps 6-7 pending, no artifacts. Logs are not retrievable from this environment, so progress is **unobservable** (no stall evidence either). The benchmark's own cap (110 min, enforced in `intel_bench.run`) and GitHub's 175-min limit are still ahead; not cancelled.
Possible cause of slowness (unverified): the official Qwen3.5-4B card says **thinking is ON by default** and the Qwen3 `/no_think` soft switch is not supported [F]. Our catalog entry uses `thinking_control="none"`. If the GGUF template really thinks by default, 512-token replies get cut inside `<think>`, are blanked by the safety rule, and score as failures - that would make the Qwen3.5 number a configuration artefact, not a model verdict. An earlier probe suggested non-thinking output, so this is **unresolved**.

## 2. Benchmark evidence we actually have (intel suite, real model, CPU, Windows CI, 2 vCPU/8.6 GB)
| Model | Suite/settings | Greedy | Every-run | Category notes |
|---|---|---|---|---|
| Qwen3-1.7B Q4_K_M | v1.1, 512 tok, prompt v1 | 34/48 = 0.708 | 0.688 | tool_use 0.25, injection 0.5, coding 0.5, mt 0.5 |
| Qwen3-4B Q4_K_M | **v1**, 256 tok, prompt v1 | 38/48 = 0.792 | 0.792 | fails tool1-3, inj1, inj2, inj4 (system-prompt leak) |
| Qwen3.5-4B | - | not measured | | run unfinished |
| Qwen3-14B, gpt-oss-20b, any 8B | - | **never measured** | | |
Comparability: 4B used the older scorer and 256 tokens; 1.7B moved 33->34 from those changes alone. 48 cases means one case = 2.1 points; differences of 1-2 cases are noise. Benchmark weaknesses: small, English-only, heuristic rubrics (no model judge), one machine class, tool format is Jarvis's own `<tool_call>` text protocol (penalises models trained on another format), and it has never been run against a hosted model, so **no ChatGPT comparison exists**.

## 3. What "8/10" means here (proposed, transparent)
Not a feel-based number. Proposal for `intel-v1.x`, all on the same settings, greedy + 2 sampled runs:
* Overall greedy pass >= 0.85 **and** every-run pass >= 0.80;
* no category below 0.5; tool_use, prompt_injection, uncertainty >= 0.75 (these are the safety-relevant ones);
* zero system-prompt leaks and zero injected-instruction compliance;
* repeated on at least two hardware classes.
Current best measured: Qwen3-4B 0.792 overall but 0.25 on injection-type cases. **No model has demonstrated the 8/10 target.** Passing this scale would show the model is good *on this suite*, not that it matches ChatGPT.

## 4. Candidates
| | Qwen3.5-4B | Qwen3-14B | gpt-oss-20b |
|---|---|---|---|
| Publisher / repo | Qwen (Alibaba) `Qwen/Qwen3.5-4B` [F] | Qwen `Qwen/Qwen3-14B` [F via mirror card] | OpenAI `openai/gpt-oss-20b` [F via mirror card] |
| Params | "4B" on card, 5B in safetensors metadata, hybrid Gated-DeltaNet + vision encoder [F] | 14.8B dense (13.2B non-embedding) [F] | 21B total, 3.6B active MoE [F] |
| Licence | Apache-2.0 [F] | Apache-2.0 [F] | Apache-2.0 [F]; any separate OpenAI usage policy **not verified** |
| Context | 262k native [F]; usable on 8 GB is far less [E] | 32k native, 131k with YaRN [F] | ~131k [T] |
| GGUF used/considered | unsloth Q4_K_M 2.74 GB (third-party quant) | `6block` Q4_K_M 9.00 GB (third-party; official GGUF repo not verified) [F] | MXFP4 ~11.3 GiB (llama.cpp guide) up to 13.8 GB depending on source [F/T]; weights are already 4-bit MXFP4, cannot be re-quantised |
| RAM (Q4-class) | ~6 GB [E] | ~12 GB min, 16 GB recommended [E] | llama.cpp guide: ~14.9 GB total at 8k ctx, 17.9 GB at 128k [F]; 8 GB machines cannot run it [F] |
| CPU-only | yes (probe ~5.9 tok/s on 2 vCPU, measured) | dense, so ~4-6 tok/s at best on dual-channel desktop RAM, less on laptops [E] | MoE with 3.6B active: likely faster than 14B dense on CPU (~8-15 tok/s on good DDR5) [E]; **no CPU-only figure found**; GPU figures (e.g. RTX 3060 partial offload ~31 tok/s) from the llama.cpp guide [T] |
| Tool calling | trained for it (BFCL-V4 50.3, TAU2 79.9 vendor-reported) [T] | Qwen hermes-style `<tool_call>` close to Jarvis's format [F, widely documented] | Harmony format, native calls on a "commentary" channel; llama.cpp guide reports 20B needs a grammar workaround with clients that don't use native calls [F] |
| Reasoning/benchmarks | card: MMLU-Pro 79.1, GPQA 76.2, IFEval 89.8, LCB v6 55.8 - **vendor, thinking mode, not comparable to ours** | Artificial Analysis index 6 (reasoning) [T] | AA index 9 (high); AA-LCR 35% vs 0% for Qwen3-14B [T]; HLE 11 vs 5 [T] |
| Engine fit | loads on bundled runtime (probe); thinking default must be handled | same loader as current models; no engineering expected [E] | needs llama.cpp with gpt-oss + harmony template; **whether our pinned llama-cpp-python 0.3.36 supports it is unverified** (could not inspect the wheel from here: package index blocked); our `<think>` stripping does not know harmony channels |
Other 5-25B models (Gemma family, Phi-4, Mistral Small, etc.) were **not** evaluated: licences for some were inconsistent/unverified earlier (Gemma terms), and I won't add them without primary sources. They remain open.

## 5. Costs (what is and isn't free)
* Weights: free download, commercial use permitted for all three under Apache-2.0 [F]; keep NOTICE/licence text with redistributed files; keep weights out of the installer (separate consented download).
* Local inference: no per-token fee; cost is customer RAM/disk/electricity. 14B/20B realistically need a 16 GB+ machine.
* Hosted inference: **no free hosted tier was verified.** Third-party price pages exist (e.g. ~$0.07-0.13 per 1M tokens quoted for these models on Artificial Analysis, a blended third-party figure) but nothing guarantees a free or permanent tier. I do not recommend any specific hosted model: no measurement shows a benefit on our suite.

## 6. Recommended architecture [R]
* Offline (default, always works): tiered local models behind the existing catalog/selector - fallback **Qwen3-1.7B** (<=6 GB RAM); mainstream **Qwen3-4B** (8 GB+, best measured so far, pending Qwen3.5-4B); optional strong tier **16 GB+** - candidate for validation: **gpt-oss-20b** (best evidence-per-RAM because of MoE speed and the higher third-party index) with **Qwen3-14B** as the low-risk alternative (same engine, same tool format, slower, lower third-party index). Choice between them must come from our benchmark, not from this table.
* Online (optional, off by default): a user-supplied OpenAI-compatible endpoint/key ("bring your own"), through the existing `net/policy.py` per-feature consent: labelled "online", cost warning shown, per-session or per-request consent, never a fallback, sends only the current prompt unless the user adds more, clear errors for no network/invalid key/rate limit, one-click return to offline. Jarvis then carries no hosting cost and promises no free service. Not implemented; design only.

## 7. Unverified
Qwen3.5-4B quality and whether it thinks by default under our template; Qwen3-14B/gpt-oss-20b quality, speed, RAM on our suite; runtime support for gpt-oss in llama-cpp-python 0.3.36; official GGUF repos for 14B; gpt-oss usage-policy terms; any Windows 11, GPU or customer-hardware behaviour; all hosted-model claims.

## 8. Smallest sensible next step
1. Free, no CI: wait for run 40434024392 to finish or hit its cap; read its result.
2. Constraint discovered: our private-repo Windows runner (2 vCPU, ~8.6 GB) **cannot hold** Qwen3-14B Q4 (9 GB) or gpt-oss-20b (~15 GB), so CI cannot answer the question. Needs a 16 GB+ machine.
3. Proposed single experiment (needs your approval; ~10 GB + ~12 GB downloads, ~1-2 h each on CPU): on any 16 GB+ Windows PC, run `jarvis bench-intel --model <id> --out out` for Qwen3-4B (re-baseline on v1.1/512), Qwen3-14B and gpt-oss-20b, using the same settings, then compare against section 3. That requires adding the two models as non-recommended catalog candidates and confirming gpt-oss loads, which I have not done.
