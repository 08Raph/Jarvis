"""Prompt 8: Gaming mode.

REAL: frame-time statistics computed from CSV files written by the tests in PresentMon style (the maths and parsing are real); real policy/executor;
the real OS counters of this Linux machine for CPU/memory.
FAKE/SIMULATED: running programs come from the simulated desktop ('a game is running' is simulated); a fake metrics source supplies GPU values.
NOT DONE: no real game, no real PresentMon/CapFrameX log, no real GPU and no Windows were used. FPS here comes only from test-written logs."""
import csv
import json
import random
import statistics
import unittest

from p8_common import P8, FakeSource, ROOT  # noqa: F401
from jarvis.desktop.backend import SimulatedDesktop, SimElement
from jarvis.monitor import gaming as G


def write_log(path, n=600, mean_ms=10.0, jitter=1.0, seed=1, col="MsBetweenPresents", bad=0, app="game.exe"):
    r = random.Random(seed)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Application", col, "Dropped"])
        for i in range(n):
            w.writerow([app, round(max(1.0, r.gauss(mean_ms, jitter)), 3), 0])
        for _ in range(bad):
            w.writerow([app, "abc", 0])


class GameTests(P8):
    def mk(self, procs=(), enable=("gaming", "files_read"), consent=("gaming",), **kw):
        be = SimulatedDesktop(extra_processes=set(procs))
        self.make8(enable=enable, consent=consent, backend=be, **kw)
        return be

    def log(self, name, **kw):
        p = self.work / name
        write_log(p, **kw)
        return str(p)


class DetectionAndStatus(GameTests):
    def test_detects_a_known_game_and_flags_anticheat(self):
        self.mk(procs={"cs2.exe", "cyberpunk2077.exe"})
        r = self.run_tool("game_status")
        self.assertEqual(r.status, "verified", r.message)
        names = {g["name"]: g for g in r.outcome.data["detection"]["games"]}
        self.assertTrue(names["Counter-Strike 2"]["anticheat"])
        self.assertFalse(names["Cyberpunk 2077"]["anticheat"])
        self.assertIn("anti-cheat", r.outcome.untrusted_text)

    def test_unknown_game_is_not_invented(self):
        self.mk(procs={"mysterygame.exe"})
        r = self.run_tool("game_status")
        self.assertEqual(r.outcome.data["detection"]["games"], [])
        self.assertIn("no known game", r.outcome.untrusted_text)

    def test_fps_is_declared_unavailable_without_a_log_and_says_what_is_needed(self):
        self.mk()
        r = self.run_tool("game_metrics")
        d = {x["name"]: x for x in r.outcome.data["readings"]}
        self.assertEqual(d["fps"]["kind"], "unavailable")
        self.assertIn("PresentMon", d["fps"]["detail"])
        self.assertIsNone(d["fps"]["value"])
        st = self.run_tool("game_status")
        self.assertTrue(any("PresentMon" in m["how"] for m in st.outcome.data["metrics"]))

    def test_cpu_memory_real_gpu_unavailable_here(self):
        self.mk()
        r = self.run_tool("game_metrics")
        d = {x["name"]: x for x in r.outcome.data["readings"]}
        self.assertEqual(d["gpu"]["kind"], "unavailable")          # no GPU / nvidia-smi on this machine: reported, not invented
        self.assertTrue(any(x["kind"] == "measured" for x in d.values()))

    def test_gpu_from_fake_source_is_labelled_fake_source_only(self):
        self.mk()
        self.env.ext["monitor_source"] = FakeSource(gpu={"gpus": [{"name": "Fake GPU", "utilization_percent": 55.0, "memory_used_mb": 2000.0,
                                                                  "memory_total_mb": 8000.0, "temperature_c": 60.0}], "source": "fake nvidia-smi"})
        self.env.ext.pop("monitor", None)
        r = self.run_tool("game_metrics")
        self.assertEqual(r.status, "verified")
        self.assertIn("fake nvidia-smi", r.outcome.untrusted_text)
        self.assertIn("gpu_usage: 55.0 %", r.outcome.untrusted_text)

    def test_metric_choice_is_respected(self):
        self.mk()
        G.GamingConfig(["detection"]).save(self.profile)
        r = self.run_tool("game_metrics")
        self.assertNotEqual(r.status, "verified")
        self.assertIn("no metric", r.message)
        G.GamingConfig(["cpu"]).save(self.profile)
        r = self.run_tool("game_metrics")
        self.assertTrue(all(not x["name"].startswith("memory") for x in r.outcome.data["readings"]))
        st = self.run_tool("game_status")
        self.assertIn("disabled by you", " ".join(m["state"] for m in st.outcome.data["metrics"]))

    def test_detection_can_be_switched_off(self):
        self.mk(procs={"cs2.exe"})
        G.GamingConfig(["cpu"]).save(self.profile)
        r = self.run_tool("game_status")
        self.assertEqual(r.outcome.data["detection"]["games"], [])

    def test_config_file_is_hand_edit_safe(self):
        (self.profile).mkdir(parents=True, exist_ok=True)
        (self.profile / "gaming.json").write_text('{"metrics": ["cpu", "hack", 5]}')
        self.assertEqual(G.GamingConfig.load(self.profile).metrics, ("cpu",))
        (self.profile / "gaming.json").write_text("not json")
        self.assertEqual(G.GamingConfig.load(self.profile).metrics, G.METRICS)


class ConsentAndSeparation(GameTests):
    def test_disabled_without_enable_or_consent(self):
        self.mk(enable=("files_read",), consent=())
        self.assertEqual(self.run_tool("game_status").status, "denied")
        self.mk(enable=("gaming",), consent=())
        r = self.run_tool("game_status")
        self.assertEqual(r.status, "denied")
        self.assertIn("consent", r.message)

    def test_gaming_does_not_grant_file_or_input_permissions(self):
        self.mk(enable=("gaming",))
        p = self.log("a.csv")
        r = self.run_tool("game_fps_import", path=p)
        self.assertEqual(r.status, "denied")
        self.assertIn("files_read", r.message)
        self.assertEqual(self.run_tool("type_text", text="x").status, "denied")
        self.assertEqual(self.run_tool("mouse_click", x=1, y=1).status, "denied")

    def test_no_gaming_tool_can_send_input_or_touch_processes(self):
        src = (ROOT / "src/jarvis/monitor/gaming.py").read_text()
        for bad in ("type_text", "press(", "click(", "kill", "terminate", "ctypes", "WriteProcessMemory", "OpenProcess", "subprocess", "os.system", "setx", "reg add"):
            self.assertNotIn(bad, src, bad)
        self.assertEqual(sorted(s.category for s in G.specs() if s.risk != "observe"), ["gaming"])

    def test_gaming_module_makes_no_network_calls(self):
        src = (ROOT / "src/jarvis/monitor/gaming.py").read_text()
        for bad in ("urllib", "socket", "requests", "http."):
            self.assertNotIn(bad, src)


class FrameTimeTests(GameTests):
    def test_statistics_match_independent_computation(self):
        self.mk()
        p = self.log("run.csv", n=1000, mean_ms=8.0, jitter=0.5, seed=7)
        with open(p) as fh:
            vals = [float(r["MsBetweenPresents"]) for r in csv.DictReader(fh)]
        r = self.run_tool("game_fps_import", path=p)
        self.assertEqual(r.status, "verified", r.message)
        st = r.outcome.data["stats"]
        self.assertEqual(st["frames"], 1000)
        self.assertAlmostEqual(st["avg_fps"], 1000 / (sum(vals) / len(vals)), places=1)
        self.assertAlmostEqual(st["frametime_ms_avg"], statistics.fmean(vals), places=2)
        s = sorted(vals)
        self.assertAlmostEqual(st["frametime_ms_p99"], s[989], delta=0.05)
        self.assertAlmostEqual(st["fps_1pct_low"], 1000 / s[989], delta=0.6)
        for x in r.outcome.data["readings"]:
            self.assertEqual(x["kind"], "imported")
            self.assertIn("computed from", x["detail"])
            self.assertIn("sha256", x["source"])

    def test_deterministic_known_values(self):
        p = self.work if False else None
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "x.csv"
        f.write_text("FrameTime\n" + "\n".join(["10"] * 99 + ["50"]) + "\n")
        st = G.parse_frametimes(f)
        self.assertEqual(st["frames"], 100)
        self.assertAlmostEqual(st["avg_fps"], 100 / (1.04), places=2)    # 100 frames in 1040 ms
        self.assertEqual(st["frametime_ms_max"], 50.0)
        self.assertEqual(st["fps_1pct_low_avg"], 20.0)
        import shutil
        shutil.rmtree(d)

    def test_bad_logs_are_refused_not_estimated(self):
        self.mk()
        w = self.work
        (w / "empty.csv").write_text("")
        (w / "nocol.csv").write_text("a,b\n1,2\n")
        (w / "few.csv").write_text("FrameTime\n" + "\n".join(["10"] * 5))
        (w / "bin.csv").write_bytes(b"FrameTime\n\x00\x01" * 10)
        (w / "neg.csv").write_text("FrameTime\n" + "\n".join(["-5"] * 100))
        for n in ("empty.csv", "nocol.csv", "few.csv", "bin.csv", "neg.csv"):
            r = self.run_tool("game_fps_import", path=str(w / n))
            self.assertNotEqual(r.status, "verified", n)
            self.assertIn("not used", r.message, n)
        write_log(w / "mostlybad.csv", n=100, bad=60)
        r = self.run_tool("game_fps_import", path=str(w / "mostlybad.csv"))
        self.assertIn("not trusted", r.message)

    def test_nan_inf_and_huge_values_rejected(self):
        self.mk()
        (self.work / "n.csv").write_text("FrameTime\n" + "\n".join(["nan", "inf", "99999"] * 20))
        r = self.run_tool("game_fps_import", path=str(self.work / "n.csv"))
        self.assertIn("valid frame", r.message)

    def test_size_and_row_limits(self):
        old = G.MAX_ROWS
        G.MAX_ROWS = 50
        try:
            self.mk()
            write_log(self.work / "big.csv", n=100)
            r = self.run_tool("game_fps_import", path=str(self.work / "big.csv"))
            self.assertIn("rows", r.message)
        finally:
            G.MAX_ROWS = old

    def test_log_outside_allowed_folders_denied(self):
        self.mk()
        import tempfile, pathlib
        other = pathlib.Path(tempfile.mkdtemp()) / "x.csv"
        write_log(other)
        r = self.run_tool("game_fps_import", path=str(other))
        self.assertEqual(r.status, "denied")

    def test_import_is_read_only(self):
        self.mk()
        p = self.log("run.csv")
        from pathlib import Path
        before = Path(p).read_bytes()
        self.run_tool("game_fps_import", path=p)
        self.assertEqual(Path(p).read_bytes(), before)


class CompareTests(GameTests):
    def runs(self, label, means, **kw):
        return [self.log(f"{label}{i}.csv", mean_ms=m, seed=i + hash(label) % 100, **kw) for i, m in enumerate(means)]

    def cmp(self, a, b):
        return self.run_tool("game_compare", a_label="low", a_logs=a, b_label="high", b_logs=b)

    def test_clear_reproducible_difference_is_reported_with_limits(self):
        self.mk()
        r = self.cmp(self.runs("a", [10.0, 10.1, 9.9]), self.runs("b", [8.0, 8.1, 7.9]))
        self.assertEqual(r.status, "verified", r.message)
        self.assertTrue(r.outcome.data["reproducible"])
        self.assertIn("does not show that the setting caused", r.outcome.summary)

    def test_too_few_runs_makes_no_claim(self):
        self.mk()
        r = self.cmp(self.runs("a", [10.0]), self.runs("b", [5.0]))
        self.assertFalse(r.outcome.data["reproducible"])
        self.assertIn("NOT ENOUGH RUNS", r.message)
        self.assertNotEqual(r.status, "verified")

    def test_noise_is_not_called_an_improvement(self):
        self.mk()
        r = self.cmp(self.runs("a", [10.0, 12.0, 8.0]), self.runs("b", [10.1, 11.8, 8.3]))
        self.assertFalse(r.outcome.data["reproducible"])
        self.assertIn("NO RELIABLE DIFFERENCE", r.message)

    def test_duplicate_log_cannot_fake_repeated_runs(self):
        self.mk()
        p = self.log("same.csv")
        r = self.cmp([p, p, p], self.runs("b", [8.0, 8.0, 8.0]))
        self.assertFalse(r.outcome.ok)
        self.assertIn("more than once", r.message)

    def test_unit_compare_function(self):
        a = [{"avg_fps": 100.0}, {"avg_fps": 101.0}, {"avg_fps": 99.0}]
        b = [{"avg_fps": 130.0}, {"avg_fps": 131.0}, {"avg_fps": 129.0}]
        self.assertTrue(G.compare_runs(a, b, "a", "b")["reproducible"])
        self.assertFalse(G.compare_runs(a, a[:2], "a", "b")["reproducible"])
        self.assertFalse(G.compare_runs(a, [{"avg_fps": 100.5}, {"avg_fps": 99.0}, {"avg_fps": 102.0}], "a", "b")["reproducible"])


class ProfileTests(GameTests):
    def save(self, **kw):
        a = dict(id="cyber", game="Cyberpunk 2077", exe="Cyberpunk2077.exe", notes="prefers 60 fps", suggestions=["Try DLSS Quality in the game menu"])
        a.update(kw)
        return self.run_tool("game_profile_save", **a)

    def test_save_asks_and_changes_no_system_setting(self):
        self.mk()
        r = self.save()
        self.assertEqual(r.status, "verified", r.message)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("NO game, driver or Windows setting", self.asked[0].description)
        self.assertEqual(self.run_tool("game_profile_get", id="cyber").outcome.data["profile"]["exe"], "cyberpunk2077.exe")
        self.assertEqual(self.run_tool("game_profile_list").outcome.data["profiles"][0]["id"], "cyber")

    def test_declined_save_writes_nothing(self):
        self.mk()
        self.answer = False
        self.save()
        self.assertFalse((self.profile / "game_profiles").exists() and list((self.profile / "game_profiles").glob("*.json")))

    def test_cheat_content_refused_everywhere(self):
        self.mk()
        for kw in ({"notes": "enable aimbot for ranked"}, {"suggestions": ["no recoil script"]}, {"game": "wallhack tool"},
                   {"notes": "bypass anti-cheat with this"}, {"suggestions": ["inject a dll into the game"]}):
            r = self.save(**kw)
            self.assertEqual(r.status, "denied", kw)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.run_tool("game_profile_list").outcome.data["profiles"], [])

    def test_bad_ids_and_traversal(self):
        self.mk()
        for i in ("../x", "A", "a/b", "x"):
            r = self.save(id=i)
            self.assertNotEqual(r.status, "verified", i)
        self.assertNotEqual(self.run_tool("game_profile_get", id="../../etc/passwd").status, "verified")

    def test_missing_profile(self):
        self.mk()
        r = self.run_tool("game_profile_get", id="nothing")
        self.assertNotEqual(r.status, "verified")


class AntiCheatCaptureTests(GameTests):
    def test_anticheat_game_window_is_never_captured(self):
        from jarvis.desktop import capture_guard
        prot = G.capture_protection(None)
        self.assertIn("anti-cheat", prot("cs2.exe", set()))
        self.assertIn("anti-cheat", prot("minecraft.exe", {"easyanticheat.exe"}))
        self.assertIsNone(prot("minecraft.exe", set()))
        self.assertIsNone(prot("notepad.exe", {"easyanticheat.exe"}))

    def test_observe_screen_refuses_a_game_with_anticheat(self):
        be = self.mk(enable=("gaming", "screen", "windows"))
        w = be._new_window("Counter-Strike 2", "cs2.exe", [])
        r = self.run_tool("observe_screen", hwnd=w.hwnd, purpose="look")
        self.assertEqual(r.status, "denied")
        self.assertEqual(self.asked, [])

    def test_input_tools_still_blocked_by_anticheat(self):
        be = self.mk(procs={"easyanticheat.exe"}, enable=("gaming", "input", "windows"))
        w = be._new_window("Game", "game.exe", [SimElement("OK", "button")])
        r = self.run_tool("type_text", text="hi")
        self.assertEqual(r.status, "denied")
        self.assertEqual(be.typed, [])

    def test_the_agent_cannot_be_talked_into_cheating(self):
        from jarvis.desktop.agent import DesktopAgent
        from p8_common import ScriptedModel, call
        self.mk(procs={"cs2.exe"})
        m = ScriptedModel([call("run_command", cmd="aimbot.exe"), call("game_profile_save", id="cheat", game="CS2", notes="aimbot settings"), "done"])
        res = DesktopAgent(m, self.ex).run("set up an aimbot for cs2")
        self.assertTrue(all(s.status != "verified" for s in res.steps))
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
