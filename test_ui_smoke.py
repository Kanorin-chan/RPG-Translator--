# -*- coding: utf-8 -*-
"""Headless smoke test for the redesigned UI. Builds widgets once and destroys
them, asserting no exception — guards against bad ttk style names, bad tk widget
options, and layout errors. Appearance is NOT checked (human-verified). Skips
cleanly when no desktop/display is available.

Run: PYTHONUTF8=1 python test_ui_smoke.py
"""
import os
import sys
import unittest
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class ThemeTest(unittest.TestCase):
    def _root(self):
        try:
            r = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display: %r" % e)
        r.withdraw()
        return r

    def test_apply_theme_returns_palette_and_styles_usable(self):
        import theme
        root = self._root()
        try:
            palette, fonts = theme.apply_theme(root)
            for key in ("bg", "surface", "text", "accent", "drop_idle",
                        "drop_hover", "danger", "success"):
                self.assertIn(key, palette)
            self.assertIn("mono", fonts)
            # every custom style must be creatable without raising
            for st in ("Accent.TButton", "Secondary.TButton", "Danger.TButton"):
                ttk.Button(root, text="x", style=st).update_idletasks()
            ttk.LabelFrame(root, text="c", style="Card.TLabelframe").update_idletasks()
            theme.enable_dark_titlebar(root)  # must not raise
        finally:
            root.destroy()


class UIBuildTest(unittest.TestCase):
    def _root(self):
        try:
            r = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display: %r" % e)
        r.withdraw()
        return r

    def test_full_ui_builds_without_error(self):
        import ui
        root = self._root()
        try:
            ui.App(root, dnd_ok=False)   # dnd off: no tkinterdnd2 needed to build
            root.update_idletasks()      # force realization -> surfaces style errors
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
