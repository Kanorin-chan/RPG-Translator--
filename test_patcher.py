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


if __name__ == "__main__":
    unittest.main()
