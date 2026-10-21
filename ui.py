#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPG-Translator — desktop control UI (Tkinter + ttk, no web server).

Shell only: sidebar navigation, page stack, status bar, one work queue, and the
shared state (settings, current project, providers). The pages hold the logic:

  pages_projects  open a game, extract, translate, patch, roll back
  review_page     review list (optional module; placeholder if missing)
  pages_providers provider profiles, connection test, concurrency probe
  pages_xunity    offline MTool translate + XUnity server/install wizard
  pages_settings  language, mode, context, batch, theme, folders

Rules: no translation logic here; slow work runs on worker threads and reports
through App.post() -> queue -> App._poll_queue() (main thread only touches Tk).

Run (Windows):  PYTHONUTF8=1 python ui.py
"""

import os
import queue
import re
import sys
import threading
import traceback

import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import core            # noqa: E402
import providers       # noqa: E402
import theme           # noqa: E402
from app_common import (LANG_BY_LABEL, LANG_CHOICES, load_ui_state,  # noqa: E402,F401
                        save_ui_state)

try:  # tkinterdnd2 is optional: without it the app is Browse-only
    from tkinterdnd2 import TkinterDnD
    _DND_IMPORTED = True
except Exception:  # pragma: no cover - environment dependent
    TkinterDnD = None
    _DND_IMPORTED = False

APP_TITLE = "RPG-Translator"
PAGES = [("projects", "Projects"), ("review", "Review"), ("providers", "Providers"),
         ("xunity", "XUnity / MTool"), ("settings", "Settings")]
MSGS_PER_TICK = 300


def create_root():
    """Tk root, preferring TkinterDnD. Returns (root, dnd_ok); never raises for DnD."""
    if _DND_IMPORTED:
        try:
            return TkinterDnD.Tk(), True
        except Exception as e:  # pragma: no cover
            sys.stderr.write("tkinterdnd2 init failed; drag-drop disabled: %r\n" % e)
    return tk.Tk(), False


def default_settings():
    cfg = {}
    try:
        cfg = core.load_config()
    except Exception:
        pass
    lang = cfg.get("target_language", "Traditional Chinese")
    if lang not in LANG_BY_LABEL:
        lang = LANG_CHOICES[0][0]
    return {
        "target_language": lang, "mode": cfg.get("mode", "dynamic"),
        "context_window": int(cfg.get("context_window", 4)),
        "batch_size": int(cfg.get("batch_size", 40)),
        "theme": "dark", "projects_dir": "", "backups_dir": "",
        "default_provider": "",
    }


class App:
    """The whole UI. Construct with a Tk root and a dnd_ok flag."""

    def __init__(self, root, dnd_ok, start_threads=True):
        self.root = root
        self.dnd_ok = dnd_ok
        self.q = queue.Queue()
        self._handlers = {}
        self._flush_hooks = []
        self.project = None
        self.project_path = None
        self.provider_items = []          # filled by ProvidersPage, shared by reference
        self.projects = self.review = self.providers_page = None
        self.xunity = self.settings_page = None

        self.ui_state = load_ui_state()
        self.settings = default_settings()
        saved = self.ui_state.get("settings")
        if isinstance(saved, dict):
            self.settings.update({k: v for k, v in saved.items() if k in self.settings})

        root.title(APP_TITLE)
        self.palette, self.fonts = theme.apply_theme(root, self.settings["theme"])
        self._apply_geometry()
        self._after_ids = [root.after(60, lambda: theme.enable_dark_titlebar(
            root, self.settings["theme"] == "dark"))]

        self.status_action_var = tk.StringVar(value="ready")
        self.status_server_var = tk.StringVar(value="XUnity server: stopped")
        self.status_project_var = tk.StringVar(value="no project")
        self._build_shell()
        self._build_pages(start_threads)
        self.show_page(self.ui_state.get("page") if self.ui_state.get("page") in dict(PAGES)
                       else "projects")

        for i, (key, _) in enumerate(PAGES, 1):
            root.bind("<Control-Key-%d>" % i, lambda e, k=key: self.show_page(k))
        self._after_ids.append(root.after(80, self._poll_queue))
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ window
    def _apply_geometry(self):
        r = self.root
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.minsize(min(880, sw - 40), min(540, sh - 80))
        w, h = min(1040, sw - 40), min(680, sh - 90)
        x, y = (sw - w) // 2, max(0, (sh - h) // 3)
        m = re.match(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$", str(self.ui_state.get("geometry", "")))
        if m:
            gw, gh = int(m.group(1)), int(m.group(2))
            gx, gy = int(m.group(3)), int(m.group(4))
            w, h = max(600, min(gw, sw)), max(400, min(gh, sh))
            if -w + 80 < gx < sw - 80 and 0 <= gy < sh - 80:   # still on screen
                x, y = gx, gy
        r.geometry("%dx%d+%d+%d" % (w, h, x, y))
        if self.ui_state.get("zoomed"):
            try:
                r.state("zoomed")
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ layout
    def _build_shell(self):
        r = self.root
        r.columnconfigure(1, weight=1)
        r.rowconfigure(0, weight=1)
        # sidebar
        side = ttk.Frame(r, style="Nav.TFrame", width=170)
        side.grid(row=0, column=0, sticky="ns")
        side.pack_propagate(False)
        ttk.Label(side, text=APP_TITLE, style="Heading.TLabel",
                  background=self.palette["surface_alt"]).pack(
            anchor="w", padx=14, pady=(14, 12))
        self.page_var = tk.StringVar()
        self.nav = {}
        for i, (key, text) in enumerate(PAGES, 1):
            rb = ttk.Radiobutton(side, text="%s" % text, value=key, variable=self.page_var,
                                 style="Nav.Toolbutton",
                                 command=lambda k=key: self.show_page(k))
            rb.pack(fill="x")
            self.nav[key] = rb
        ttk.Separator(r, orient="vertical").grid(row=0, column=0, sticky="nse")
        # page area
        self.stack = ttk.Frame(r)
        self.stack.grid(row=0, column=1, sticky="nsew")
        self.stack.columnconfigure(0, weight=1)
        self.stack.rowconfigure(0, weight=1)
        # status bar
        bar = ttk.Frame(r, style="Bar.TFrame")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        inner = ttk.Frame(bar, style="Bar.TFrame")
        inner.pack(fill="x", padx=10, pady=3)
        ttk.Label(inner, textvariable=self.status_action_var, style="Bar.TLabel").pack(side="left")
        ttk.Label(inner, textvariable=self.status_server_var, style="Bar.TLabel").pack(side="right")
        ttk.Label(inner, text="|", style="Bar.TLabel").pack(side="right", padx=8)
        ttk.Label(inner, textvariable=self.status_project_var, style="Bar.TLabel").pack(side="right")
        if not self.dnd_ok:
            ttk.Label(inner, text="|", style="Bar.TLabel").pack(side="right", padx=8)
            ttk.Label(inner, text="drag-drop unavailable, use Browse",
                      style="Bar.TLabel").pack(side="right")

    def _build_pages(self, start_threads):
        import pages_projects
        import pages_providers
        import pages_settings
        import pages_xunity
        self.pages = {}
        # providers first: it owns provider_items that the Projects page lists
        self.providers_page = pages_providers.ProvidersPage(self.stack, self)
        self.projects = pages_projects.ProjectsPage(self.stack, self)
        self.review_host = ttk.Frame(self.stack, padding=12)
        self._build_review(self.review_host)
        self.xunity = pages_xunity.XUnityPage(self.stack, self, start_threads)
        self.settings_page = pages_settings.SettingsPage(self.stack, self)
        self.pages = {"projects": self.projects, "review": self.review_host,
                      "providers": self.providers_page, "xunity": self.xunity,
                      "settings": self.settings_page}
        self.projects.refresh_providers()
        self._cur_page = None

    def _build_review(self, host):
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        actions = {"retranslate": lambda ids: self.projects.retranslate(ids),
                   "save": lambda: self.projects.save_project(),
                   "status": self.set_status}
        try:
            from review_page import ReviewPage
            self.review = ReviewPage(host, self.get_project, actions)
            self.review.grid(row=0, column=0, sticky="nsew")
        except ImportError:
            ttk.Label(host, text="The review page (review_page.py) is not installed yet.",
                      style="Muted.TLabel").grid(row=0, column=0)
        except Exception as e:  # a broken review page must not take the app down
            traceback.print_exc()
            ttk.Label(host, text="Review page failed to load: %r" % (e,),
                      style="Danger.TLabel").grid(row=0, column=0)

    def show_page(self, key):
        if key not in self.pages or key == getattr(self, "_cur_page", None):
            self.page_var.set(key if key in self.pages else self.page_var.get())
            return
        if self._cur_page:
            self.pages[self._cur_page].grid_remove()
        self.pages[key].grid(row=0, column=0, sticky="nsew")
        self._cur_page = key
        self.page_var.set(key)

    # ------------------------------------------------------------------ shared state
    def get_project(self):
        return self.project

    def set_project(self, proj, path):
        self.project, self.project_path = proj, path
        if proj is None:
            self.status_project_var.set("no project")
        else:
            self.status_project_var.set("%s · %d lines" % (proj.engine, len(proj.lines)))
        if self.review is not None and proj is not None:
            try:
                self.review.load(proj)
            except Exception:
                traceback.print_exc()
                self.set_status("review page failed to load the project")

    def review_refresh(self, ids):
        if self.review is not None:
            try:
                self.review.refresh_lines(ids)
            except Exception:
                traceback.print_exc()

    def set_status(self, text):
        self.status_action_var.set(text)

    def save_settings(self):
        self.ui_state["settings"] = dict(self.settings)
        return save_ui_state(self.ui_state)

    def default_provider(self):
        items = self.provider_items
        want = self.settings.get("default_provider")
        for p in items:
            if p["id"] == want:
                return p
        return items[0] if items else None

    def providers_changed(self):
        if self.projects is not None:
            self.projects.refresh_providers()

    def build_cfgs(self, prov):
        """[primary cfg, fallback cfgs...] from config.json + Settings + provider profiles."""
        base = core.load_config()
        s = self.settings
        base.update(target_language=LANG_BY_LABEL.get(s["target_language"], LANG_CHOICES[0])[1],
                    mode=s["mode"], context_window=s["context_window"],
                    batch_size=s["batch_size"], context_enabled=s["context_window"] > 0)
        by_id = {p["id"]: p for p in self.provider_items}
        order, seen, todo = [], set(), [prov["id"]]
        while todo:
            pid = todo.pop(0)
            if pid in seen or pid not in by_id:
                continue
            seen.add(pid)
            if pid == prov["id"] or by_id[pid].get("enabled", True):
                order.append(pid)
            todo.extend(by_id[pid].get("fallback") or [])
        return [providers.make_cfg(base, by_id[pid]) for pid in order]

    # ------------------------------------------------------------------ threads / queue
    def run_thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def post(self, *msg):
        self.q.put(msg)

    def on(self, kind, fn):
        self._handlers[kind] = fn

    def on_flush(self, fn):
        self._flush_hooks.append(fn)

    def _poll_queue(self):
        try:
            for _ in range(MSGS_PER_TICK):
                msg = self.q.get_nowait()
                fn = self._handlers.get(msg[0])
                if fn is None:
                    continue
                try:
                    fn(*msg[1:])
                except Exception:   # one bad message must not stop the pump
                    traceback.print_exc()
        except queue.Empty:
            pass
        for fn in self._flush_hooks:
            try:
                fn()
            except Exception:
                traceback.print_exc()
        self._after_ids.append(self.root.after(60 if self.q.empty() else 5, self._poll_queue))
        del self._after_ids[:-4]

    # ------------------------------------------------------------------ shutdown
    def cancel_timers(self):
        for i in self._after_ids:
            try:
                self.root.after_cancel(i)
            except Exception:
                pass
        self._after_ids = []

    def _on_close(self):
        self.cancel_timers()
        try:
            self.ui_state["page"] = getattr(self, "_cur_page", "projects")
            if self.root.state() == "zoomed":
                self.ui_state["zoomed"] = True
            else:
                self.ui_state["zoomed"] = False
                self.ui_state["geometry"] = self.root.geometry()
            self.save_settings()
        except Exception:
            pass
        if self.projects is not None and self.projects.stop_flag is not None:
            self.projects.stop_flag.set()
        if self.xunity is not None:
            self.xunity.shutdown()
        self.root.destroy()


def main():
    root, dnd_ok = create_root()
    App(root, dnd_ok)
    root.mainloop()


if __name__ == "__main__":
    main()
