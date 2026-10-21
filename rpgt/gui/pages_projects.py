# -*- coding: utf-8 -*-
"""Projects page: open a game, extract, translate, patch with backup, roll back.

Everything slow runs on worker threads and reports through app.post(); this
page only touches widgets from the main thread (handlers registered on app)."""
import hashlib
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from rpgt import core
from rpgt import patcher
from rpgt import project as project_mod
from rpgt import runner
try:   # a broken reader module must not stop the UI from starting
    from rpgt import readers
    READERS_ERROR = ""
except Exception as _e:   # pragma: no cover - environment dependent
    READERS_ERROR = repr(_e)

    class readers:   # noqa: N801 - stub with the same two names
        READERS = {}

        @staticmethod
        def detect(root):
            return None
from rpgt.gui.app_common import (LogText, autowrap, dropped_path, fmt_eta, make_table,
                        register_drop_tree)


def archive_status_of(root):
    """'archive-encrypted' if any loaded reader reports an undecryptable archive."""
    for name in ("rpgmmv", "rpgmruby", "wolf"):
        mod = sys.modules.get("readers." + name)
        fn = getattr(mod, "archive_status", None)
        if fn:
            try:
                if fn(root) == "archive-encrypted":
                    return "archive-encrypted"
            except Exception:
                pass
    return None


class ProjectsPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.root = app.root
        self.game_root = None
        self.reader_cls = None
        self.project_path = None
        self.busy = False
        self.stop_flag = None
        self.t0 = 0.0
        self.scenes_total = 0
        self._line_ids = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        ttk.Label(self, text="Projects", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))

        # toolbar
        tb = ttk.Frame(self)
        tb.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.btn_open = ttk.Button(tb, text="Open game folder…", command=self.browse_game)
        self.btn_load = ttk.Button(tb, text="Load project…", command=self.browse_project)
        self.btn_saveas = ttk.Button(tb, text="Save as…", command=self.save_as)
        self.btn_extract = ttk.Button(tb, text="Extract", command=self.extract)
        self.btn_translate = ttk.Button(tb, text="Translate", style="Accent.TButton",
                                        command=self.translate)
        self.btn_stop = ttk.Button(tb, text="Stop", style="Danger.TButton",
                                   command=self.stop)
        self.btn_patch = ttk.Button(tb, text="Patch game", command=self.patch)
        self.btn_rollback = ttk.Button(tb, text="Roll back…", command=self.rollback)
        for i, b in enumerate((self.btn_open, self.btn_load, self.btn_saveas)):
            b.pack(side="left", padx=(0, 6))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        for b in (self.btn_extract, self.btn_translate, self.btn_stop):
            b.pack(side="left", padx=(0, 6))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        for b in (self.btn_patch, self.btn_rollback):
            b.pack(side="left", padx=(0, 6))

        # game info (also the drop target)
        self.info = ttk.LabelFrame(self, text="Game", style="Drop.TLabelframe")
        self.info.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.info.columnconfigure(1, weight=1)
        self.v_path = tk.StringVar()
        self.v_engine = tk.StringVar(value="-")
        self.v_lines = tk.StringVar(value="-")
        self.v_proj = tk.StringVar(value="-")
        self.v_prov = tk.StringVar()
        ttk.Label(self.info, text="Folder").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(self.info, textvariable=self.v_path, state="readonly").grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=2)
        ttk.Label(self.info, text="Engine").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=2)
        ttk.Label(self.info, textvariable=self.v_engine).grid(row=1, column=1, sticky="w")
        ttk.Label(self.info, text="Lines").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=2)
        ttk.Label(self.info, textvariable=self.v_lines).grid(row=2, column=1, sticky="w")
        ttk.Label(self.info, text="Project file").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=2)
        ttk.Entry(self.info, textvariable=self.v_proj, state="readonly").grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=2)
        ttk.Label(self.info, text="Provider").grid(row=1, column=2, sticky="e", padx=(16, 8))
        self.prov_cb = ttk.Combobox(self.info, textvariable=self.v_prov,
                                    state="readonly", width=34)
        self.prov_cb.grid(row=1, column=3, sticky="e")
        self.prov_cb.bind("<<ComboboxSelected>>", self._on_prov)
        self.warn = tk.StringVar()
        self.warn_lbl = ttk.Label(self.info, textvariable=self.warn,
                                  style="Danger.TLabel", justify="left")
        self.warn_lbl.grid(row=4, column=0, columnspan=4, sticky="ew")
        autowrap(self.warn_lbl)
        self.warn_lbl.grid_remove()
        self.warn.trace_add("write", lambda *_: self.warn_lbl.grid()
                            if self.warn.get() else self.warn_lbl.grid_remove())

        # progress
        pr = ttk.Frame(self)
        pr.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        pr.columnconfigure(0, weight=1)
        self.prog = ttk.Progressbar(pr, mode="determinate", maximum=100)
        self.prog.grid(row=0, column=0, sticky="ew")
        self.v_prog = tk.StringVar(value="idle")
        ttk.Label(pr, textvariable=self.v_prog, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0))

        ttk.Label(self, text="Log", style="Heading.TLabel").grid(
            row=4, column=0, sticky="w", pady=(0, 3))
        self.log = LogText(self, app.palette, app.fonts, height=8)
        self.log.grid(row=5, column=0, sticky="nsew")

        for k, fn in (("p_detect", self._on_detect), ("p_loaded", self._on_loaded),
                      ("p_log", self.log.append), ("p_progress", self._on_progress),
                      ("p_line", self._on_line), ("p_done", self._on_done),
                      ("p_error", self._on_error),
                      ("p_rb_refused", self._on_rb_refused)):
            app.on(k, fn)
        app.on_flush(self._flush_lines)
        self.refresh_providers()
        self._update_buttons()
        # the whole page takes a drop (a game folder, its .exe, or a project .json)
        register_drop_tree(self, self._on_drop, app.dnd_ok, self.info,
                           "DropHover.TLabelframe", "Drop.TLabelframe")
        if READERS_ERROR:
            self.warn.set("Reader modules failed to load: " + READERS_ERROR)

    # ---------------------------------------------------------------- state
    @property
    def project(self):
        return self.app.project

    def _update_buttons(self):
        has_game = bool(self.game_root)
        has_proj = self.project is not None
        n_tr = sum(1 for l in self.project.lines if l.translation) if has_proj else 0
        idle = not self.busy

        def st(on):
            return "normal" if (on and idle) else "disabled"
        self.btn_open.config(state=st(True))
        self.btn_load.config(state=st(True))
        self.btn_saveas.config(state=st(has_proj))
        self.btn_extract.config(state=st(has_game and self.reader_cls is not None))
        self.btn_translate.config(state=st(has_proj and bool(self.project.lines)))
        self.btn_patch.config(state=st(has_proj and n_tr > 0 and self.reader_cls is not None))
        self.btn_rollback.config(state=st(has_game))
        self.btn_stop.config(state="normal" if (self.busy and self.stop_flag) else "disabled")

    def _set_busy(self, busy, stop_flag=None):
        self.busy = busy
        self.stop_flag = stop_flag if busy else None
        self._update_buttons()

    def _summary(self):
        p = self.project
        if not p:
            self.v_lines.set("-")
            return
        c = {}
        for l in p.lines:
            c[l.status] = c.get(l.status, 0) + 1
        flagged = sum(1 for l in p.lines if l.flagged)
        self.v_lines.set("%d  (translated %d, kept original %d, edited %d, skipped %d, new %d; flagged %d)"
                         % (len(p.lines), c.get("ok", 0), c.get("kept", 0),
                            c.get("edited", 0), c.get("skipped", 0), c.get("new", 0),
                            flagged))

    def refresh_providers(self):
        items = self.app.provider_items
        labels = [p.get("label") or p["id"] for p in items]
        self.prov_cb.config(values=labels)
        cur = self.app.default_provider()
        if cur:
            self.v_prov.set(cur.get("label") or cur["id"])
        else:
            self.v_prov.set("")

    def _on_prov(self, _e=None):
        i = self.prov_cb.current()
        if 0 <= i < len(self.app.provider_items):
            self.app.settings["default_provider"] = self.app.provider_items[i]["id"]
            self.app.save_settings()
            self.app.providers_changed()

    # ---------------------------------------------------------------- open / load
    def _start_dir(self):
        return self.app.settings.get("projects_dir") or None

    def browse_game(self):
        d = filedialog.askdirectory(title="Select the game folder",
                                    initialdir=self._start_dir())
        if d:
            self.open_game(d)

    def browse_project(self):
        f = filedialog.askopenfilename(
            title="Load project", initialdir=self._start_dir(),
            filetypes=[("Project", "project.json"), ("JSON", "*.json"), ("All", "*.*")])
        if f:
            self.load_project_file(f)

    def _on_drop(self, event):
        path = dropped_path(self.root, event.data, keep_files=(".json",))
        if not path or self.busy:
            return
        if os.path.isdir(path):
            self.open_game(path)
        elif path.lower().endswith(".json"):
            self.load_project_file(path)

    def open_game(self, path):
        path = os.path.normpath(path)
        self.game_root = path
        self.v_path.set(path)
        self.v_engine.set("detecting…")
        self.warn.set("")
        self.project_path = os.path.join(path, "_rpgt", "project.json")
        self.v_proj.set(self.project_path)
        self.app.set_project(None, None)
        self._summary()
        self._set_busy(True)
        self.app.run_thread(self._detect_worker, path, self.project_path)

    def _detect_worker(self, path, proj_path):
        try:
            cls = readers.detect(path)
            archive = None if cls else archive_status_of(path)
            loaded = None
            if os.path.isfile(proj_path):
                try:
                    loaded = project_mod.Project.load(proj_path)
                except Exception as e:
                    self.app.post("p_log", "existing project file unreadable: %r" % e)
            self.app.post("p_detect", path, cls.name if cls else None,
                          getattr(cls, "label", "") if cls else "", archive, loaded)
        except Exception as e:
            self.app.post("p_error", "detect failed: %r" % e)

    def _on_detect(self, path, name, label, archive, loaded):
        self._set_busy(False)
        self.reader_cls = readers.READERS.get(name) if name else None
        if name:
            self.v_engine.set("%s  (%s)" % (label or name, name))
            self.log.append("Detected %s in %s" % (name, path))
        elif archive == "archive-encrypted":
            self.v_engine.set("not readable")
            self.warn.set("This game's data is in an encrypted archive. "
                          "Extract it with an external tool first; the program does not decrypt archives.")
        else:
            self.v_engine.set("unknown")
            self.warn.set("No supported engine found in this folder "
                          "(RPG Maker MV/MZ/VX/VXAce/XP, or extracted Wolf data).")
        if loaded is not None:
            self.log.append("Loaded existing project (%d lines)." % len(loaded.lines))
            self._adopt(loaded)
        self._update_buttons()
        self.app.set_status("opened %s" % path)

    def load_project_file(self, path):
        path = os.path.normpath(path)
        self._set_busy(True)
        self.v_proj.set(path)
        self.app.run_thread(self._load_worker, path)

    def _load_worker(self, path):
        try:
            proj = project_mod.Project.load(path)
        except Exception as e:
            self.app.post("p_error", "could not load %s: %r" % (path, e))
            return
        self.app.post("p_loaded", path, proj)

    def _on_loaded(self, path, proj):
        self.prog.stop()
        self.prog.config(mode="determinate", maximum=100, value=0)
        self.v_prog.set("idle")
        self.project_path = path
        self.v_proj.set(path)
        self.game_root = proj.root
        self.v_path.set(proj.root)
        self.reader_cls = readers.READERS.get(proj.engine)
        self.v_engine.set(proj.engine if self.reader_cls is None else
                          "%s  (%s)" % (self.reader_cls.label or proj.engine, proj.engine))
        self.warn.set("" if os.path.isdir(proj.root) else
                      "The game folder in this project no longer exists.")
        self._adopt(proj)
        self._set_busy(False)
        self.log.append("Loaded project %s (%d lines)." % (path, len(proj.lines)))
        self.app.set_status("project loaded: %d lines" % len(proj.lines))

    def _adopt(self, proj):
        self.app.set_project(proj, self.project_path)
        self._summary()
        self._update_buttons()

    def save_as(self):
        if not self.project:
            return
        f = filedialog.asksaveasfilename(
            title="Save project as", defaultextension=".json",
            initialfile="project.json", initialdir=os.path.dirname(self.project_path or "") or None,
            filetypes=[("Project", "*.json")])
        if not f:
            return
        self.project_path = os.path.normpath(f)
        self.v_proj.set(self.project_path)
        self.app.project_path = self.project_path
        self.save_project()

    def save_project(self):
        p = self.project
        if not p or not self.project_path:
            return False
        try:
            os.makedirs(os.path.dirname(self.project_path), exist_ok=True)
            p.save(self.project_path)
        except Exception as e:
            self.app.set_status("save failed: %r" % e)
            messagebox.showerror("Save failed", str(e), parent=self)
            return False
        self.app.set_status("project saved")
        self._summary()
        return True

    # ---------------------------------------------------------------- extract
    def extract(self):
        if self.busy or not self.reader_cls or not self.game_root:
            return
        old = self.project
        self._set_busy(True)
        self.v_prog.set("extracting…")
        self.prog.config(mode="indeterminate")
        self.prog.start(12)
        self.app.run_thread(self._extract_worker, self.reader_cls, self.game_root,
                            self.project_path, old)

    def _extract_worker(self, cls, root, proj_path, old):
        try:
            reader = cls()
            lines = reader.extract(root)
            for fname, msg in getattr(reader, "warnings", []) or []:
                self.app.post("p_log", "warning %s: %s" % (fname, msg))
            kept = 0
            if old is not None:   # carry translations over by stable id
                prev = old.by_id()
                for l in lines:
                    o = prev.get(l.id)
                    if o is not None and o.source == l.source and (o.translation or o.status != "new"):
                        l.translation, l.status = o.translation, o.status
                        l.flagged, l.note = o.flagged, o.note
                        kept += 1
            meta = dict(old.meta) if old is not None else {}
            proj = project_mod.Project(root, cls.name, lines, meta)
            os.makedirs(os.path.dirname(proj_path), exist_ok=True)
            proj.save(proj_path)
            self.app.post("p_log", "Extracted %d lines (%d kept from the previous project)."
                          % (len(lines), kept))
            self.app.post("p_loaded", proj_path, proj)
        except Exception as e:
            self.app.post("p_error", "extract failed: %r" % e)

    # ---------------------------------------------------------------- translate / re-translate
    def _prepare_run(self):
        prov = self.app.default_provider()
        if not prov:
            messagebox.showwarning("No provider", "Add a provider on the Providers page first.",
                                   parent=self)
            return None
        if not prov.get("base_url") or not prov.get("model"):
            messagebox.showwarning("Provider incomplete",
                                   "'%s' needs a base URL and a model." % (prov.get("label") or prov["id"]),
                                   parent=self)
            return None
        return prov

    def translate(self):
        if self.busy or not self.project:
            return
        prov = self._prepare_run()
        if not prov:
            return
        cfgs = self.app.build_cfgs(prov)
        conc = int(prov.get("concurrency") or 1)
        proj, path = self.project, self.project_path
        self.scenes_total = sum(1 for _ in proj.scenes())
        flag = threading.Event()
        self.t0 = time.time()
        self.prog.config(mode="determinate", maximum=100, value=0)
        self.v_prog.set("starting… (concurrency %d, %s)" % (conc, prov.get("label") or prov["id"]))
        self._set_busy(True, flag)
        self.app.set_status("translating")
        self.log.append("Translate: provider '%s', concurrency %d, fallbacks %d"
                        % (prov.get("label") or prov["id"], conc, len(cfgs) - 1))
        self.app.run_thread(self._translate_worker, proj, cfgs, conc, path, flag)

    def _translate_worker(self, proj, cfgs, conc, path, flag):
        post = self.app.post
        s = self.app.settings
        try:
            summ = runner.run_project(
                proj, cfgs, core.load_glossary(), project_path=path, concurrency=conc,
                batch_size=s["batch_size"], context_k=s["context_window"],
                on_progress=lambda d, t, c: post("p_progress", d, t, dict(c)),
                on_line=lambda l: post("p_line", l.id), stop_flag=flag)
            post("p_done", "translate", summ)
        except Exception as e:
            post("p_error", "translate failed: %r" % e)

    def retranslate(self, line_ids):
        """Review-page action: re-translate the given line ids, one at a time."""
        if self.busy or not self.project or not line_ids:
            self.app.set_status("busy or nothing selected")
            return
        prov = self._prepare_run()
        if not prov:
            return
        cfgs = self.app.build_cfgs(prov)
        flag = threading.Event()
        ids = list(line_ids)
        self.t0 = time.time()
        self.prog.config(mode="indeterminate")
        self.prog.start(12)
        self.v_prog.set("re-translating %d line(s)…" % len(ids))
        self._set_busy(True, flag)
        self.app.run_thread(self._retranslate_worker, self.project, ids, cfgs,
                            self.project_path, flag)

    def _retranslate_worker(self, proj, ids, cfgs, path, flag):
        post = self.app.post
        try:
            res = runner.retranslate_lines(
                proj, ids, cfgs, core.load_glossary(), context_k=self.app.settings["context_window"],
                project_path=path, on_line=lambda l: post("p_line", l.id), stop_flag=flag)
            post("p_done", "retranslate", res)
        except Exception as e:
            post("p_error", "re-translate failed: %r" % e)

    def stop(self):
        if self.stop_flag is not None:
            self.stop_flag.set()
            self.btn_stop.config(state="disabled")
            self.app.set_status("stopping after the current batch…")

    def _on_progress(self, done, total, counts):
        if total:
            self.prog.config(maximum=total, value=done)
        el = time.time() - self.t0
        eta = ""
        if done and total and el > 0:
            eta = " · ETA %s" % fmt_eta(max(0, total - done) * el / done)
        self.v_prog.set(
            "scenes %d/%d · batches %d/%d · ok %d · kept %d · failed %d · skipped %d%s"
            % (counts.get("scenes_done", 0), self.scenes_total, done, total,
               counts.get("ok", 0), counts.get("kept", 0), counts.get("failed", 0),
               counts.get("skipped", 0), eta))

    def _on_line(self, line_id):
        self._line_ids.append(line_id)

    def _flush_lines(self):
        if self._line_ids:
            ids, self._line_ids = self._line_ids, []
            self.app.review_refresh(ids)

    def _on_done(self, kind, res):
        self._flush_lines()
        self.prog.stop()
        self.prog.config(mode="determinate", maximum=100, value=100)
        self._set_busy(False)
        self._summary()
        if kind == "translate":
            msg = ("%s · ok %d · kept %d · failed %d · skipped %d · protected %d · %s"
                   % ("STOPPED" if res.get("stopped") else "done", res["ok"], res["kept"],
                      res["failed"], res["skipped"], res["protected"],
                      fmt_eta(res.get("elapsed", 0))))
        elif kind == "retranslate":
            msg = ("re-translate %s · ok %d · kept %d · failed %d · skipped %d"
                   % ("STOPPED" if res.get("stopped") else "done", res["ok"], res["kept"],
                      res["failed"], res["skipped"]))
        elif kind == "patch":
            msg = "patch: %s, %d file(s)" % (res.get("status", "?"), len(res.get("files", [])))
        else:
            msg = "rolled back %d file(s)" % res
        self.v_prog.set(msg)
        self.log.append(msg)
        self.app.set_status(msg)

    def _on_error(self, msg):
        self.prog.stop()
        self.prog.config(mode="determinate", value=0)
        self._set_busy(False)
        self.v_prog.set(msg)
        self.log.append("ERROR: " + msg)
        self.app.set_status(msg)
        self._update_buttons()

    # ---------------------------------------------------------------- patch / rollback
    def backup_dir(self):
        b = (self.app.settings.get("backups_dir") or "").strip()
        if not b or not self.game_root:
            return None   # patcher default: <game>\_rpgt_backups
        tag = hashlib.sha1(os.path.abspath(self.game_root).lower().encode("utf-8")).hexdigest()[:6]
        return os.path.join(b, "%s-%s" % (os.path.basename(self.game_root.rstrip("\\/")) or "game", tag))

    def _shown_backup_dir(self):
        return self.backup_dir() or os.path.join(os.path.abspath(self.game_root), patcher.BACKUP_DIRNAME)

    def patch(self):
        if self.busy or not self.project or not self.reader_cls:
            return
        n = sum(1 for l in self.project.lines if l.translation)
        if not n:
            return
        if not messagebox.askokcancel(
                "Patch game",
                "Write %d translated line(s) into the game files?\n\n"
                "Game: %s\n\n"
                "Before anything is changed, every file that will be touched is copied to:\n%s\\<timestamp>\n"
                "with a manifest. Roll back restores them. The patch is verified first and "
                "aborts, leaving the game untouched, if it does not re-read cleanly."
                % (n, self.game_root, self._shown_backup_dir()), parent=self):
            return
        self.save_project()
        self._set_busy(True)
        self.prog.config(mode="indeterminate")
        self.prog.start(12)
        self.v_prog.set("patching…")
        self.app.run_thread(self._patch_worker, self.game_root, self.reader_cls,
                            list(self.project.lines), self.backup_dir())

    def _patch_worker(self, root, cls, lines, bdir):
        try:
            man = patcher.patch_game(root, cls, lines, backup_dir=bdir,
                                     on_log=lambda m: self.app.post("p_log", m))
            self.app.post("p_done", "patch", man)
        except Exception as e:
            self.app.post("p_error", "patch failed (game left as it was): %s" % e)

    def rollback(self):
        if self.busy or not self.game_root:
            return
        try:
            items = patcher.list_backups(self.game_root, self.backup_dir())
        except Exception as e:
            messagebox.showerror("Roll back", str(e), parent=self)
            return
        if not items:
            messagebox.showinfo("Roll back", "No backups found in:\n%s" % self._shown_backup_dir(),
                                parent=self)
            return
        man = self._pick_backup(items)
        if not man:
            return
        if not messagebox.askokcancel(
                "Roll back",
                "Restore %d file(s) from backup %s?\n\nThe patched files are replaced by the "
                "originals saved in:\n%s"
                % (len(man["files"]), man.get("created", "?"), man["backup_path"]), parent=self):
            return
        self._start_rollback(man, False)

    def _start_rollback(self, man, force):
        self._set_busy(True)
        self.prog.config(mode="indeterminate")
        self.prog.start(12)
        self.v_prog.set("rolling back…")
        self.app.run_thread(self._rollback_worker, self.game_root, man, force)

    def _rollback_worker(self, root, man, force):
        try:
            done = patcher.rollback(root, man, on_log=lambda m: self.app.post("p_log", m),
                                    force=force)
            self.app.post("p_done", "rollback", len(done))
        except patcher.RollbackRefused as e:
            self.app.post("p_rb_refused", str(e), man)
        except Exception as e:
            self.app.post("p_error", "roll back failed: %s" % e)

    def _on_rb_refused(self, msg, man):
        self.prog.stop()
        self._set_busy(False)
        self.v_prog.set("roll back refused")
        self.log.append("Roll back refused: " + msg)
        if "changed since patch" in msg and messagebox.askyesno(
                "Roll back", "%s\n\nOverwrite those files anyway?" % msg, parent=self):
            self._start_rollback(man, True)
        elif "changed since patch" not in msg:
            messagebox.showinfo("Roll back", msg, parent=self)

    def _pick_backup(self, items):
        """Modal list of backups; returns the chosen manifest dict or None."""
        dlg = tk.Toplevel(self)
        dlg.title("Roll back")
        dlg.transient(self.winfo_toplevel())
        dlg.configure(background=self.app.palette["bg"])
        dlg.columnconfigure(0, weight=1)
        dlg.rowconfigure(1, weight=1)
        ttk.Label(dlg, text="Choose a backup to restore", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        fr, tree = make_table(dlg, [("stamp", "Created", 140, "w"), ("status", "Status", 110, "w"),
                                    ("files", "Files", 50, "e"), ("path", "Folder", 300, "w")],
                              height=8)
        fr.grid(row=1, column=0, sticky="nsew", padx=12)
        for i, m in enumerate(items):
            tree.insert("", "end", iid=str(i), values=(
                m.get("created", ""), m.get("status", ""), len(m.get("files", [])),
                m.get("backup_path", "")))
        tree.selection_set("0")
        out = {"m": None}

        def ok(_e=None):
            sel = tree.selection()
            if sel:
                out["m"] = items[int(sel[0])]
            dlg.destroy()
        bar = ttk.Frame(dlg)
        bar.grid(row=2, column=0, sticky="e", padx=12, pady=12)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(bar, text="Roll back…", style="Accent.TButton", command=ok).pack(
            side="right", padx=(0, 6))
        tree.bind("<Double-1>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()
        tree.focus_set()
        self.wait_window(dlg)
        return out["m"]
