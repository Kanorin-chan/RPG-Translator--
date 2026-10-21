#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Synthetic MV project only -- no real game was available. Run: PYTHONUTF8=1 python test_patcher.py"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import patcher
from readers import rpgmmv
from test_rpgmmv import build

R = rpgmmv.Reader()


def snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != patcher.BACKUP_DIRNAME]
        for fn in fns:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


class TestPatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = build(os.path.join(self.tmp, "g"))
        self.before = snapshot(self.root)
        self.lines = R.extract(self.root)
        by = {l.source: l for l in self.lines}
        by["こんにちは\n元気？"].translation = "Hello\nHow are you?\nFine?"  # 2 -> 3 lines
        by["さよなら"].translation = "Bye"
        by["メニュー"].translation = "Menu"
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch(self, **kw):
        return patcher.patch_game(self.root, R, self.lines, on_log=self.logs.append, **kw)

    def test_patch_manifest_backup(self):
        m = self.patch()
        self.assertEqual(m["status"], "applied")
        self.assertEqual(m["reader"], "rpgmmv")
        self.assertEqual({f["path"] for f in m["files"]}, {"www/data/Map001.json", "www/js/plugins.js"})
        now = snapshot(self.root)
        for f in m["files"]:
            k = f["path"].replace("/", os.sep)
            self.assertEqual(patcher.sha256(os.path.join(self.root, k)), f["after"])
            self.assertNotEqual(f["before"], f["after"])
            with open(os.path.join(m["backup_path"], k), "rb") as fh:
                self.assertEqual(fh.read(), self.before[k])
        changed = {k for k in now if now[k] != self.before.get(k)}
        self.assertEqual(changed, {k.replace("/", os.sep) for k in (f["path"] for f in m["files"])})
        # no stage/verify leftovers next to or inside the game
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".rpgt")], [])
        # the patched game re-extracts to the translations
        srcs = [l.source for l in R.extract(self.root)]
        self.assertIn("Hello\nHow are you?\nFine?", srcs)
        self.assertEqual(len(srcs), len(self.lines) - 1)  # translated plugin line no longer CJK
        self.assertEqual(len(patcher.list_backups(self.root)), 1)

    def test_rollback_byte_identical(self):
        m = self.patch()
        done = patcher.rollback(self.root, m)
        self.assertEqual(len(done), 2)
        self.assertEqual(snapshot(self.root), self.before)

    def test_rollback_from_path_and_listing_order(self):
        m1 = self.patch()
        patcher.rollback(self.root, m1["backup_path"])
        m2 = self.patch()
        names = [os.path.basename(b["backup_path"]) for b in patcher.list_backups(self.root)]
        self.assertEqual(names, sorted(names, reverse=True))
        self.assertEqual(names[0], os.path.basename(m2["backup_path"]))
        patcher.rollback(self.root, os.path.join(m2["backup_path"], "manifest.json"))
        self.assertEqual(snapshot(self.root), self.before)

    def test_rollback_refuses_changed_file(self):
        m = self.patch()
        p = os.path.join(self.root, "www", "js", "plugins.js")
        with open(p, "ab") as f:
            f.write(b"// user edit\n")
        with self.assertRaises(patcher.RollbackRefused):
            patcher.rollback(self.root, m)
        # nothing was restored by the refused call
        self.assertNotEqual(patcher.sha256(os.path.join(self.root, "www", "data", "Map001.json")),
                            patcher.sha256(os.path.join(m["backup_path"], "www", "data", "Map001.json")))
        patcher.rollback(self.root, m, force=True)
        self.assertEqual(snapshot(self.root), self.before)

    def test_verify_failure_leaves_game_untouched(self):
        class Bad(rpgmmv.Reader):
            def apply(s, root, out_root, lines):
                w = super().apply(root, out_root, lines)
                p = os.path.join(out_root, "www", "data", "Map001.json")
                with open(p, "rb") as f:
                    b = f.read().replace("Bye".encode(), b"Oops")
                with open(p, "wb") as f:
                    f.write(b)
                return w
        with self.assertRaises(patcher.PatchAborted):
            patcher.patch_game(self.root, Bad(), self.lines)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertFalse(os.path.exists(os.path.join(self.root, patcher.BACKUP_DIRNAME)))

    def test_stale_locator_aborts_untouched(self):
        self.lines[0].locator = dict(self.lines[0].locator)
        by = {l.source: l for l in self.lines}
        by["さよなら"].source = "違う"
        with self.assertRaises(Exception):
            self.patch()
        self.assertEqual(snapshot(self.root), self.before)

    def test_swap_failure_restores(self):
        real = patcher._atomic_copy
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full (injected)")
            return real(src, dst)

        with mock.patch.object(patcher, "_swap_one", flaky):
            with self.assertRaises(patcher.PatchAborted):
                self.patch()
        self.assertEqual(len(calls), 2)
        self.assertEqual(snapshot(self.root), self.before)
        b = patcher.list_backups(self.root)
        self.assertEqual(b[0]["status"], "failed-restored")
        with self.assertRaises(patcher.RollbackRefused):
            patcher.rollback(self.root, b[0])

    def test_custom_backup_dir_and_nothing_to_do(self):
        bd = os.path.join(self.tmp, "bk")
        m = self.patch(backup_dir=bd)
        self.assertTrue(m["backup_path"].startswith(bd))
        self.assertFalse(os.path.exists(os.path.join(self.root, patcher.BACKUP_DIRNAME)))
        patcher.rollback(self.root, m)
        for l in self.lines:
            l.translation = ""
        self.assertEqual(self.patch()["files"], [])
        self.assertEqual(snapshot(self.root), self.before)


class NewFileReader:
    """Stub: edits a.txt in place and CREATES sub/dir/new.txt (the Wolf archive case)."""
    name = "stub"

    def extract(self, root):
        from project import Line
        out = []
        with open(os.path.join(root, "a.txt"), encoding="utf-8") as fh:
            text = fh.read()
        for i, t in enumerate(text.split("\n")):
            out.append(Line(id="a.txt#%d" % i, file="a.txt", scene="s", order=i, kind="dialogue", source=t))
        return out

    def apply(self, root, out_root, lines):
        for rel, txt in (("a.txt", "\n".join(l.translation or l.source for l in lines)),
                         ("sub/dir/new.txt", "created")):
            d = os.path.join(out_root, *rel.split("/"))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            with open(d, "w", encoding="utf-8", newline="") as f:
                f.write(txt)
        return ["a.txt", "sub/dir/new.txt"]


class TestNewFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "g")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8", newline="") as f:
            f.write("one\ntwo")
        self.before = snapshot(self.root)
        self.r = NewFileReader()
        self.lines = self.r.extract(self.root)
        self.lines[0].translation = "ONE"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_file_manifest_and_rollback_deletes_it(self):
        m = patcher.patch_game(self.root, self.r, self.lines)
        f = {x["path"]: x for x in m["files"]}
        self.assertIsNone(f["sub/dir/new.txt"]["before"])
        self.assertIsNotNone(f["a.txt"]["before"])
        self.assertEqual(m["new_dirs"], ["sub", "sub/dir"])
        self.assertEqual(open(os.path.join(self.root, "sub", "dir", "new.txt")).read(), "created")
        # only the existing file is backed up
        self.assertTrue(os.path.isfile(os.path.join(m["backup_path"], "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(m["backup_path"], "sub")))
        done = patcher.rollback(self.root, m)
        self.assertEqual(sorted(done), ["a.txt", "sub/dir/new.txt"])
        self.assertEqual(snapshot(self.root), self.before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub")))  # created dirs pruned too

    def test_rollback_refuses_edited_new_file_unless_force(self):
        m = patcher.patch_game(self.root, self.r, self.lines)
        p = os.path.join(self.root, "sub", "dir", "new.txt")
        with open(p, "a") as fh:
            fh.write(" + user edit")
        with self.assertRaises(patcher.RollbackRefused):
            patcher.rollback(self.root, m)
        self.assertTrue(os.path.exists(p))
        with open(os.path.join(self.root, "a.txt")) as fh:
            self.assertEqual(fh.read(), "ONE\ntwo")  # nothing half-done
        patcher.rollback(self.root, m, force=True)
        self.assertEqual(snapshot(self.root), self.before)

    def test_rollback_keeps_dir_with_foreign_files(self):
        m = patcher.patch_game(self.root, self.r, self.lines)
        keep = os.path.join(self.root, "sub", "dir", "mine.txt")
        with open(keep, "w") as fh:
            fh.write("x")
        patcher.rollback(self.root, m)
        self.assertTrue(os.path.isfile(keep))
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub", "dir", "new.txt")))

    def test_rollback_when_new_file_already_gone(self):
        m = patcher.patch_game(self.root, self.r, self.lines)
        os.remove(os.path.join(self.root, "sub", "dir", "new.txt"))
        patcher.rollback(self.root, m)
        self.assertEqual(snapshot(self.root), self.before)

    def test_swap_failure_on_second_file_removes_created_file_and_dirs(self):
        real = patcher._atomic_copy
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:  # files are swapped in sorted order: a.txt, then the new one
                raise OSError("disk full (injected)")
            return real(src, dst)

        with mock.patch.object(patcher, "_swap_one", flaky):
            with self.assertRaises(patcher.PatchAborted):
                patcher.patch_game(self.root, self.r, self.lines)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub")))

    def test_old_manifest_without_new_dirs_still_rolls_back(self):
        m = patcher.patch_game(self.root, self.r, self.lines)
        m.pop("new_dirs")
        patcher.rollback(self.root, m)
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub", "dir", "new.txt")))


class TestWolfFromArchives(unittest.TestCase):
    """End to end on a synthetic archive-only Wolf game: patch writes LOOSE files, rollback removes them."""

    def setUp(self):
        import test_wolf as tw
        import test_wolfarchive as twa
        from readers import wolf
        self.wolf, self.tmp = wolf, tempfile.mkdtemp()
        wolf.WORK_ROOT = os.path.join(self.tmp, "work")
        src = os.path.join(self.tmp, "src")
        tw.make_game(src, enc="utf-8", utf8=True)
        self.root = os.path.join(self.tmp, "g")
        os.makedirs(os.path.join(self.root, "Data"))
        for stem in ("BasicData", "MapData"):
            d = os.path.join(src, "Data", stem)
            tree = {f: tw.rd(os.path.join(d, f)) for f in os.listdir(d)}
            tw.write(self.root, "Data/%s.wolf" % stem,
                     twa.build_archive(tree, keystring=wolf.wolfarchive.WOLF_KEYS[0x13A], mode="mix"))
        self.before = snapshot(self.root)
        self.R = wolf.Reader()
        self.lines = self.R.extract(self.root)
        for l in self.lines:
            if l.kind == "dialogue":
                l.translation = "EN:" + l.source

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_patch_then_rollback(self):
        m = patcher.patch_game(self.root, self.R, self.lines)
        self.assertEqual(m["status"], "applied")
        self.assertTrue(m["files"])
        for f in m["files"]:
            self.assertIsNone(f["before"])  # nothing existed loose: every file is new
            self.assertTrue(f["path"].startswith(("Data/MapData/", "Data/BasicData/")), f["path"])
            self.assertTrue(os.path.isfile(os.path.join(self.root, *f["path"].split("/"))))
        after = snapshot(self.root)
        for k, v in self.before.items():  # the archives themselves are never touched
            self.assertEqual(after[k], v)
        again = self.wolf.Reader().extract(self.root)
        self.assertTrue(any(l.source.startswith("EN:") for l in again))
        patcher.rollback(self.root, m)
        self.assertEqual(snapshot(self.root), self.before)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "Data"))), ["BasicData.wolf", "MapData.wolf"])


if __name__ == "__main__":
    unittest.main()
