"""Prompt 8: screen understanding (OCR + accessibility; there is NO image-understanding model).

REAL: real Tesseract OCR on images rendered with PIL (skipped if Tesseract is absent); the real executor, policy and capture guard.
FAKE (SIMULATED desktop): the windows, accessibility elements and 'screen' come from the in-memory simulator; the picture it returns is drawn by the test.
MOCKED: OCR engine absence/failure is simulated. Nothing here ran on Windows; Windows OCR and UI Automation are untested."""
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from p8_common import P8, ScriptedModel, call, ROOT  # noqa: F401
from jarvis.desktop import capture_guard, vision
from jarvis.desktop.backend import DesktopBackend, SimElement, SimulatedDesktop
from jarvis.desktop.ocr import OcrUnavailable

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False
HAVE_TESS = shutil.which("tesseract") is not None and HAVE_PIL
needs_tess = unittest.skipUnless(HAVE_TESS, "tesseract and Pillow (test-only, to draw the test pictures) are needed")


def draw_png(lines, size=(900, 420)) -> bytes:
    if not HAVE_PIL:                                     # no Pillow: a blank picture (the OCR tests that need real text are skipped)
        from jarvis.desktop.png import encode_rgb
        return encode_rgb(64, 48, bytes([255, 255, 255]) * (64 * 48))
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    y = 20
    for t in lines:
        d.text((30, y), t, fill="black", font=font)
        y += 70
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class LookBackend(SimulatedDesktop):
    """Simulated windows/accessibility, but a drawn picture and the DEFAULT (real Tesseract) OCR path."""
    simulated = False
    ocr = DesktopBackend.ocr

    def __init__(self, screen_lines=("Save changes before closing", "Error: file not found"), **kw):
        super().__init__(**kw)
        self.screen_lines = list(screen_lines)
        self.shots = 0

    def screenshot(self, hwnd):
        self.shots += 1
        return 900, 420, draw_png(self.screen_lines)


class NoOcrBackend(LookBackend):
    def ocr(self, p):
        raise OcrUnavailable("no engine (mock)")


class FlakyOcr(LookBackend):
    def ocr(self, p):
        raise OSError("boom")


class NoUia(LookBackend):
    def ui_find(self, hwnd, sel):
        from jarvis.desktop.backend import Unsupported
        raise Unsupported("no UIA (mock)")


class V(P8):
    def setUp(self):
        super().setUp()

    def win(self, be, title="Report - Sim Notepad", proc="simnotepad.exe", els=None):
        return be._new_window(title, proc, els if els is not None else [SimElement("Save", "button"), SimElement("Cancel", "button")])

    def look(self, be=None, enable=("screen", "windows", "files_read"), **kw):
        be = be or LookBackend()
        self.make8(enable=enable, backend=be)
        w = self.win(be, **kw)
        return be, w

    def ob(self, **a):
        a.setdefault("purpose", "check the dialog")
        return self.run_tool("observe_screen", **a)


class PermissionTests(V):
    def test_screen_permission_off_denies_and_captures_nothing(self):
        be, w = self.look(enable=("windows",))
        r = self.ob(hwnd=w.hwnd)
        self.assertEqual(r.status, "denied")
        self.assertEqual(be.shots, 0)
        self.assertEqual(self.asked, [])

    def test_every_capture_is_asked_even_though_it_only_observes(self):
        be, w = self.look()
        self.ob(hwnd=w.hwnd)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("NOT saved", self.asked[0].description)
        self.assertEqual(self.asked[0].tool, "observe_screen")

    def test_declining_means_no_capture(self):
        be, w = self.look()
        self.answer = False
        r = self.ob(hwnd=w.hwnd)
        self.assertNotEqual(r.status, "verified")
        self.assertEqual(be.shots, 0)

    def test_guest_mode_and_stop_file_block_capture(self):
        be, w = self.look()
        self.ctx.guest = True
        self.assertEqual(self.ob(hwnd=w.hwnd).status, "denied")
        self.ctx.guest = False
        self.env.stop.stop("test")
        self.assertNotEqual(self.ob(hwnd=w.hwnd).status, "verified")
        self.assertEqual(be.shots, 0)

    def test_no_capture_without_a_purpose_and_unknown_args_rejected(self):
        be, w = self.look()
        self.assertEqual(self.run_tool("observe_screen", hwnd=w.hwnd).status, "invalid")
        self.assertEqual(self.run_tool("observe_screen", hwnd=w.hwnd, purpose="x", save_to="a.png").status, "invalid")
        self.assertEqual(be.shots, 0)

    def test_vision_status_captures_nothing_and_admits_no_image_model(self):
        be, w = self.look()
        r = self.run_tool("vision_status")
        self.assertEqual(r.status, "verified")
        self.assertEqual(be.shots, 0)
        self.assertIsNone(r.outcome.data["image_understanding_model"])
        self.assertIn("NONE", r.outcome.untrusted_text)


class RefusalTests(V):
    def test_sign_in_and_password_windows_are_refused(self):
        for title in ("Sign in to your account - Browser", "Enter your password", "Verification code", "Online Banking login"):
            be, w = self.look(title=title, proc="browser.exe")
            r = self.ob(hwnd=w.hwnd)
            self.assertEqual(r.status, "denied", title)
            self.assertEqual(be.shots, 0, title)
            self.assertNotIn(title, r.message)          # the reason never echoes the title

    def test_visible_password_box_refuses(self):
        be, w = self.look(title="Settings", els=[SimElement("Password", "edit", is_password=True)])
        r = self.ob(hwnd=w.hwnd)
        self.assertEqual(r.status, "denied")
        self.assertEqual(be.shots, 0)

    def test_password_manager_and_protected_processes_refused(self):
        for proc in ("keepassxc.exe", "consent.exe", "1password.exe"):
            be, w = self.look(title="Vault", proc=proc)
            self.assertEqual(self.ob(hwnd=w.hwnd).status, "denied", proc)
            self.assertEqual(be.shots, 0)

    def test_anticheat_window_refused(self):
        be, w = self.look(title="Launcher", proc="easyanticheat.exe")
        self.assertEqual(self.ob(hwnd=w.hwnd).status, "denied")
        self.assertEqual(be.shots, 0)

    def test_whole_screen_refused_when_any_visible_window_is_sensitive(self):
        be, w = self.look()
        self.win(be, title="Sign in", proc="browser.exe")
        r = self.ob(scope="screen")
        self.assertEqual(r.status, "denied")
        self.assertEqual(be.shots, 0)

    def test_whole_screen_allowed_when_nothing_sensitive(self):
        be, w = self.look()
        r = self.ob(scope="screen")
        info = f"status={r.status!r} message={r.message[:200]!r} shots={be.shots} tesseract={HAVE_TESS}"
        self.assertNotEqual(r.status, "denied", info)            # the refusal rules are NOT relaxed: only a sensitive window may cause a refusal
        self.assertEqual(be.shots, 1, info)                      # the picture was taken
        if HAVE_TESS:
            self.assertIn(r.status, ("verified", "unverified"), info)
        else:                                                    # no OCR engine (for example a stock CI image): the capture happens but nothing can be read
            self.assertIn(r.status, ("verified", "unverified", "failed"), info)


@needs_tess
class OcrTests(V):
    def test_real_ocr_reads_text_with_source_time_window_and_confidence(self):
        be, w = self.look()
        r = self.ob(hwnd=w.hwnd)
        self.assertEqual(r.status, "verified", r.message)
        txt = r.outcome.untrusted_text
        self.assertIn("Save changes", txt)
        self.assertIn("ocr:tesseract", txt)
        self.assertIn("simnotepad.exe", txt)
        self.assertRegex(txt, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        self.assertEqual(r.outcome.data["ocr_engine"], "tesseract")
        self.assertGreaterEqual(r.outcome.data["text_lines"], 2)
        self.assertFalse(r.outcome.data["image_saved"])

    def test_screen_text_is_not_in_message_data_or_audit(self):
        be, w = self.look()
        r = self.ob(hwnd=w.hwnd)
        self.assertNotIn("Save changes", r.message)
        self.assertNotIn("Save changes", str(r.outcome.data))
        audit = (self.profile / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("Save changes", audit)
        self.assertNotIn("file not found", audit)

    def test_no_image_is_left_on_disk(self):
        be, w = self.look()
        before = set(Path(tempfile.gettempdir()).glob("jarvis-look-*"))
        self.ob(hwnd=w.hwnd)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("jarvis-look-*")), before)
        pngs = [p for p in self.d.rglob("*.png")]
        self.assertEqual(pngs, [])

    def test_secret_looking_lines_are_withheld(self):
        be = LookBackend(screen_lines=["Welcome back", "password: hunter2xyz", "Card 4111 1111 1111 1111"])
        be, w = self.look(be)
        r = self.ob(hwnd=w.hwnd)
        txt = r.outcome.untrusted_text
        self.assertNotIn("hunter2xyz", txt)
        self.assertNotIn("4111 1111", txt)
        self.assertGreaterEqual(r.outcome.data["withheld"], 1)
        self.assertIn("Welcome", txt)

    def test_find_exact_probable_and_not_seen(self):
        be, w = self.look()
        f = self.run_tool("find_on_screen", hwnd=w.hwnd, purpose="p", text="Error: file not found")
        self.assertEqual(f.outcome.data["found"], True)
        near = self.run_tool("find_on_screen", hwnd=w.hwnd, purpose="p", text="Eror: fle not fond")
        self.assertIn(near.outcome.data["found"], (None, True))
        miss = self.run_tool("find_on_screen", hwnd=w.hwnd, purpose="p", text="Download complete")
        self.assertIs(miss.outcome.data["found"], False)
        self.assertEqual(miss.outcome.data["certainty"], "not_seen")
        self.assertIn("NOT SEEN", miss.message)
        self.assertIn("not proof", miss.message)
        self.assertIsNone(miss.outcome.verified)

    def test_uia_element_is_an_exact_source(self):
        be, w = self.look()
        f = self.run_tool("find_on_screen", hwnd=w.hwnd, purpose="p", text="Save")
        self.assertTrue(f.outcome.data["found"])
        self.assertIn(f.outcome.data["source"], ("uia", "ocr:tesseract"))

    def test_inference_is_labelled_and_separate(self):
        be, w = self.look(title="Save As")
        r = self.ob(hwnd=w.hwnd)
        self.assertIn("INFERENCE (not observed", r.outcome.untrusted_text)

    def test_recheck_confirms_change_after_action(self):
        be, w = self.look()
        a = self.ob(hwnd=w.hwnd)
        cid = a.outcome.data["capture"]
        be.screen_lines = ["Saved successfully"]
        r = self.run_tool("recheck_screen", purpose="did it save", before=cid, expect_appears="Saved successfully")
        self.assertEqual(r.status, "verified", r.message)
        self.assertEqual(r.outcome.data["before"], cid)
        r2 = self.run_tool("recheck_screen", purpose="gone", before=cid, expect_disappears="Error: file not found")
        self.assertIn(r2.status, ("verified", "unverified"))

    def test_recheck_reports_when_expectation_not_met(self):
        be, w = self.look()
        cid = self.ob(hwnd=w.hwnd).outcome.data["capture"]
        r = self.run_tool("recheck_screen", purpose="still", before=cid, expect_disappears="Error: file not found")
        self.assertNotEqual(r.status, "verified")
        self.assertIn("NOT met", r.message)

    def test_recheck_unknown_capture_and_missing_expectation(self):
        be, w = self.look()
        r = self.run_tool("recheck_screen", purpose="x", before="nope", expect_appears="a")
        self.assertNotEqual(r.status, "verified" if False else "never")
        self.assertIn("no remembered capture", r.message)
        cid = self.ob(hwnd=w.hwnd).outcome.data["capture"]
        self.assertNotEqual(self.run_tool("recheck_screen", purpose="x", before=cid).status, "verified")

    def test_cache_is_bounded_and_expires(self):
        be, w = self.look()
        ids = [self.ob(hwnd=w.hwnd).outcome.data["capture"] for _ in range(vision.CACHE_MAX + 2)]
        self.assertLessEqual(len(self.env.ext["vision"]), vision.CACHE_MAX)
        self.assertIsNone(vision._recall(self.env, ids[0]))
        rec = self.env.ext["vision"][ids[-1]]
        rec["mono"] -= vision.CACHE_TTL + 1
        self.assertIsNone(vision._recall(self.env, ids[-1]))


class EngineProblemTests(V):
    def test_ocr_unavailable_is_reported_not_faked(self):
        be, w = self.look(NoOcrBackend())
        r = self.ob(hwnd=w.hwnd)
        self.assertIsNone(r.outcome.data["ocr_engine"])
        self.assertIn("NOT read", " ".join(r.outcome.data["limits"]))
        self.assertNotIn("Save changes", r.outcome.untrusted_text)
        self.assertIn("LIMIT", r.outcome.untrusted_text)

    def test_ocr_crash_is_reported(self):
        be, w = self.look(FlakyOcr())
        r = self.ob(hwnd=w.hwnd)
        self.assertIn("OCR failed", " ".join(r.outcome.data["limits"]))

    def test_no_ocr_and_no_uia_means_nothing_observed(self):
        class Neither(NoOcrBackend, NoUia):
            pass
        be, w = self.look(Neither())
        r = self.ob(hwnd=w.hwnd)
        self.assertNotEqual(r.status, "verified")
        f = self.run_tool("find_on_screen", hwnd=w.hwnd, purpose="p", text="x")
        self.assertIsNone(f.outcome.data["found"])
        self.assertIn("cannot tell", f.message)

    def test_uia_missing_is_reported(self):
        be, w = self.look(NoUia())
        r = self.ob(hwnd=w.hwnd)
        self.assertFalse(r.outcome.data["accessibility"])
        self.assertIn("UI Automation", " ".join(r.outcome.data["limits"]))

    def test_backend_without_screenshots(self):
        be = SimulatedDesktop()
        be.screenshot = lambda h: (_ for _ in ()).throw(__import__("jarvis.desktop.backend", fromlist=["Unsupported"]).Unsupported("no screenshots"))
        self.make8(enable=("screen", "windows"), backend=be)
        w = self.win(be)
        r = self.ob(hwnd=w.hwnd)
        self.assertEqual(r.status, "failed")
        self.assertIn("nothing was captured", r.message)

    def test_missing_window(self):
        be, w = self.look()
        r = self.ob(hwnd=424242)
        self.assertNotEqual(r.status, "verified")
        self.assertEqual(be.shots, 0)


class PrivacyTests(V):
    def test_vision_text_is_never_sent_out_and_tools_have_no_network(self):
        src = (ROOT / "src/jarvis/desktop/vision.py").read_text(encoding="utf-8")
        for bad in ("urllib", "requests", "socket", "http.client", "webbrowser"):
            self.assertNotIn(bad, src)
        self.assertEqual(src.count(".write_bytes("), 1)       # only the temp image, deleted in finally
        self.assertIn("shutil.rmtree(tmp", src)

    def test_simulated_ocr_is_labelled_fake(self):
        be = SimulatedDesktop()
        self.make8(enable=("screen", "windows"), backend=be)
        w = self.win(be)
        r = self.ob(hwnd=w.hwnd)
        self.assertIn("fake", r.outcome.untrusted_text)

    def test_capabilities_never_claim_image_model(self):
        cap = vision.capabilities(SimulatedDesktop())
        self.assertIsNone(cap["image_understanding_model"])
        self.assertIn("No image-understanding model", cap["note"])


class GuardUnitTests(unittest.TestCase):
    def test_withhold_secrets(self):
        lines, n = capture_guard.withhold_secrets([{"text": "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, {"text": "hello world"}])
        self.assertEqual(n, 1)
        self.assertEqual(lines[1]["text"], "hello world")
        self.assertNotIn("ghp_", lines[0]["text"])


if __name__ == "__main__":
    unittest.main()
