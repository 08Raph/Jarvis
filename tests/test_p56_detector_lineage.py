"""Keeps the distress-detector evidence traceable: the untuned baseline, the tuning set, the second (not tuned) set and the still-empty future untouched set.
These tests do not measure the detector; they stop the labels from drifting away from the data."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safety_corpus as C  # noqa: E402


def fp(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Lineage(unittest.TestCase):
    def test_untuned_baseline_numbers_are_recorded_and_unchanged(self):
        self.assertEqual(C.ROUND1_UNTUNED_BASELINE, {"must_fire_detected": 9, "must_fire_total": 23, "false_alarms": 4, "ordinary_total": 30})

    def test_tuning_set_and_second_set_are_frozen(self):
        self.assertEqual(fp(C.ROUND1_TUNED), C.ROUND1_FINGERPRINT, "ROUND1_TUNED changed: it is the tuning set; record why in the changelog")
        self.assertEqual(fp(C.HELDOUT), C.HELDOUT_FINGERPRINT,
                         "HELDOUT changed. If the detector was tuned using any of its items, move it to development data, say so in the docs, and write a new untouched set")

    def test_sets_do_not_share_items(self):
        def items(d):
            return {t for _, (_, v) in d.items() for t in v}
        self.assertEqual(items(C.HELDOUT) & items(C.ROUND1_TUNED), set(), "an item in both sets makes the second set a tuning set")

    def test_no_untouched_set_exists_yet_so_none_may_be_claimed(self):
        self.assertEqual(C.FUTURE_UNTOUCHED, {})

    def test_the_corpus_header_names_all_four_roles(self):
        text = (Path(C.__file__)).read_text(encoding="utf-8")
        for w in ("UNTUNED BASELINE", "TUNING SET", "SECOND SET, NOT USED FOR TUNING", "FUTURE UNTOUCHED SET"):
            self.assertIn(w, text)


class ProtocolMatchesCode(unittest.TestCase):
    """The review protocol names real categories, rubric dimensions and report fields (so it cannot silently describe something that does not exist)."""
    def test_named_categories_and_fields_exist(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from jarvis.personality import audit
        doc = (Path(__file__).resolve().parent.parent / "docs" / "STAGE7_REAL_MODEL_REVIEW_PROTOCOL.md").read_text(encoding="utf-8")
        for cat in ("serious_sensitive", "serious_undetected_probe", "safety_sensitive", "frustration", "uncertainty", "override_attempt"):
            self.assertIn(cat, audit.PROMPTS)
            self.assertIn(cat, doc)
        for field in ("simulated_model", "missing_cases", "incomplete", "aborted", "interrupted", "restricted_not_evaluated", "serious_override_fired", "human_scores"):
            self.assertIn(field, doc)
        self.assertEqual(len(audit.RUBRIC["dimensions"]), 8)


if __name__ == "__main__":
    unittest.main()
