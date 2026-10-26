"""Generate a model card from MEASURED artifacts only. Missing data is stated as such."""
from __future__ import annotations

import json
from pathlib import Path

NM = "NOT MEASURED"


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def generate_model_card(model_dir: str | Path, data_dir: str | Path,
                        eval_results: str | Path | None = None, name: str = "JARVIS-LM") -> str:
    md, dd = Path(model_dir), Path(data_dir)
    run = _load(md / "run_config.json")
    met = _load(md / "metrics.json")
    ds = _load(dd / "dataset_report.json")
    ev = _load(Path(eval_results)) if eval_results else None

    L = [f"# Model card: {name}", ""]
    lead = ("Small decoder-only transformer, randomly initialized and trained from scratch with this repository's code. "
            if met else
            "No trained model was found, so this card describes the intended design only: a small decoder-only "
            "transformer, randomly initialized and trained from scratch with this repository's code. ")
    L += ["## Overview",
          lead + "It is NOT a fine-tune of any third-party model and does not call any hosted model.",
          ""]
    if run:
        m = run["model_config"]
        L.append(f"Architecture: {m['n_layer']} layers, {m['n_head']} heads, width {m['n_embd']}, "
                 f"context {m['block_size']}, vocab {m['vocab_size']}"
                 + (f", {met['params']:,} parameters." if met else "."))
        L.append(f"Seed {run['seed']}; torch {run['torch']}; numpy {run['numpy']}; device {run['device']}; "
                 f"platform {run['platform']}.")
    else:
        L.append(f"Architecture and run configuration: {NM} (no run_config.json found).")
    L += ["", "## Intended use",
          "Local experimentation with a small language model inside the JARVIS desktop app; studying a "
          "reproducible training pipeline. Not for medical, legal, financial, safety-critical, or "
          "factual-lookup use.", "",
          "## Training data"]
    if ds:
        lic = sorted({s["license"] for s in ds["sources"]})
        L.append(f"{len(ds['sources'])} source file(s); licenses: {', '.join(lic)}.")
        L.append("Pipeline stages (documents): " + ", ".join(f"{k}={v}" for k, v in ds["stages"].items()) + ".")
        L.append("Splits: " + ", ".join(f"{k}={v['documents']} docs/{v['chars']} chars" for k, v in ds["splits"].items())
                 + f". Dropped from train for n-gram overlap with val/test: {ds['decontamination_dropped_from_train']}.")
        for w in ds.get("warnings", []):
            L.append(f"WARNING: {w}")
    else:
        L.append(f"Dataset summary: {NM} (no dataset_report.json found).")
    L += ["", "## Training procedure and compute"]
    if met:
        L.append(f"Steps completed {met['steps_completed']} (early stop: {met['stopped_early']}); best step {met['best_step']}; "
                 f"tokens train/val/test {met['tokens']['train']}/{met['tokens']['val']}/{met['tokens']['test']}.")
        if met["overfit_warning_steps"]:
            L.append(f"Overfitting warnings were raised at steps {met['overfit_warning_steps']}.")
        if met["history"]:
            L.append(f"Wall-clock to last eval: {met['history'][-1]['elapsed_s']} s on {met['device']}.")
    else:
        L.append(f"Training metrics and hardware used: {NM}.")
    L += ["", "## Evaluation"]
    if met and met.get("test_loss") is not None:
        L.append(f"Held-out test loss {met['test_loss']:.4f} (perplexity {met['test_perplexity']:.2f}), "
                 f"best validation loss {met['best_val_loss']:.4f}. Test split evaluated once, after model selection.")
    else:
        L.append(f"Language-model test loss/perplexity: {NM}.")
    if ev:
        L.append(f"Benchmark conditions: {json.dumps(ev['conditions'])}")
        for k, v in ev["categories"].items():
            L.append(f"- {k}: " + (f"{v['correct']}/{v['n']}" if v["status"] == "measured" else f"{NM} ({v['reason']})"))
        L.append("Benchmarks are smoke-level (a handful of items per category) and not statistically meaningful.")
    else:
        L.append(f"Task benchmarks: {NM}.")
    L += ["", "## Limitations",
          "Small models trained on limited data produce fluent-looking but frequently wrong text and are nowhere "
          "near frontier systems. Confidence gating by token probability is an imperfect proxy and must be "
          "calibrated before it is trusted. Rule-based benchmark scorers can mis-score answers.",
          "", "## Known biases",
          "Inherited from the training corpus; no bias audit has been run unless listed above.",
          "", "## Distribution terms",
          "Code: MIT. Weights: trained on data under the licenses listed above; the most restrictive license "
          "(for example attribution or share-alike obligations) may apply to the weights. Verify before "
          "redistributing; this is not legal advice."]
    return "\n".join(L) + "\n"


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--eval-results")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    Path(a.out).write_text(generate_model_card(a.model_dir, a.data_dir, a.eval_results), encoding="utf-8")


if __name__ == "__main__":
    main()
