# -*- coding: utf-8 -*-
"""Tests for review_page.ReviewPage. Skips when there is no display.
Run: PYTHONUTF8=1 python test_review_page.py
"""
import os
import sys
import time
import unittest
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import project as P
import review_page


def make_project(n):
    lines = []
    for i in range(n):
        st = ("new", "ok", "kept", "edited", "skipped")[i % 5]
        lines.append(P.Line(
            id="f#%d" % i, file="f.json", scene="scene%02d" % (i // 100), order=i,
            kind="dialogue", source="こんにちは %d" % i, speaker="Mio" if i % 2 else "",
            translation="" if st == "new" else "Hello %d" % i, status=st,
            flagged=(i % 7 == 0)))
    return P.Project(root="x", engine="t", lines=lines)


class ReviewPageTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display: %r" % e)
        self.root.withdraw()
        self.calls = {"rt": [], "save": 0, "status": []}
        self.proj = make_project(1000)
        a = {"retranslate": lambda ids: self.calls["rt"].append(list(ids)),
             "save": lambda: self.calls.__setitem__("save", self.calls["save"] + 1),
             "status": lambda s: self.calls["status"].append(s)}
        self.page = review_page.ReviewPage(self.root, lambda: self.proj, a)
        self.page.pack(fill="both", expand=True)

    def tearDown(self):
        self.root.destroy()

    def settle(self, page=None):
        page = page or self.page
        end = time.time() + 30
        while page.loading and time.time() < end:
            self.root.update()
        self.root.update()
        self.assertFalse(page.loading)

    def load(self):
        self.page.load(self.proj)
        self.settle()

    def test_populate_5k_fast(self):
        self.proj = make_project(5000)
        t0 = time.time()
        self.load()
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(len(self.page.tree.get_children()), 5000)
        self.assertEqual(self.page.v_count.get(), "5000 of 5000 lines")

    def test_works_under_plain_ttk(self):
        ttk.Style(self.root).theme_use("default")
        self.load()
        self.assertEqual(len(self.page.tree.get_children()), 1000)

    def test_filters(self):
        self.load()
        p = self.page
        p.set_filter(status="kept")
        self.settle()
        want = [l for l in self.proj.lines if l.status == "kept"]
        self.assertEqual(len(p.tree.get_children()), len(want))
        p.set_filter(status="flagged")
        self.settle()
        self.assertEqual(len(p.tree.get_children()),
                         sum(1 for l in self.proj.lines if l.flagged))
        p.set_filter(status="all", scene="scene03")
        self.settle()
        self.assertEqual(len(p.tree.get_children()), 100)
        p.set_filter(scene="", text="HELLO 42")
        self.settle()
        want = [l for l in self.proj.lines if "hello 42" in l.translation.lower()]
        self.assertTrue(want)
        self.assertEqual(len(p.tree.get_children()), len(want))
        p.set_filter(text="こんにちは 99")
        self.settle()
        self.assertEqual(p.v_count.get(), "11 of 1000 lines")  # 99, 990-999 via source

    def test_multiselect_ids(self):
        self.load()
        kids = self.page.tree.get_children()
        self.page.tree.selection_set(kids[3], kids[1], kids[10])
        self.assertEqual(self.page.selected_ids(), ["f#1", "f#3", "f#10"])
        self.page.select_all()
        self.assertEqual(len(self.page.selected_ids()), 1000)

    def test_edit_save_marks_edited_and_revert(self):
        self.load()
        p = self.page
        p.tree.selection_set("r1")
        self.root.update()
        p._show_detail()
        p.t_tr.delete("1.0", "end")
        p.t_tr.insert("1.0", "Edited text\nsecond line")
        p.save_edit()
        l = self.proj.lines[1]
        self.assertEqual((l.translation, l.status), ("Edited text\nsecond line", "edited"))
        self.assertEqual(p.tree.set("r1", "status"), "edited")
        self.assertTrue(p.tree.set("r1", "translation").endswith("…"))
        self.assertEqual(self.calls["save"], 1)
        p.revert_line()
        self.assertEqual((l.translation, l.status), ("Hello 1", "ok"))
        self.assertEqual(p.tree.set("r1", "status"), "ok")

    def test_flag_toggle(self):
        self.load()
        p = self.page
        p.tree.selection_set("r1", "r2")
        p.toggle_flag()
        self.assertTrue(self.proj.lines[1].flagged and self.proj.lines[2].flagged)
        self.assertEqual(p.tree.set("r1", "status"), "flagged")
        p.toggle_flag()
        self.assertFalse(self.proj.lines[1].flagged or self.proj.lines[2].flagged)
        self.assertEqual(p.tree.set("r1", "status"), "ok")

    def test_retranslate_gets_ids(self):
        self.load()
        self.page.tree.selection_set("r5", "r2")
        self.page.retranslate_selected()
        self.assertEqual(self.calls["rt"], [["f#2", "f#5"]])
        self.page.tree.selection_set()
        self.page.retranslate_selected()
        self.assertEqual(len(self.calls["rt"]), 1)   # empty selection: no call

    def test_refresh_touches_only_given_rows(self):
        self.load()
        p = self.page
        before = {k: p.tree.item(k) for k in p.tree.get_children()}
        self.proj.lines[4].translation = "changed"
        self.proj.lines[4].status = "ok"
        self.proj.lines[9].flagged = True
        p.refresh_lines(["f#4", "f#9", "nope"])
        changed = [k for k in p.tree.get_children() if p.tree.item(k) != before[k]]
        self.assertEqual(sorted(changed), ["r4", "r9"])
        self.assertEqual(p.tree.set("r4", "translation"), "changed")

    def test_context_shown(self):
        self.load()
        self.page.tree.selection_set("r10")
        self.page._show_detail()
        ctx = self.page.t_ctx.get("1.0", "end")
        self.assertIn("こんにちは 9", ctx)
        self.assertIn("こんにちは 6", ctx)
        self.assertNotIn("こんにちは 5\n", ctx)   # K=4


if __name__ == "__main__":
    unittest.main(verbosity=2)
