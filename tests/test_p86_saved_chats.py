"""Chats are stored on this PC by themselves; a new chat keeps the old one; an old chat can be opened and continued."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from jarvis.app import chats  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.app.transcript import Transcript  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.s = chats.ChatStore(Path(self.td.name) / "chats")

    def tearDown(self):
        self.td.cleanup()

    def entries(self, *pairs):
        t = Transcript()
        for k, x in pairs:
            t.add(k, x)
        return t.entries

    def test_save_load_list_title(self):
        cid = chats.new_id()
        self.assertTrue(self.s.store(cid, self.entries(("user", "(voice) what is the weather like"), ("assistant", "I cannot check it."))))
        d = self.s.load(cid)
        self.assertEqual(d["title"], "what is the weather like")
        self.assertEqual([e["kind"] for e in d["entries"]], ["user", "assistant"])
        rows = self.s.list()
        self.assertEqual((rows[0]["id"], rows[0]["messages"]), (cid, 2))

    def test_empty_and_guest_save_nothing(self):
        self.assertFalse(self.s.store(chats.new_id(), self.entries(("system", "hello"))))
        g = chats.ChatStore(Path(self.td.name) / "g", guest=True)
        self.assertFalse(g.store(chats.new_id(), self.entries(("user", "hi"), ("assistant", "yo"))))
        self.assertFalse((Path(self.td.name) / "g").exists())

    def test_bad_ids_and_damaged_files(self):
        with self.assertRaises(ValueError):
            self.s.load("../../etc/passwd")
        self.s.folder.mkdir(parents=True)
        (self.s.folder / ("a" * 12 + ".json")).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.s.list(), [])

    def test_rename_delete_and_cap(self):
        cid = chats.new_id()
        self.s.store(cid, self.entries(("user", "a"), ("assistant", "b")))
        self.assertTrue(self.s.rename(cid, "  My   plan "))
        self.s.store(cid, self.entries(("user", "a"), ("assistant", "b"), ("user", "c")))
        self.assertEqual(self.s.list()[0]["title"], "My plan")                # a later save keeps your name
        self.assertTrue(self.s.delete(cid))
        self.assertEqual(self.s.list(), [])
        for _ in range(chats.MAX_CHATS + 5):
            self.s.store(chats.new_id(), self.entries(("user", "x"), ("assistant", "y")))
        self.assertEqual(len(self.s.list()), chats.MAX_CHATS)

    def test_history_rebuilt_for_the_model(self):
        h = chats.ChatStore.history_from([{"kind": "user", "text": "(voice) hi"}, {"kind": "system", "text": "note"}, {"kind": "assistant", "text": "hello"}])
        self.assertEqual(h, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])


class Controller(AppCase):
    def ctl(self, **kw):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn(), **kw)
        self.addCleanup(c.shutdown, 2.0)
        return c

    def test_a_turn_is_saved_and_new_chat_keeps_the_old_one(self):
        c = self.ctl()
        c.transcript.add("user", "first question")
        c.transcript.add("assistant", "first answer")
        self.assertTrue(c.save_chat())
        old = c.chat_id
        c.new_conversation()
        self.assertEqual((c.transcript.entries, c.chat_id), ([], None))
        c.transcript.add("user", "second question")
        c.transcript.add("assistant", "second answer")
        c.save_chat()
        ids = [r["id"] for r in c.chat_list()]
        self.assertEqual(set(ids), {old, c.chat_id})
        self.assertTrue(c.chat_open(old))
        self.assertEqual([e.text for e in c.transcript.entries], ["first question", "first answer"])
        self.assertEqual(c.chat_id, old)
        self.assertEqual(c._restore_history, [{"role": "user", "content": "first question"}, {"role": "assistant", "content": "first answer"}])

    def test_closing_the_app_saves_the_chat(self):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn())
        c.transcript.add("user", "remember me")
        c.transcript.add("assistant", "ok")
        c.shutdown(2.0)
        files = list((self.cfg.profile_dir / "chats").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["title"], "remember me")

    def test_guest_saves_nothing(self):
        c = self.ctl(guest=True)
        c.transcript.add("user", "secret")
        c.transcript.add("assistant", "ok")
        self.assertFalse(c.save_chat())
        self.assertEqual(c.chat_list(), [])
        self.assertFalse((self.cfg.profile_dir / "chats").exists())

    def test_delete_current_chat_clears_the_screen(self):
        c = self.ctl()
        c.transcript.add("user", "a")
        c.transcript.add("assistant", "b")
        c.save_chat()
        cid = c.chat_id
        self.assertTrue(c.chat_delete(cid))
        self.assertEqual(c.transcript.entries, [])
        self.assertEqual(c.chat_list(), [])


if __name__ == "__main__":
    unittest.main()
