# -*- coding: utf-8 -*-
"""Shared UI helpers for RPG-Translator pages: tables, log text, drop targets,
settings persistence. No translation logic. Stdlib only."""
import json
import os
import sys
import tkinter as tk
from tkinter import ttk

try:
    from tkinterdnd2 import DND_FILES
except Exception:  # optional
    DND_FILES = None

LANG_CHOICES = [
    ("Traditional Chinese", "Traditional Chinese", "zh-Hant"),
    ("Simplified Chinese", "Simplified Chinese", "zh-Hans"),
    ("English", "English", "en"),
]
LANG_BY_LABEL = {c[0]: c for c in LANG_CHOICES}
MAX_LOG_LINES = 1000


def ui_json_path():
    base = os.environ.get("APPDATA")
    d = os.path.join(base, "RPG-Translator") if base else os.path.dirname(
        os.path.abspath(__file__))
    return os.path.join(d, "ui.json")


def load_ui_state():
    try:
        with open(ui_json_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_state(state):
    try:
        path = ui_json_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def fmt_eta(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    m, s = divmod(seconds, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m)


def oneline(s, n=120):
    s = (s or "").replace("\r", "").replace("\n", " / ")
    return s if len(s) <= n else s[: n - 1] + "…"


def make_table(parent, columns, height=8, selectmode="browse"):
    """columns: [(id, heading, width, anchor)]. Returns (frame, tree) with scrollbar."""
    fr = ttk.Frame(parent)
    fr.columnconfigure(0, weight=1)
    fr.rowconfigure(0, weight=1)
    tree = ttk.Treeview(fr, columns=[c[0] for c in columns], show="headings",
                        height=height, selectmode=selectmode)
    for cid, head, width, anchor in columns:
        tree.heading(cid, text=head, anchor=anchor)
        tree.column(cid, width=width, anchor=anchor, stretch=True)
    sb = ttk.Scrollbar(fr, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")
    return fr, tree


class LogText(ttk.Frame):
    """Read-only monospace log with a scrollbar; append() is main-thread only."""

    def __init__(self, parent, palette, fonts, height=10):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(self, height=height, wrap="none", state="disabled")
        from rpgt.gui import theme
        theme.style_text(self.text, palette, fonts)
        sy = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        sx = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")

    def clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def append(self, lines):
        if isinstance(lines, str):
            lines = [lines]
        t = self.text
        t.config(state="normal")
        for ln in lines:
            t.insert("end", ln if ln.endswith("\n") else ln + "\n")
        try:
            last = int(t.index("end-1c").split(".")[0])
            if last > MAX_LOG_LINES:
                t.delete("1.0", "%d.0" % (last - MAX_LOG_LINES))
        except Exception:
            pass
        t.config(state="disabled")
        t.see("end")


def register_drop(widget, handler, dnd_ok, hover_style=None, idle_style=None):
    """Make `widget` a file-drop target (no-op without tkinterdnd2)."""
    if not dnd_ok or DND_FILES is None:
        return

    def on_enter(event):
        if hover_style:
            widget.configure(style=hover_style)
        return event.action

    def on_leave(event):
        if idle_style:
            widget.configure(style=idle_style)

    def on_drop(event):
        on_leave(event)
        return handler(event)

    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<DropEnter>>", on_enter)
        widget.dnd_bind("<<DropLeave>>", on_leave)
        widget.dnd_bind("<<Drop>>", on_drop)
    except Exception as e:  # pragma: no cover
        sys.stderr.write("drop registration failed: %r\n" % e)


def dropped_path(root, data, keep_files=None):
    """First real filesystem path from a tkinterdnd2 payload.
    keep_files=None returns it as dropped. A tuple of extensions (e.g. (".json",)) means the
    caller wants a game FOLDER: any other dropped file, typically the game's .exe, is turned
    into the folder that holds it."""
    try:
        items = root.tk.splitlist(data)
    except Exception:
        items = [data.strip().strip("{}")]
    path = items[0] if items else ""
    if keep_files is not None and path and os.path.isfile(path)             and not path.lower().endswith(tuple(keep_files)):
        path = os.path.dirname(path)
    return path


def register_drop_tree(widget, handler, dnd_ok, hover_widget=None,
                       hover_style=None, idle_style=None):
    """register_drop on `widget` and every widget inside it. tkdnd does not pass a drop to a
    parent, so a drop that lands on a label or entry would otherwise be lost. Hover styling
    applies to hover_widget only."""
    if widget is hover_widget:
        register_drop(widget, handler, dnd_ok, hover_style, idle_style)
    else:
        register_drop(widget, handler, dnd_ok)
    for child in widget.winfo_children():
        register_drop_tree(child, handler, dnd_ok, hover_widget, hover_style, idle_style)


def label_entry_row(parent, row, text, widget, col=0, colspan=1, sticky="ew"):
    """Grid a right-padded label at (row,col) and widget at (row,col+1)."""
    ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w",
                                      padx=(0, 10), pady=3)
    widget.grid(row=row, column=col + 1, columnspan=colspan, sticky=sticky, pady=3)
    return widget


def autowrap(label, margin=24):
    """Wrap a ttk.Label to its own width (re-wraps on resize)."""
    label.bind("<Configure>", lambda e: label.configure(
        wraplength=max(100, e.width - margin)))
    return label
