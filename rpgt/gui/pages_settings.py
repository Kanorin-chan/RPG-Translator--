# -*- coding: utf-8 -*-
"""Settings page: run defaults for project translation, theme, folders.
Stored in %APPDATA%\\RPG-Translator\\ui.json (not config.json, which the server reads)."""
import tkinter as tk
from tkinter import filedialog, ttk

from rpgt.gui.app_common import LANG_CHOICES, autowrap


class SettingsPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        s = app.settings
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="Settings", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))

        run = ttk.LabelFrame(self, text="Project translation")
        run.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        run.columnconfigure(1, weight=1)
        self.v_lang = tk.StringVar(value=s["target_language"])
        self.v_mode = tk.StringVar(value=s["mode"])
        self.v_ctx = tk.StringVar(value=str(s["context_window"]))
        self.v_batch = tk.StringVar(value=str(s["batch_size"]))
        self._row(run, 0, "Target language", ttk.Combobox(
            run, textvariable=self.v_lang, state="readonly", width=24,
            values=[c[0] for c in LANG_CHOICES]))
        self._row(run, 1, "Mode", ttk.Combobox(
            run, textvariable=self.v_mode, state="readonly", width=24,
            values=("dynamic", "strict_ja")))
        self._row(run, 2, "Context window (lines)", ttk.Spinbox(
            run, from_=0, to=50, width=8, textvariable=self.v_ctx))
        self._row(run, 3, "Batch size (lines)", ttk.Spinbox(
            run, from_=1, to=200, width=8, textvariable=self.v_batch))
        autowrap(ttk.Label(run, text="dynamic: normalise any source to the target. strict_ja: "
                           "Japanese only. Context = previous lines sent as reference.",
                           style="Muted.TLabel", justify="left")).grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        look = ttk.LabelFrame(self, text="Appearance")
        look.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.v_theme = tk.StringVar(value=s["theme"])
        self._row(look, 0, "Theme", ttk.Combobox(
            look, textvariable=self.v_theme, state="readonly", width=24,
            values=("dark", "light")))
        ttk.Label(look, text="Takes effect after restarting the program.",
                  style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=10)

        paths = ttk.LabelFrame(self, text="Folders")
        paths.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        paths.columnconfigure(1, weight=1)
        self.v_proj = tk.StringVar(value=s["projects_dir"])
        self.v_back = tk.StringVar(value=s["backups_dir"])
        for r, (text, var, hint) in enumerate((
                ("Games start in", self.v_proj, "Where the folder pickers open."),
                ("Backups folder", self.v_back,
                 "Empty = <game>\\_rpgt_backups. Otherwise a subfolder per game here."))):
            ttk.Label(paths, text=text).grid(row=r * 2, column=0, sticky="w",
                                             padx=(0, 10), pady=3)
            ttk.Entry(paths, textvariable=var).grid(row=r * 2, column=1, sticky="ew")
            ttk.Button(paths, text="Browse…",
                       command=lambda v=var: self._browse(v)).grid(
                row=r * 2, column=2, padx=(8, 0))
            ttk.Label(paths, text=hint, style="Muted.TLabel").grid(
                row=r * 2 + 1, column=1, sticky="w")

        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, sticky="w")
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(
            side="left")
        self.msg = tk.StringVar()
        ttk.Label(bar, textvariable=self.msg, style="Muted.TLabel").pack(
            side="left", padx=10)

    @staticmethod
    def _row(parent, r, text, widget):
        ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
        widget.grid(row=r, column=1, sticky="w", pady=3)

    def _browse(self, var):
        d = filedialog.askdirectory(parent=self, initialdir=var.get() or None)
        if d:
            var.set(d)

    @staticmethod
    def _int(v, default, lo, hi):
        try:
            return max(lo, min(hi, int(float(v.get()))))
        except ValueError:
            return default

    def save(self):
        s = self.app.settings
        s["target_language"] = self.v_lang.get()
        s["mode"] = self.v_mode.get()
        s["context_window"] = self._int(self.v_ctx, 4, 0, 50)
        s["batch_size"] = self._int(self.v_batch, 40, 1, 200)
        s["theme"] = self.v_theme.get()
        s["projects_dir"] = self.v_proj.get().strip()
        s["backups_dir"] = self.v_back.get().strip()
        ok = self.app.save_settings()
        self.msg.set("Saved." if ok else "Could not write ui.json.")
        self.app.set_status("settings saved" if ok else "settings NOT saved")
