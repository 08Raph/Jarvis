"""Since 2026-10-30 the four adult-only styles are no longer blocked by the restriction switch (the owner asked to test them). The 18+ opt-in gate stays.
They remain UNREVIEWED with a real model; nothing here says how any model behaves. FAKE: no model is used."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from jarvis.personality import styles  # noqa: E402
from jarvis.personality.persona import PERSONA_FILE, Persona  # noqa: E402

ADULT = ["savage", "flirty", "sexy", "romantic"]


class Unlocked(AppCase):
    def persona(self):
        pdir = self.d / "p"
        pdir.mkdir(exist_ok=True)
        return Persona(pdir)

    def test_the_shipped_switch_is_off(self):
        self.assertIs(styles.ADULT_MODES_RESTRICTED, False)
        self.assertFalse(self.persona().restricted())

    def test_without_the_adult_opt_in_they_are_still_locked(self):
        p = self.persona()
        for sid in ADULT:
            with self.assertRaises(ValueError):
                p.set_style(sid)
            self.assertEqual(p.settings.style, "normal")

    def test_with_the_adult_opt_in_they_can_be_selected(self):
        p = self.persona()
        p.set_adult_optin(True)
        for sid in ADULT:
            p.set_style(sid)
            self.assertEqual(p.settings.style, sid)

    def test_the_picker_no_longer_shows_the_safety_review_label(self):
        p = self.persona()
        p.set_adult_optin(True)
        rows = {d["id"]: d for d in p.describe()} if hasattr(p, "describe") else {}
        for sid in ADULT:
            if sid in rows:
                self.assertFalse(rows[sid].get("restricted"))
                self.assertTrue(rows[sid]["available"])


if __name__ == "__main__":
    unittest.main()
