"""Personality page wiring against STAND-IN Qt classes (FAKE QT). PySide6 is not installed here, so this runs `jarvis.ui.window` against recording stand-ins for
QSpinBox / QCheckBox / QComboBox and a REAL AppController(simulate=True). It checks the page code's wiring and the values it would show; it CANNOT show that
anything draws, is clickable, is readable or looks right. Real-Qt and Windows verification are listed in docs/STAGE7_AUDIT.md as NOT done."""
import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from test_p34_readiness import _Anything, _fake_qt  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402,F401  (imported BEFORE the stand-in Qt patch so numpy is not loaded twice)
import jarvis.voice.kokoro_engine  # noqa: E402,F401


class Spin(_Anything):
    def __init__(self, *a, **k):
        self._v, self._range, self.handlers = 0, (0, 99), []
        self.editingFinished = mock.MagicMock()
        self.editingFinished.connect = lambda f: self.handlers.append(f)

    def setRange(self, lo, hi):
        self._range = (lo, hi)

    def setValue(self, v):
        self._v = max(self._range[0], min(self._range[1], v))

    def value(self):
        return self._v


class Check(_Anything):
    def __init__(self, *a, **k):
        self._c = False

    def setChecked(self, c):
        self._c = bool(c)

    def isChecked(self):
        return self._c


class Combo(_Anything):
    def __init__(self, *a, **k):
        self.items, self.cur = [], -1

    def clear(self):
        self.items, self.cur = [], -1

    def addItem(self, text, data=None):
        self.items.append((text, data))

    def count(self):
        return len(self.items)

    def setCurrentIndex(self, i):
        self.cur = i

    def currentData(self):
        return self.items[self.cur][1] if 0 <= self.cur < len(self.items) else None


class PersonalityPage(AppCase):
    def setUp(self):
        super().setUp()
        mods = _fake_qt()
        for n, cls in (("QSpinBox", Spin), ("QCheckBox", Check), ("QComboBox", Combo)):
            setattr(mods["PySide6.QtWidgets"], n, cls)
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)
        sys.modules.pop("jarvis.ui.window", None)
        self.addCleanup(lambda: sys.modules.pop("jarvis.ui.window", None))
        self.W = importlib.import_module("jarvis.ui.window")
        self.ctl = AppController(self.cfg, simulate=True)
        self.addCleanup(self.ctl.shutdown, 1.0)
        self.w = object.__new__(self.W.MainWindow)
        self.w.ctl = self.ctl
        self.w.statusBar = lambda: mock.Mock()
        self.w.error_box = lambda e: self.errors.append(e)
        self.w.refresh = lambda *_: None
        self.errors = []
        self.w._personality_page()
        self.w.speak_box, self.w.speech_rate, self.w.speech_note = Check(), Spin(), mock.MagicMock()
        self.w.speech_rate.setRange(50, 200)

    def test_all_17_modes_listed_and_adult_ones_marked_locked(self):
        self.w._refresh_personality()
        items = self.w.persona_combo.items
        self.assertEqual(len(items), 17)
        locked = {d for t, d in items if "locked" in t}
        self.assertEqual(locked, {"savage", "flirty", "sexy", "romantic"})
        self.assertEqual(self.w.persona_combo.currentData(), "normal")

    def test_five_controls_exist_range_minus2_to_2_and_show_saved_values(self):
        self.assertEqual(set(self.w.persona_ctl_spins), {"humor", "warmth", "directness", "formality", "verbosity"})
        self.assertTrue(all(s._range == (-2, 2) for s in self.w.persona_ctl_spins.values()))
        self.ctl.persona_control("warmth", -2)
        self.ctl.persona_control("verbosity", 2)
        self.w._refresh_personality()
        self.assertEqual((self.w.persona_ctl_spins["warmth"].value(), self.w.persona_ctl_spins["verbosity"].value(), self.w.persona_ctl_spins["humor"].value()), (-2, 2, 0))

    def test_editing_a_spin_saves_through_the_controller_and_only_that_control(self):
        sp = self.w.persona_ctl_spins["formality"]
        sp.setValue(2)
        for h in sp.handlers:
            h()
        st = self.ctl.persona_state()["settings"]
        self.assertEqual((st["formality"], st["humor"], st["warmth"], st["directness"], st["verbosity"]), (2, 0, 0, 0, 0))
        self.assertEqual(self.errors, [])

    def test_limitation_note_is_shown(self):
        self.w._refresh_personality()
        self.assertIn("small local model", self.w.persona_limit.setText.call_args[0][0] if hasattr(self.w.persona_limit.setText, "call_args") else "")

    def test_adult_checkbox_reflects_state_and_unlocks_after_opt_in(self):
        self.w._refresh_personality()
        self.assertFalse(self.w.persona_adult.isChecked())
        self.ctl.persona_adult(True, confirmed=True)
        self.w._refresh_personality()
        self.assertTrue(self.w.persona_adult.isChecked())
        self.assertFalse(any("locked" in t for t, _ in self.w.persona_combo.items))

    def test_apply_style_switches_without_restart(self):
        self.w._refresh_personality()
        self.w.persona_combo.setCurrentIndex([d for _, d in self.w.persona_combo.items].index("companion"))
        self.w._persona_apply()
        self.assertEqual(self.ctl.persona_state()["settings"]["style"], "companion")


if __name__ == "__main__":
    unittest.main()
