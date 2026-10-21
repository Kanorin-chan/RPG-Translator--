# -*- coding: utf-8 -*-
"""Headless UI tests: theme, App shell, every page builds with no provider server,
and the Projects flow (detect -> extract -> patch -> roll back) on a synthetic MV
game. Appearance is NOT checked here. Skips cleanly without a display.

Run: PYTHONUTF8=1 python test_ui_smoke.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _root():
    try:
        r = tk.Tk()
    except tk.TclError as e:
        raise unittest.SkipTest("no display: %r" % e)
    r.withdraw()
    return r


class ThemeTest(unittest.TestCase):
    def test_both_modes_and_styles(self):
        import theme
        root = _root()
        try:
            for mode in ("dark", "light"):
                palette, fonts = theme.apply_theme(root, mode)
                for key in ("bg", "surface", "text", "accent", "drop_idle",
                            "drop_hover", "danger", "success", "select"):
                    self.assertIn(key, palette)
                self.assertIn("mono", fonts)
                for st in ("Accent.TButton", "Secondary.TButton", "Danger.TButton",
                           "Toolbar.TButton"):
                    ttk.Button(root, text="x", style=st).update_idletasks()
                ttk.LabelFrame(root, text="c", style="Drop.TLabelframe").update_idletasks()
                ttk.Radiobutton(root, text="n", style="Nav.Toolbutton").update_idletasks()
            self.assertNotEqual(theme.PALETTES["dark"]["bg"], theme.PALETTES["light"]["bg"])
            theme.enable_dark_titlebar(root)   # must not raise
        finally:
            root.destroy()


class AppCase(unittest.TestCase):
    """Builds the App against a private APPDATA so real ui.json/providers are untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"APPDATA": self.tmp})
        self._env.start()
        self.root = _root()
        import ui
        self.ui = ui
        self.app = ui.App(self.root, dnd_ok=False, start_threads=False)
        self.root.update_idletasks()

    def tearDown(self):
        try:
            self.app.cancel_timers()
            self.root.destroy()
        except tk.TclError:
            pass
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump(self, cond, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False


class UIBuildTest(AppCase):
    def test_all_pages_build_and_switch(self):
        self.assertEqual(self.root.title(), "RPG-Translator")
        for key, _ in self.ui.PAGES:
            self.app.show_page(key)
            self.root.update()
            self.assertEqual(self.app.pages[key].winfo_manager(), "grid", key)
        # exactly one page shown at a time
        self.assertEqual(sum(1 for p in self.app.pages.values() if p.winfo_manager()), 1)

    def test_pages_need_no_provider_server(self):
        # default (synthesised local) profile exists, build_cfgs works offline
        prov = self.app.default_provider()
        self.assertIsNotNone(prov)
        cfgs = self.app.build_cfgs(prov)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0]["provider"]["id"], prov["id"])
        self.assertEqual(cfgs[0]["concurrency"], int(prov["concurrency"]))

    def test_review_page_hosted_or_placeholder(self):
        host = self.app.review_host
        self.assertTrue(host.winfo_children())
        try:
            import review_page   # noqa: F401
        except ImportError:
            self.assertIsNone(self.app.review)
        else:
            self.assertIsNotNone(self.app.review)

    def test_geometry_and_settings_persist(self):
        import app_common
        self.app._on_close()                      # writes ui.json (geometry + settings)
        d = app_common.load_ui_state()
        self.assertRegex(d["geometry"], r"^\d+x\d+[+-]-?\d+[+-]-?\d+$")
        self.assertIn("batch_size", d["settings"])
        d["geometry"] = "900x600+30+30"           # a saved window position/size
        d["settings"]["batch_size"] = 17
        self.assertTrue(app_common.save_ui_state(d))
        root2 = _root()
        app2 = None
        try:
            app2 = self.ui.App(root2, dnd_ok=False, start_threads=False)
            root2.update()
            self.assertEqual(app2.settings["batch_size"], 17)
            self.assertTrue(root2.geometry().startswith("900x600+30+30"), root2.geometry())
        finally:
            if app2:
                app2.cancel_timers()
            root2.destroy()
        self.root = _root()   # tearDown destroys it

    def test_light_theme_builds(self):
        self.app._on_close()
        with open(os.path.join(self.tmp, "RPG-Translator", "ui.json"), encoding="utf-8") as f:
            d = json.load(f)
        d["settings"]["theme"] = "light"
        with open(os.path.join(self.tmp, "RPG-Translator", "ui.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)
        root2 = _root()
        a2 = None
        try:
            a2 = self.ui.App(root2, dnd_ok=False, start_threads=False)
            root2.update_idletasks()
        finally:
            if a2:
                a2.cancel_timers()
            root2.destroy()
        self.root = _root()


class ProvidersPageTest(AppCase):
    def test_add_duplicate_delete_save_apply(self):
        import providers
        pg = self.app.providers_page
        n0 = len(pg.items)
        pg._add()
        self.assertEqual(len(pg.items), n0 + 1)
        pg.v["label"].set("Cloud A")
        pg.v["api_key"].set("sk-secret-123456789")
        pg._dup()
        self.assertEqual(len(pg.items), n0 + 2)
        self.assertEqual(len({p["id"] for p in pg.items}), n0 + 2)
        self.assertTrue(pg._save())
        saved = providers.load_providers()
        self.assertEqual(len(saved), n0 + 2)
        self.assertEqual(pg.key_entry.cget("show"), "•")      # masked by default
        pg.v_show.set(True)
        pg._toggle_key()
        self.assertEqual(pg.key_entry.cget("show"), "")
        # probe result -> Apply safe value stores concurrency in the profile
        pg._probe_pid = pg.items[pg.cur]["id"]
        pg._on_probe_done({"levels": [], "fastest": 8, "safe": 4, "stopped_because": "x"})
        self.assertEqual(str(pg.apply_btn.cget("state")), "normal")
        with mock.patch.object(self.app, "run_thread"):
            pg._apply_safe()
        self.assertEqual(providers.load_providers()[pg.cur]["concurrency"], 4)
        with mock.patch("pages_providers.messagebox.askyesno", return_value=True):
            pg._delete()
        self.assertEqual(len(pg.items), n0 + 1)

    def test_probe_table_fills_from_levels(self):
        pg = self.app.providers_page
        pg._on_level({"n": 2, "ok": 3, "errors": 0, "rate_limited": 1,
                      "lines_per_sec": 4.5, "p50": 1.0, "p95": 2.25})
        row = pg.ptree.item(pg.ptree.get_children()[0], "values")
        self.assertEqual([str(v) for v in row], ["2", "3", "0", "1", "4.50", "2.25"])

    def test_unreachable_provider_test_fails_gracefully(self):
        pg = self.app.providers_page
        pg.v["base_url"].set("http://127.0.0.1:9")   # nothing listens
        pg._test()
        self.assertTrue(self.pump(lambda: pg.test_var.get().startswith("Failed")))


class ProjectsFlowTest(AppCase):
    def test_detect_extract_patch_rollback(self):
        from test_rpgmmv import build
        game = build(os.path.join(self.tmp, "game"))
        snap = _snapshot(game)
        pp = self.app.projects
        pp.open_game(game)
        self.assertTrue(self.pump(lambda: pp.reader_cls is not None and not pp.busy))
        self.assertEqual(pp.reader_cls.name, "rpgmmv")
        pp.extract()
        self.assertTrue(self.pump(lambda: self.app.project is not None and not pp.busy))
        proj = self.app.project
        self.assertGreater(len(proj.lines), 0)
        self.assertTrue(os.path.isfile(pp.project_path))
        self.assertTrue(pp.v_lines.get().startswith(str(len(proj.lines))))
        # fake a translation for every dialogue line, then patch
        for l in proj.lines:
            if l.kind == "dialogue":
                l.translation, l.status = "T:" + l.source, "ok"
        pp._update_buttons()
        self.assertEqual(str(pp.btn_patch.cget("state")), "normal")
        with mock.patch("pages_projects.messagebox.askokcancel", return_value=True) as ask:
            pp.patch()
        self.assertIn("backup", ask.call_args[0][1].lower())
        self.assertTrue(self.pump(lambda: not pp.busy and "patch:" in pp.v_prog.get()))
        self.assertIn("applied", pp.v_prog.get())
        self.assertNotEqual(_snapshot(game), snap)       # game really changed
        # roll back through the same UI path (dialog bypassed)
        import patcher
        man = patcher.list_backups(game, pp.backup_dir())[0]
        with mock.patch.object(pp, "_pick_backup", return_value=man), \
                mock.patch("pages_projects.messagebox.askokcancel", return_value=True):
            pp.rollback()
        self.assertTrue(self.pump(lambda: not pp.busy and "rolled back" in pp.v_prog.get()))
        self.assertEqual(_snapshot(game), snap)

    def test_unknown_folder_shows_message(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        pp = self.app.projects
        pp.open_game(empty)
        self.assertTrue(self.pump(lambda: pp.v_engine.get() == "unknown"))
        self.assertTrue(pp.warn.get())
        self.assertEqual(str(pp.btn_extract.cget("state")), "disabled")

    def test_backup_dir_setting(self):
        pp = self.app.projects
        pp.game_root = os.path.join(self.tmp, "G")
        self.assertIsNone(pp.backup_dir())
        self.app.settings["backups_dir"] = os.path.join(self.tmp, "bk")
        self.assertTrue(pp.backup_dir().startswith(os.path.join(self.tmp, "bk")))

    def test_translate_requires_provider_fields(self):
        pp = self.app.projects
        import project as pm
        self.app.set_project(pm.Project(self.tmp, "rpgmmv", [pm.Line(
            "a#1", "a", "s", 0, "dialogue", "こんにちは")]), None)
        pp._update_buttons()
        self.app.default_provider()["model"] = ""
        with mock.patch("pages_projects.messagebox.showwarning") as w:
            pp.translate()
        self.assertTrue(w.called)
        self.assertFalse(pp.busy)


def _snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ("_rpgt_backups", "_rpgt")]
        for fn in fns:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
