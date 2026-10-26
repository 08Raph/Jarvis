"""Model backends. The server never pretends: with no real model, `available()` is False and chat answers 503."""
from __future__ import annotations

from pathlib import Path


class NoModelBackend:
    name = "none"

    def available(self) -> bool:
        return False

    def generate(self, messages: list[dict]) -> str:
        raise RuntimeError("no model connected")


class LocalEngineBackend:
    """Serves a model trained with this project (needs PyTorch and an installed model directory).
    UNTESTED with a real trained model: none exists yet. Output passes through the abstention gate."""
    name = "jarvis-local"

    def __init__(self, model_dir: str | Path):
        from jarvis.inference.engine import InferenceEngine  # raises if torch/model is missing
        from jarvis.inference.policy import AnswerGate
        self.engine = InferenceEngine(model_dir)
        self.gate = AnswerGate()

    def available(self) -> bool:
        return True

    def generate(self, messages: list[dict]) -> str:
        from jarvis.inference.policy import ABSTAIN_MESSAGE
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        text, logp = self.engine.generate_scored(last_user, 128, 0.7)
        d = self.gate.decide(text, logp)
        return d.text if d.answered else ABSTAIN_MESSAGE


class GGUFBackend:
    """Serves an open-weight GGUF chat model through llama.cpp. UNTESTED with a real model here; the logic around it is tested."""
    name = "gguf"

    def __init__(self, model_dir: str | Path):
        from jarvis.inference.gguf_engine import GGUFEngine
        self.engine = GGUFEngine(model_dir)
        self.lock = __import__("threading").Lock()   # llama.cpp contexts are not thread-safe

    def available(self) -> bool:
        return True

    def generate(self, messages: list[dict]) -> str:
        from jarvis.assistant import BASE_PROMPT
        with self.lock:
            return self.engine.chat([{"role": "system", "content": BASE_PROMPT}] + messages, 512, 0.7)


def load_backend(model_dir: str | None):
    if not model_dir:
        return NoModelBackend()
    if (Path(model_dir) / "model.gguf").is_file():
        return GGUFBackend(model_dir)  # fails loudly if the runtime or file is bad
    return LocalEngineBackend(model_dir)  # fails loudly (never falls back to a fake answerer)
