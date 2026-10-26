from __future__ import annotations

import json
import random
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

TRAITS = ("formality", "humor", "warmth", "directness", "verbosity", "initiative", "technical_depth")

# name -> trait values 0..1
MODES: dict[str, dict[str, float]] = {
    "Professional JARVIS": dict(formality=0.85, humor=0.2, warmth=0.45, directness=0.7, verbosity=0.5, initiative=0.5, technical_depth=0.6),
    "Friendly Companion": dict(formality=0.3, humor=0.5, warmth=0.9, directness=0.5, verbosity=0.6, initiative=0.6, technical_depth=0.4),
    "Technical Engineer": dict(formality=0.5, humor=0.1, warmth=0.3, directness=0.85, verbosity=0.6, initiative=0.4, technical_depth=0.95),
    "Concise Operator": dict(formality=0.6, humor=0.0, warmth=0.2, directness=0.95, verbosity=0.05, initiative=0.2, technical_depth=0.6),
    "Humorous Assistant": dict(formality=0.35, humor=0.9, warmth=0.7, directness=0.5, verbosity=0.55, initiative=0.5, technical_depth=0.5),
}
DEFAULT_MODE = "Professional JARVIS"

# Message kinds whose wording is never styled: correctness, safety and honesty come first.
CRITICAL_KINDS = frozenset({"error", "refusal", "permission", "security", "privacy", "uncertain"})

INVARIANTS = (
    "Personality is style only. It never overrides privacy, security, permission prompts or truthfulness.",
    "Do not claim human feelings, a body, or experiences. Do not claim to be a fictional character or any real person.",
    "State uncertainty and limits plainly. Never invent facts, tool results or actions.",
)

# British-English-inspired register, written originally (no catchphrases or signature lines of any character,
# no "sir"). Chosen by formality bucket.
_ACK = {
    "formal": ["Certainly.", "Of course.", "Right away.", "Very well."],
    "casual": ["Sure thing.", "Right, on it.", "Happy to help with that.", "Got it."],
}
_CLOSE_OFFER = {
    "formal": ["Shall I go further into any of this?", "Do say if you would like more detail."],
    "casual": ["Want me to dig into any of that?", "Shout if you'd like more detail."],
}
_QUIP = ["That one was a gentle stretch for my circuits.", "Smoother than expected, which is always nice to report.",
         "No drama this time, which I count as a win."]
_WARM = ["Glad that's sorted.", "Pleased to have helped."]  # "glad/pleased" read as courtesy, not a claim of emotion
FORBIDDEN_FEELING_CLAIMS = ("i feel", "i'm feeling", "i am feeling", "i love", "i'm so happy", "i am so happy", "i missed you",
                            "my heart", "i'm excited", "i am excited", "i was lonely", "makes me sad")


@dataclass
class PersonalityProfile:
    mode: str = DEFAULT_MODE
    traits: dict[str, float] = field(default_factory=lambda: dict(MODES[DEFAULT_MODE]))

    @classmethod
    def from_mode(cls, mode: str) -> "PersonalityProfile":
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {list(MODES)}")
        return cls(mode, dict(MODES[mode]))

    def validate(self) -> "PersonalityProfile":
        if set(self.traits) != set(TRAITS):
            raise ValueError(f"traits must be exactly {TRAITS}")
        for k, v in self.traits.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"trait {k} must be a number in 0..1")
        return self

    def behavior_instructions(self) -> str:
        """Text describing the style, followed by the fixed invariants. The invariants come LAST and are not
        derived from traits, so no trait setting can remove them."""
        t = self.traits
        lines = [f"Style profile: {self.mode}."]
        lines.append("Register: " + ("formal and polished" if t["formality"] >= 0.6 else "relaxed and conversational") + ".")
        lines.append("Humour: " + ("light, occasional" if t["humor"] >= 0.6 else "minimal") + ".")
        lines.append("Tone: " + ("warm and encouraging" if t["warmth"] >= 0.6 else "neutral and businesslike") + ".")
        lines.append("Lead with the answer." if t["directness"] >= 0.6 else "Give brief context before the answer.")
        lines.append("Length: " + ("terse" if t["verbosity"] < 0.25 else "moderate" if t["verbosity"] < 0.65 else "thorough") + ".")
        lines.append("Offer next steps." if t["initiative"] >= 0.55 else "Do only what was asked.")
        lines.append("Technical depth: " + ("high, include precise terms" if t["technical_depth"] >= 0.7 else "plain language") + ".")
        return "\n".join(lines + list(INVARIANTS))


def render(text: str, profile: PersonalityProfile, kind: str = "answer", rng: random.Random | None = None) -> str:
    """Wrap a reply in the profile's style. Critical kinds are returned UNCHANGED. The core text is never edited,
    only prefixed/suffixed, so facts and numbers cannot be altered by style."""
    if kind in CRITICAL_KINDS:
        return text
    t = profile.traits
    r = rng or random.Random(zlib.crc32(text.encode("utf-8")))  # deterministic per text unless an rng is supplied
    reg = "formal" if t["formality"] >= 0.6 else "casual"
    pre, post = "", []
    if kind == "ack" or (kind == "answer" and t["verbosity"] >= 0.3 and t["warmth"] >= 0.25):
        if t["verbosity"] >= 0.3:
            pre = r.choice(_ACK[reg]) + " "
    if kind == "answer":
        if t["humor"] >= 0.7 and r.random() < 0.5:
            post.append(r.choice(_QUIP))
        if t["warmth"] >= 0.8 and r.random() < 0.5:
            post.append(r.choice(_WARM))
        if t["initiative"] >= 0.55 and t["verbosity"] >= 0.4:
            post.append(r.choice(_CLOSE_OFFER[reg]))
    if t["verbosity"] < 0.15:      # concise operator: no decoration at all
        return text
    return (pre + text + (" " + " ".join(post) if post else "")).strip()


class PersonalityStore:
    """Persists the active profile and a queue of PROPOSED changes. A proposal changes nothing until the user
    approves it; there is no automatic learning path."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.profile = PersonalityProfile()
        self.pending: list[dict] = []
        self.history: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            prof = PersonalityProfile(raw["profile"]["mode"], dict(raw["profile"]["traits"])).validate()
            if prof.mode not in MODES and prof.mode != "Custom":
                raise ValueError("bad mode")
            self.profile, self.pending, self.history = prof, list(raw.get("pending", [])), list(raw.get("history", []))
        except (KeyError, ValueError, TypeError, OSError):
            self.profile, self.pending, self.history = PersonalityProfile(), [], []  # corrupt -> safe default

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profile": asdict(self.profile), "pending": self.pending, "history": self.history}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set_mode(self, mode: str) -> None:
        self.profile = PersonalityProfile.from_mode(mode)
        self._log("set_mode", mode)
        self.save()

    def set_trait(self, trait: str, value: float) -> None:
        """Explicit user action."""
        if trait not in TRAITS:
            raise ValueError(f"unknown trait {trait!r}; choose from {TRAITS}")
        new = dict(self.profile.traits, **{trait: float(value)})
        PersonalityProfile("Custom", new).validate()
        self.profile = PersonalityProfile("Custom", new)
        self._log("set_trait", f"{trait}={value}")
        self.save()

    def propose(self, trait: str, value: float, reason: str) -> int:
        """Record a SUGGESTED change (e.g. the user seemed to prefer shorter answers). Nothing is applied."""
        if trait not in TRAITS:
            raise ValueError(f"unknown trait {trait!r}")
        PersonalityProfile("Custom", dict(self.profile.traits, **{trait: float(value)})).validate()
        pid = max([p["id"] for p in self.pending] + [h.get("id", 0) for h in self.history if h["action"] == "proposal_resolved"] + [0]) + 1
        self.pending.append({"id": pid, "trait": trait, "value": float(value), "reason": reason})
        self.save()
        return pid

    def approve(self, pid: int) -> None:
        p = self._take(pid)
        self.set_trait(p["trait"], p["value"])
        self._log("proposal_resolved", f"approved {pid}", pid)
        self.save()

    def reject(self, pid: int) -> None:
        self._take(pid)
        self._log("proposal_resolved", f"rejected {pid}", pid)
        self.save()

    def _take(self, pid: int) -> dict:
        for i, p in enumerate(self.pending):
            if p["id"] == pid:
                return self.pending.pop(i)
        raise KeyError(f"no pending proposal {pid}")

    def _log(self, action: str, detail: str, pid: int = 0) -> None:
        self.history.append({"action": action, "detail": detail, "id": pid})
