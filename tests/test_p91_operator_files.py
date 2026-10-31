"""Operator file tools: search, duplicates, organize+undo, safe delete/restore, ZIP safety, backups with verification. Real files in a temp folder."""
import os
import sys
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from operator_fakes import OpCase  # noqa: E402


class Search(OpCase):
    def test_search_filters(self):
        self.write("a.pdf", "x" * 10)
        self.write("b.txt", "needle here")
        big = self.write("big.bin", "x" * 3_000_000)
        old = self.write("old.txt", "old")
        os.utime(old, (time.time() - 400 * 86400,) * 2)
        self.assertIn("a.pdf", self.op.run("files.search", ext="pdf").message)
        self.assertIn("b.txt", self.op.run("files.search", contains="needle").message)
        self.assertIn("big.bin", self.op.run("files.search", min_mb=2).message)
        self.assertIn("old.txt", self.op.run("files.old", days=365).message)
        self.assertNotIn("old.txt", self.op.run("files.recent", days=1).message)

    def test_unnamed_folder_scans_all_allowed_folders(self):
        self.write("one.txt", "1", "Desktop")
        self.write("two.txt", "2", "Documents")
        out = self.op.run("files.large")
        self.assertIn("one.txt", out.message)
        self.assertIn("two.txt", out.message)

    def test_duplicates_are_found_by_content_and_nothing_is_deleted(self):
        a = self.write("a.bin", "same" * 5000)
        b = self.write("copy of a.bin", "same" * 5000)
        self.write("different.bin", "other" * 5000)
        out = self.op.run("files.duplicates", folder="Downloads")
        self.assertEqual(len(out.data), 1)
        self.assertTrue(a.exists() and b.exists())
        self.assertIn("haven't deleted", out.message)

    def test_hash_verify_and_compare(self):
        a = self.write("a.txt", "abc")
        b = self.write("b.txt", "abd")
        h = self.op.run("files.hash", path=str(a)).data
        self.assertEqual(h, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(self.op.run("files.verify_hash", path=str(a), expected=h).status, "verified")
        self.assertEqual(self.op.run("files.verify_hash", path=str(b), expected=h).status, "failed")
        self.assertIn("1 line(s) added", self.op.run("files.compare", a=str(a), b=str(b)).message)


class Organize(OpCase):
    def test_plan_changes_nothing_apply_moves_and_verifies_undo_restores(self):
        self.write("holiday.jpg")
        self.write("Screenshot 1.png")
        self.write("notes.pdf")
        self.write("setup.exe")
        plan = self.op.run("files.organize_plan", folder="Downloads")
        self.assertTrue(plan.ok)
        self.assertTrue((self.dl / "holiday.jpg").exists())
        self.assertFalse((self.dl / "Pictures").exists())
        self.asked.clear()
        done = self.op.run("files.organize_apply", plan=plan.data["plan"])
        self.assertEqual(done.status, "verified", done.message)
        self.assertEqual(self.asked[0][1], "high")                      # moving many files always asks
        self.assertTrue((self.dl / "Pictures" / "holiday.jpg").exists())
        self.assertTrue((self.dl / "Screenshots" / "Screenshot 1.png").exists())
        self.assertTrue((self.dl / "Documents" / "notes.pdf").exists())
        self.assertTrue((self.dl / "Installers" / "setup.exe").exists())
        self.assertFalse((self.dl / "holiday.jpg").exists())
        self.assertEqual(self.op.run("files.organize_undo").status, "verified")
        self.assertTrue((self.dl / "holiday.jpg").exists())

    def test_apply_declined_moves_nothing(self):
        self.write("a.jpg")
        plan = self.op.run("files.organize_plan", folder="Downloads")
        self.answer = False
        self.assertEqual(self.op.run("files.organize_apply", plan=plan.data["plan"]).status, "declined")
        self.assertTrue((self.dl / "a.jpg").exists())

    def test_never_overwrites_a_file_that_is_already_there(self):
        self.write("a.jpg", "NEW")
        (self.dl / "Pictures").mkdir()
        (self.dl / "Pictures" / "a.jpg").write_text("OLD")
        plan = self.op.run("files.organize_plan", folder="Downloads")
        self.op.run("files.organize_apply", plan=plan.data["plan"])
        self.assertEqual((self.dl / "Pictures" / "a.jpg").read_text(), "OLD")
        self.assertEqual((self.dl / "Pictures" / "a (2).jpg").read_text(), "NEW")

    def test_a_plan_file_cannot_be_used_to_escape_the_folder(self):
        import json
        self.write("a.jpg")
        plan = self.op.run("files.organize_plan", folder="Downloads")
        f = self.op.folder / "organize_plans" / (plan.data["plan"] + ".json")
        d = json.loads(f.read_text())
        d["moves"][0]["dest"] = str(self.home.parent / "evil" / "a.jpg")
        f.write_text(json.dumps(d))
        out = self.op.run("files.organize_apply", plan=plan.data["plan"])
        self.assertFalse((self.home.parent / "evil").exists())
        self.assertNotEqual(out.status, "verified")

    def test_move_set_by_pattern_into_a_named_folder(self):
        self.write("Screenshot a.png")
        self.write("keep.txt")
        plan = self.op.run("files.organize_plan", folder="Downloads", pattern="screenshot*", into="Screenshots")
        self.op.run("files.organize_apply", plan=plan.data["plan"])
        self.assertTrue((self.dl / "Screenshots" / "Screenshot a.png").exists())
        self.assertTrue((self.dl / "keep.txt").exists())


class Basic(OpCase):
    def test_create_folder_copy_rename_move(self):
        self.assertEqual(self.op.run("files.create_folder", path="School").status, "verified")
        self.assertTrue((self.home / "Desktop" / "School").is_dir() or any(p.name == "School" for r in self.op.roots() for p in r.iterdir()))
        src = self.write("a.txt", "data")
        c = self.op.run("files.copy", src=str(src))
        self.assertEqual(c.status, "verified")
        self.assertTrue((self.dl / "a - copy.txt").exists())
        self.assertEqual(self.op.run("files.rename", path=str(src), new_name="b.txt").status, "verified")
        self.assertEqual(self.op.run("files.move", src=str(self.dl / "b.txt"), dest=str(self.home / "Documents")).status, "verified")
        self.assertTrue((self.home / "Documents" / "b.txt").exists())
        self.assertNotEqual(self.op.run("files.rename", path=str(self.dl / "a - copy.txt"), new_name="..\\evil.txt").status, "verified")

    def test_delete_goes_to_the_recycle_folder_always_asks_and_can_be_restored(self):
        f = self.write("gone.txt", "precious")
        self.answer = False
        self.assertEqual(self.op.run("files.delete", path=str(f)).status, "declined")
        self.assertTrue(f.exists())
        self.answer = True
        out = self.op.run("files.delete", path=str(f))
        self.assertEqual(out.status, "verified")
        self.assertFalse(f.exists())
        self.assertEqual(self.asked[-1][1], "high")
        self.assertEqual(self.op.run("files.restore", name="gone").status, "verified")
        self.assertEqual(f.read_text(), "precious")

    def test_programs_and_scripts_are_not_opened(self):
        p = self.write("run.bat", "echo hi")
        out = self.op.run("files.open", path=str(p))
        self.assertEqual(out.status, "refused")
        self.assertEqual(self.fake.opened, [])

    def test_inventory_and_temp_and_unusual(self):
        self.write("a.tmp")
        self.write("x.exe")
        self.write("report.pdf.exe")
        self.write("n.txt")
        self.assertIn("a.tmp", self.op.run("files.temp").message)
        self.assertIn("x.exe", self.op.run("files.unusual").message)
        inv = self.op.run("files.inventory", folder="Downloads", save=True)
        self.assertEqual(inv.status, "verified")
        self.assertEqual(inv.data["files"], 4)


class Zip(OpCase):
    def test_zip_roundtrip_and_verify(self):
        d = self.dl / "proj"
        d.mkdir()
        (d / "a.txt").write_text("A")
        (d / "sub").mkdir()
        (d / "sub" / "b.txt").write_text("B")
        z = self.op.run("zip.create", sources=[str(d)])
        self.assertEqual(z.status, "verified", z.message)
        self.assertEqual(self.op.run("zip.verify", archive=z.data).status, "verified")
        self.assertIn("a.txt", self.op.run("zip.list", archive=z.data).message)
        (d / "a.txt").unlink()
        out = self.op.run("zip.extract", archive=z.data, dest=str(self.dl / "out"))
        self.assertEqual(out.status, "verified", out.message)
        self.assertEqual((self.dl / "out" / "proj" / "a.txt").read_text(), "A")

    def test_extraction_refuses_path_traversal_and_writes_nothing(self):
        z = self.dl / "evil.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("ok.txt", "ok")
            f.writestr("../../escaped.txt", "bad")
        out = self.op.run("zip.extract", archive=str(z), dest=str(self.dl / "x"))
        self.assertEqual(out.status, "refused")
        self.assertFalse((self.dl.parent / "escaped.txt").exists())
        self.assertFalse((self.dl / "x" / "ok.txt").exists())

    def test_extraction_refuses_absolute_drive_stream_and_link_members(self):
        for name in ("/etc/x", "C:/x.txt", "a/b:evil", "\\\\srv\\share\\x"):
            z = self.dl / "bad.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr(name, "x")
            self.assertEqual(self.op.run("zip.extract", archive=str(z), dest=str(self.dl / "y")).status, "refused", name)
            z.unlink()
        z = self.dl / "link.zip"
        with zipfile.ZipFile(z, "w") as f:
            i = zipfile.ZipInfo("lnk")
            i.external_attr = (0o120777 << 16)
            f.writestr(i, "/etc/passwd")
        self.assertEqual(self.op.run("zip.extract", archive=str(z), dest=str(self.dl / "z")).status, "refused")

    def test_extraction_never_overwrites_an_existing_folder(self):
        d = self.dl / "p"
        d.mkdir()
        (d / "a.txt").write_text("A")
        z = self.op.run("zip.create", sources=[str(d)]).data
        (self.dl / "out").mkdir()
        (self.dl / "out" / "keep.txt").write_text("keep")
        self.op.run("zip.extract", archive=z, dest=str(self.dl / "out"))
        self.assertEqual((self.dl / "out" / "keep.txt").read_text(), "keep")
        self.assertTrue((self.dl / "out (2)" / "p" / "a.txt").exists())

    def test_damaged_archive_is_reported(self):
        z = self.dl / "broken.zip"
        z.write_bytes(b"PK\x03\x04not really a zip")
        self.assertEqual(self.op.run("zip.verify", archive=str(z)).status, "failed")

    def test_compare_archives(self):
        a, b = self.dl / "a.zip", self.dl / "b.zip"
        with zipfile.ZipFile(a, "w") as f:
            f.writestr("x", "1")
            f.writestr("y", "2")
        with zipfile.ZipFile(b, "w") as f:
            f.writestr("x", "1")
            f.writestr("y", "changed")
            f.writestr("z", "3")
        out = self.op.run("zip.compare", a=str(a), b=str(b))
        self.assertEqual(out.data["changed"], ["y"])
        self.assertEqual(out.data["b"], ["z"])


class Backup(OpCase):
    def setUp(self):
        super().setUp()
        self.proj = self.home / "Documents" / "proj"
        self.proj.mkdir()
        (self.proj / "a.txt").write_text("A1")
        (self.proj / "b.txt").write_text("B1")

    def test_backup_is_verified_and_restores_into_a_new_folder(self):
        out = self.op.run("backup.create", source=str(self.proj))
        self.assertEqual(out.status, "verified", out.message)
        self.assertEqual(self.op.run("backup.verify").status, "verified")
        (self.proj / "a.txt").write_text("CHANGED")
        r = self.op.run("backup.restore")
        self.assertEqual(r.status, "verified", r.message)
        self.assertEqual((Path(r.data) / "a.txt").read_text(), "A1")
        self.assertEqual((self.proj / "a.txt").read_text(), "CHANGED")      # the original is untouched

    def test_damage_to_a_backup_is_detected(self):
        out = self.op.run("backup.create", source=str(self.proj))
        victim = next(Path(out.data).rglob("a.txt"))
        victim.write_text("tampered")
        v = self.op.run("backup.verify")
        self.assertEqual(v.status, "failed")
        self.assertIn("damaged", v.message)

    def test_incremental_copies_only_what_changed_and_restores_everything(self):
        self.op.run("backup.create", source=str(self.proj))
        time.sleep(1.1)
        (self.proj / "b.txt").write_text("B2")
        out = self.op.run("backup.create", source=str(self.proj), incremental=True)
        self.assertIn("1 copied", out.message)
        self.assertIn("1 unchanged", out.message)
        r = self.op.run("backup.restore")
        self.assertEqual((Path(r.data) / "a.txt").read_text(), "A1")
        self.assertEqual((Path(r.data) / "b.txt").read_text(), "B2")
        self.assertEqual(self.op.run("backup.verify").status, "verified")

    def test_retention_keeps_the_newest_full_backups(self):
        for i in range(3):
            self.op.run("backup.create", source=str(self.proj), keep=2)
            time.sleep(1.1)
            (self.proj / "a.txt").write_text(f"v{i}")
        sets = self.op.run("backup.list").data
        self.assertEqual(len(sets), 2)

    def test_compare_and_missing(self):
        self.op.run("backup.create", source=str(self.proj))
        time.sleep(1.1)
        (self.proj / "c.txt").write_text("new")
        self.assertIn("c.txt", self.op.run("backup.missing", source=str(self.proj)).message)
        self.op.run("backup.create", source=str(self.proj))
        self.assertIn("1 new", self.op.run("backup.compare").message)
        self.assertEqual(self.op.run("backup.report").status, "verified")

    def test_not_enough_space_is_refused_before_copying(self):
        import shutil
        from unittest import mock
        with mock.patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            out = self.op.run("backup.create", source=str(self.proj))
        self.assertIn("enough space", out.message)


class Storage(OpCase):
    def test_drives_biggest_folders_cleanup_and_growth(self):
        self.write("big/x.bin", "x" * 50000, "Documents")
        self.write("installer.exe", "x" * 100)
        os.utime(self.dl / "installer.exe", (time.time() - 90 * 86400,) * 2)
        self.assertTrue(self.op.run("storage.drives").ok)
        b = self.op.run("storage.biggest_folders", folder="Documents")
        self.assertIn("big", b.message)
        c = self.op.run("storage.cleanup_candidates", folder="Downloads")
        self.assertIn("installer", c.message)
        self.assertTrue((self.dl / "installer.exe").exists())
        self.op.run("storage.report")
        self.assertTrue(self.op.run("storage.report").ok)

    def test_drive_health_comes_from_windows_and_is_reported_honestly(self):
        self.fake.ps_answers = [("Get-PhysicalDisk", '{"FriendlyName":"Disk0","HealthStatus":"Warning","OperationalStatus":"OK"}')]
        out = self.op.run("storage.health")
        self.assertIn("not reported healthy", out.message)


if __name__ == "__main__":
    unittest.main()
