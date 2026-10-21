#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for runner.py with a fake core.call_llm. Run: PYTHONUTF8=1 python test_runner.py"""

import os
import re
import tempfile
import threading
import unittest

import core
import project as pj
import runner

BASE = {"name": "primary", "model": "m", "endpoint_url": "x", "batch_size": 2,
        "context_window": 2, "save_every_batches": 1, "temperature": 0.3,
        "max_tokens_per_entry": 60, "mode": "dynamic",
        "target_language": "Traditional Chinese"}


def cfg(**kw):
    return dict(BASE, **kw)


class Fake:
    """Deterministic fake LLM. replies[src] = queue of overrides popped per translation."""

    def __init__(self, replies=None, batch_garbage=False, bad_cfg_names=()):
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.batch_garbage = batch_garbage
        self.bad = set(bad_cfg_names)
        self.calls = []  # (kind, cfg name, sources, ctx_sources, ctx_pairs)
        self.lock = threading.Lock()

    def _out(self, src):
        q = self.replies.get(src)
        return q.pop(0) if q else "T<%s>" % src

    def __call__(self, c, msg, temperature, max_tokens, mode="dynamic"):
        pairs = re.findall(r"^    (.*) => (.*)$", msg, re.M)
        with self.lock:
            if c.get("name") in self.bad:
                self.calls.append(("error", c.get("name"), [], [], []))
                raise RuntimeError("down")
            if "Translate each numbered" in msg:
                body = msg.split("Translate each numbered", 1)[1]
                ch = core.MARK_SPLIT_RE.split(body)
                srcs = [ch[i + 1].strip() for i in range(1, len(ch) - 1, 2)]
                self.calls.append(("batch", c.get("name"), srcs, [p[0] for p in pairs], pairs))
                if self.batch_garbage:
                    return "no markers here"
                return "\n".join("%s%d%s\n%s" % (core.MARK_OPEN, i, core.MARK_CLOSE,
                                                 self._out(s))
                                 for i, s in enumerate(srcs, 1))
            src = msg.rsplit("\n\n", 1)[1]
            self.calls.append(("solo", c.get("name"), [src], [p[0] for p in pairs], pairs))
            return self._out(src)

    def sent(self):
        return [s for k, _, ss, _, _ in self.calls for s in ss]


def make_project():
    lines, order = [], 0
    for sc in ("S1", "S2", "S3"):
        for n in range(5):
            src = "%s-line%d" % (sc, n)
            kind = "dialogue"
            if n == 3:
                src = "…"           # no payload -> filter skips it
            if n == 4:
                src, kind = "\\N[1]", "dialogue"  # pure code -> skipped
            lines.append(pj.Line(id="f#%d" % order, file="f", scene=sc, order=order,
                                 kind=kind, source=src))
            order += 1
    return pj.Project(root="r", engine="t", lines=lines)


def snap(p):
    return [(l.id, l.status, l.translation, l.flagged) for l in p.lines]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = core.FAILURES_LOG, core.call_llm
        core.FAILURES_LOG = os.path.join(self.tmp.name, "failures.log")
        self.fake = Fake()
        core.call_llm = self.fake

    def tearDown(self):
        core.FAILURES_LOG, core.call_llm = self._old
        self.tmp.cleanup()

    def use(self, fake):
        self.fake = fake
        core.call_llm = fake

    def test_batches_scene_contiguous_and_counts(self):
        p = make_project()
        s = runner.run_project(p, [cfg()], {})
        self.assertEqual((s["ok"], s["skipped"], s["kept"], s["failed"]), (9, 6, 0, 0))
        self.assertEqual(s["scenes_done"], 3)
        for kind, _, srcs, ctx, pairs in self.fake.calls:
            self.assertEqual(kind, "batch")
            self.assertLessEqual(len(srcs), 2)
            scenes = {x[:2] for x in srcs + ctx}
            self.assertEqual(len(scenes), 1, "context/segments crossed a scene")
            for a, b in pairs:
                self.assertEqual(b, "T<%s>" % a)  # prior translations, not sources
        first_s2 = next(c for c in self.fake.calls if c[2][0].startswith("S2"))
        self.assertEqual(first_s2[3], [])  # nothing earlier in its own scene
        second_s1 = [c for c in self.fake.calls if c[2][0].startswith("S1")][1]
        self.assertEqual(second_s1[3], ["S1-line0", "S1-line1"])
        for l in p.lines:
            if l.status == "ok":
                self.assertEqual(l.translation, "T<%s>" % l.source)

    def test_skipped_never_sent(self):
        p = make_project()
        runner.run_project(p, [cfg()], {})
        sent = self.fake.sent()
        self.assertNotIn("…", sent)
        self.assertNotIn("\\N[1]", sent)
        self.assertEqual({l.status for l in p.lines if l.source in ("…", "\\N[1]")}, {"skipped"})

    def test_solo_fallback_on_bad_markers(self):
        self.use(Fake(batch_garbage=True))
        p = make_project()
        s = runner.run_project(p, [cfg()], {})
        self.assertEqual(s["ok"], 9)
        kinds = [c[0] for c in self.fake.calls]
        self.assertIn("solo", kinds)
        self.assertEqual(kinds.count("solo"), 9)

    def test_structure_failure_retry_then_kept(self):
        p = make_project()
        p.lines[0].source = "Hi \\N[1] there"
        self.use(Fake({"Hi \\N[1] there": ["Hi there", "Hi there"]}))
        s = runner.run_project(p, [cfg()], {})
        l = p.lines[0]
        self.assertEqual((l.status, l.flagged, l.translation), ("kept", True, l.source))
        self.assertTrue(l.note.startswith("structure_mismatch"))
        self.assertEqual(s["kept"], 1)

    def test_structure_failure_recovered_by_retry(self):
        p = make_project()
        p.lines[0].source = "Hi \\N[1] there"
        self.use(Fake({"Hi \\N[1] there": ["Hi there", "Yo \\N[1]"]}))
        runner.run_project(p, [cfg()], {})
        l = p.lines[0]
        self.assertEqual((l.status, l.flagged, l.translation), ("ok", False, "Yo \\N[1]"))

    def test_language_drift_kept(self):
        p = make_project()
        p.lines[0].source = "你好嗎"
        self.use(Fake({"你好嗎": ["ปลั๊กอิน", "ปลั๊กอิน"]}))
        runner.run_project(p, [cfg()], {})
        l = p.lines[0]
        self.assertEqual((l.status, l.flagged), ("kept", True))
        self.assertTrue(l.note.startswith("language_drift"))

    def test_resume_sends_nothing(self):
        p = make_project()
        runner.run_project(p, [cfg()], {})
        n = len(self.fake.calls)
        s = runner.run_project(p, [cfg()], {})
        self.assertEqual(len(self.fake.calls), n)
        self.assertEqual(s["ok"], 0)

    def test_edited_protected_and_force(self):
        p = make_project()
        p.lines[0].status, p.lines[0].translation = "edited", "mine"
        runner.run_project(p, [cfg()], {})
        self.assertEqual((p.lines[0].status, p.lines[0].translation), ("edited", "mine"))
        self.assertNotIn("S1-line0", self.fake.sent())
        self.fake.calls.clear()
        s = runner.run_project(p, [cfg()], {}, force=True)
        self.assertEqual(s["ok"], 9)
        self.assertEqual(len(self.fake.sent()), 9)
        self.assertEqual(p.lines[0].translation, "T<S1-line0>")

    def test_stop_flag(self):
        p = make_project()
        path = os.path.join(self.tmp.name, "p.json")
        stop = threading.Event()
        s = runner.run_project(p, [cfg()], {}, project_path=path, stop_flag=stop,
                               on_progress=lambda d, t, i: stop.set())
        self.assertTrue(s["stopped"])
        self.assertEqual(s["batches_done"], 1)
        self.assertEqual(sum(1 for l in p.lines if l.status == "ok"), 2)
        self.assertEqual(snap(pj.Project.load(path)), snap(p))

    def test_concurrency_matches_sequential(self):
        a, b = make_project(), make_project()
        runner.run_project(a, [cfg()], {}, concurrency=1)
        self.fake.calls.clear()
        runner.run_project(b, [cfg()], {}, concurrency=4)
        self.assertEqual(snap(a), snap(b))
        for _, _, srcs, ctx, _ in self.fake.calls:
            self.assertEqual(len({x[:2] for x in srcs + ctx}), 1)

    def test_only_ids_and_save_and_callbacks(self):
        p = make_project()
        path = os.path.join(self.tmp.name, "q.json")
        seen, prog = [], []
        runner.run_project(p, [cfg()], {}, project_path=path, only_ids={"f#0", "f#1"},
                           on_line=lambda l: seen.append(l.id),
                           on_progress=lambda d, t, i: prog.append((d, t)))
        self.assertEqual(sorted(seen), ["f#0", "f#1"])
        self.assertEqual(prog, [(1, 1)])
        self.assertEqual(sum(1 for l in p.lines if l.status != "new"), 2)
        self.assertTrue(os.path.exists(path))

    def test_retranslate_multiselect_context_and_isolation(self):
        p = make_project()
        runner.run_project(p, [cfg()], {})
        p.lines[1].status = "edited"
        p.lines[1].flagged = True
        self.fake.calls.clear()
        s = runner.retranslate_lines(p, ["f#1", "f#6", "nope"], [cfg()], {}, context_k=2)
        self.assertEqual((s["ok"], s["missing"]), (2, 1))
        calls = self.fake.calls
        self.assertEqual([c[0] for c in calls], ["solo", "solo"])
        self.assertEqual([c[2] for c in calls], [["S1-line1"], ["S2-line1"]])
        self.assertEqual(calls[0][3], ["S1-line0"])  # only one line precedes it
        # game-order context (previous 2 lines), crossing the scene boundary
        self.assertEqual(calls[1][3], ["\\N[1]", "S2-line0"])
        self.assertNotIn("S1-line1", calls[1][3])  # no other target leaks in
        self.assertEqual((p.lines[1].status, p.lines[1].flagged), ("ok", False))

    def test_retranslate_clears_or_sets_flag(self):
        p = make_project()
        p.lines[0].source = "Hi \\N[1] there"
        p.lines[0].flagged, p.lines[0].status = True, "kept"
        self.use(Fake({"Hi \\N[1] there": ["Hi there", "Hi there"]}))
        runner.retranslate_lines(p, ["f#0"], [cfg()], {})
        self.assertEqual((p.lines[0].status, p.lines[0].flagged), ("kept", True))
        runner.retranslate_lines(p, ["f#0"], [cfg()], {})
        self.assertEqual((p.lines[0].status, p.lines[0].flagged), ("ok", False))

    def test_fallback_cfg_used(self):
        self.use(Fake(bad_cfg_names={"primary"}))
        p = make_project()
        s = runner.run_project(p, [cfg(), cfg(name="backup")], {})
        self.assertEqual(s["ok"], 9)
        names = {c[1] for c in self.fake.calls if c[0] != "error"}
        self.assertEqual(names, {"backup"})
        self.assertTrue(any(c[0] == "error" for c in self.fake.calls))

    def test_all_cfgs_down_leaves_lines_new(self):
        self.use(Fake(bad_cfg_names={"primary", "backup"}))
        p = make_project()
        s = runner.run_project(p, [cfg(), cfg(name="backup")], {})
        self.assertEqual((s["ok"], s["failed"]), (0, 9))
        self.assertEqual({l.status for l in p.lines if l.source not in ("…", "\\N[1]")}, {"new"})

    def test_call_with_fallback_raises_last(self):
        def f(c):
            raise RuntimeError(c["n"])
        with self.assertRaises(RuntimeError) as cm:
            runner._call_with_fallback(f, [{"n": "a"}, {"n": "b"}])
        self.assertEqual(str(cm.exception), "b")


if __name__ == "__main__":
    unittest.main()
