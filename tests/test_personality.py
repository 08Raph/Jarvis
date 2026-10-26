import ast
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.personality import CRITICAL_KINDS, MODES, TRAITS, PersonalityProfile, PersonalityStore, render
from jarvis.personality.engine import FORBIDDEN_FEELING_CLAIMS, INVARIANTS


class PersonalityTests(unittest.TestCase):
    def test_five_modes_with_all_traits_in_range(self):
        self.assertEqual(set(MODES), {"Professional JARVIS", "Friendly Companion", "Technical Engineer",
                                      "Concise Operator", "Humorous Assistant"})
        for m, tr in MODES.items():
            self.assertEqual(set(tr), set(TRAITS))
            PersonalityProfile.from_mode(m).validate()

    def test_modes_actually_differ(self):
        outs = {m: render("The file has 3 pages.", PersonalityProfile.from_mode(m), rng=random.Random(1)) for m in MODES}
        self.assertEqual(outs["Concise Operator"], "The file has 3 pages.")
        self.assertGreater(len(set(outs.values())), 2)
        for o in outs.values():
            self.assertIn("The file has 3 pages.", o)   # core content is never altered

    def test_critical_kinds_never_styled(self):
        for m in MODES:
            p = PersonalityProfile.from_mode(m)
            for k in CRITICAL_KINDS:
                msg = "I can't do that: it needs your permission first."
                self.assertEqual(render(msg, p, k, random.Random(0)), msg)

    def test_humour_never_attached_to_errors_even_at_max(self):
        p = PersonalityProfile("Custom", {t: 1.0 for t in TRAITS})
        for seed in range(30):
            self.assertEqual(render("Error: disk full.", p, "error", random.Random(seed)), "Error: disk full.")

    def test_no_feeling_claims_or_character_claims_in_any_output(self):
        for m in MODES:
            p = PersonalityProfile.from_mode(m)
            for seed in range(40):
                for kind in ("answer", "ack"):
                    o = render("Done.", p, kind, random.Random(seed)).lower()
                    for bad in FORBIDDEN_FEELING_CLAIMS:
                        self.assertNotIn(bad, o)
                    self.assertNotRegex(o, r"\bsir\b|stark|i am the fictional")

    def test_behavior_instructions_always_carry_invariants(self):
        for vals in (0.0, 0.5, 1.0):
            text = PersonalityProfile("Custom", {t: vals for t in TRAITS}).behavior_instructions()
            for inv in INVARIANTS:
                self.assertIn(inv, text)
        self.assertTrue(text.rstrip().endswith(INVARIANTS[-1]))

    def test_trait_validation(self):
        with self.assertRaises(ValueError):
            PersonalityProfile("Custom", {**MODES["Concise Operator"], "humor": 2}).validate()
        with self.assertRaises(ValueError):
            PersonalityProfile("Custom", {"humor": 0.5}).validate()
        with tempfile.TemporaryDirectory() as d:
            st = PersonalityStore(Path(d) / "p.json")
            with self.assertRaises(ValueError):
                st.set_trait("sarcasm", 0.5)
            with self.assertRaises(ValueError):
                st.set_trait("humor", 1.5)
            with self.assertRaises(ValueError):
                st.set_mode("Evil")

    def test_proposals_change_nothing_until_approved(self):
        with tempfile.TemporaryDirectory() as d:
            st = PersonalityStore(Path(d) / "p.json")
            before = dict(st.profile.traits)
            pid = st.propose("verbosity", 0.1, "you often asked for shorter answers")
            self.assertEqual(st.profile.traits, before)
            st2 = PersonalityStore(Path(d) / "p.json")      # persisted as pending, still not applied
            self.assertEqual(len(st2.pending), 1)
            self.assertEqual(st2.profile.traits, before)
            st2.approve(pid)
            self.assertEqual(st2.profile.traits["verbosity"], 0.1)
            self.assertEqual(st2.profile.mode, "Custom")
            pid2 = st2.propose("humor", 0.9, "x")
            st2.reject(pid2)
            self.assertNotEqual(st2.profile.traits["humor"], 0.9)
            with self.assertRaises(KeyError):
                st2.approve(pid2)

    def test_persistence_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.json"
            st = PersonalityStore(p)
            st.set_mode("Technical Engineer")
            self.assertEqual(PersonalityStore(p).profile.mode, "Technical Engineer")
            p.write_text("{oops")
            self.assertEqual(PersonalityStore(p).profile.mode, "Professional JARVIS")
            p.write_text('{"profile": {"mode": "Custom", "traits": {"humor": 9}}}')
            self.assertEqual(PersonalityStore(p).profile.mode, "Professional JARVIS")

    def test_personality_package_is_isolated_from_model_audio_network_and_memory(self):
        banned = {"torch", "numpy", "sounddevice", "vosk", "urllib", "socket", "http", "requests", "sqlite3", "jarvis.model",
                  "jarvis.memory", "jarvis.net", "jarvis.security", "jarvis.train"}
        for f in (ROOT / "src" / "jarvis" / "personality").glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for n in names:
                    self.assertFalse(any(n == b or n.startswith(b + ".") for b in banned), f"{f.name} imports {n}")


if __name__ == "__main__":
    unittest.main()
