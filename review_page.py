# -*- coding: utf-8 -*-
"""Review page: filterable table of every line + detail/edit panel (docs sec. 10/11).

ReviewPage(parent, get_project, actions). `actions` is supplied by the host:
  actions["retranslate"](line_ids)  host runs runner.retranslate_lines in a thread,
                                    then calls page.refresh_lines(ids)
  actions["save"]()                 persist the project
  actions["status"](text)           status-bar message
Uses only ttk style names (works under theme.py's dark theme or plain ttk).
Stdlib only.
"""
import tkinter as tk
from tkinter import ttk

CHUNK = 500            # rows inserted per after() tick
MAX_CELL = 300         # chars of the first line shown in a cell
STATUS_FILTERS = ("all", "flagged", "kept", "ok", "new", "edited", "skipped")
COLUMNS = (("n", "#", 56, "e", False), ("scene", "scene", 110, "w", False),
           ("speaker", "speaker", 90, "w", False), ("source", "source", 300, "w", True),
           ("translation", "translation", 300, "w", True),
           ("status", "status", 70, "w", False))


def _cell(s):
    """First line of s, with an ellipsis when it was cut."""
    lines = s.splitlines() or [""]
    first = lines[0]
    if len(first) > MAX_CELL:
        return first[:MAX_CELL] + "…"
    return first + "…" if len(lines) > 1 else first


class ReviewPage(ttk.Frame):
    def __init__(self, parent, get_project, actions):
        super().__init__(parent)
        self.get_project = get_project
        self.actions = actions
        self._lines = []         # Line objects in play order
        self._idx = {}           # line id -> index into _lines
        self._low = []           # lowercase "source\ntranslation" per line
        self._scene_low = []     # lowercase scene per line
        self._visible = []       # indices currently matching the filter
        self._prev = {}          # line id -> (translation, status) before last edit
        self._gen = 0            # bumps to cancel a stale chunked fill
        self._job = None         # pending chunk after-id
        self._search_job = None
        self._ctx_job = None
        self.loading = False
        self._cur = None         # index of the line shown in the detail panel

        self.v_status = tk.StringVar(value="all")
        self.v_scene = tk.StringVar()
        self.v_text = tk.StringVar()
        self.v_k = tk.IntVar(value=4)
        self.v_count = tk.StringVar(value="0 of 0 lines")
        self.v_head = tk.StringVar(value="No line selected")

        self._build()
        self._init_colors()
        self.bind("<<ThemeChanged>>", lambda e: self._init_colors())

    # ---- layout ----
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Status").pack(side="left")
        self.cb_status = ttk.Combobox(bar, textvariable=self.v_status, width=9,
                                      values=STATUS_FILTERS, state="readonly")
        self.cb_status.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Scene").pack(side="left")
        self.cb_scene = ttk.Combobox(bar, textvariable=self.v_scene, width=18)
        self.cb_scene.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Search").pack(side="left")
        self.ent_search = ttk.Entry(bar, textvariable=self.v_text, width=28)
        self.ent_search.pack(side="left", padx=(4, 12))
        ttk.Label(bar, textvariable=self.v_count).pack(side="right")
        self.cb_status.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())
        self.cb_scene.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())
        self.v_scene.trace_add("write", lambda *a: self._debounce())
        self.v_text.trace_add("write", lambda *a: self._debounce())

        self.pane = ttk.PanedWindow(self, orient="vertical")
        self.pane.grid(row=1, column=0, sticky="nsew", padx=8)

        top = ttk.Frame(self.pane)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(top, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="extended", height=12)
        for cid, title, w, anchor, stretch in COLUMNS:
            self.tree.heading(cid, text=title, anchor="w")
            self.tree.column(cid, width=w, anchor=anchor, stretch=stretch,
                             minwidth=40)
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.pane.add(top, weight=3)

        det = ttk.Frame(self.pane, padding=(0, 6, 0, 0))
        det.columnconfigure((0, 1, 2), weight=1, uniform="d")
        det.rowconfigure(2, weight=1)
        self.pane.add(det, weight=2)
        ttk.Label(det, textvariable=self.v_head).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ctl = ttk.Frame(det)
        ctl.grid(row=0, column=1, columnspan=2, sticky="e")
        self.btn_save = ttk.Button(ctl, text="Save edit", command=self.save_edit)
        self.btn_revert = ttk.Button(ctl, text="Revert line", command=self.revert_line)
        ttk.Label(ctl, text="Context lines").pack(side="left")
        self.sp_k = ttk.Spinbox(ctl, from_=0, to=20, width=3, textvariable=self.v_k,
                                command=self._show_detail)
        self.sp_k.pack(side="left", padx=(4, 16))
        self.sp_k.bind("<Return>", lambda e: self._show_detail())
        self.btn_save.pack(side="left", padx=(0, 6))
        self.btn_revert.pack(side="left")

        self.t_ctx = self._text(det, 2, 0, "Context (previous lines)", False)
        self.t_src = self._text(det, 2, 1, "Source", False)
        self.t_tr = self._text(det, 2, 2, "Translation", True)

        btns = ttk.Frame(self, padding=(8, 6, 8, 8))
        btns.grid(row=2, column=0, sticky="ew")
        self.btn_rt = ttk.Button(btns, text="Re-translate selected",
                                 command=self.retranslate_selected)
        self.btn_rt.pack(side="left")
        ttk.Button(btns, text="Flag / Unflag selected",
                   command=self.toggle_flag).pack(side="left", padx=6)
        ttk.Button(btns, text="Select all",
                   command=self.select_all).pack(side="left")

        for w in (self.tree, self.t_tr, self.t_src, self.t_ctx, self.ent_search,
                  self.cb_scene, self.cb_status):
            w.bind("<Control-f>", self._focus_search)
            w.bind("<Control-F>", self._focus_search)
        for w in (self.tree, self.t_tr):
            w.bind("<Control-Return>", self._on_save_key)
            w.bind("<Control-r>", self._on_rt_key)
            w.bind("<Control-R>", self._on_rt_key)
        self.tree.bind("<Control-a>", self._on_select_all_key)
        self.tree.bind("<Control-A>", self._on_select_all_key)
        self.tree.bind("<f>", self._on_flag_key)
        self.tree.bind("<F>", self._on_flag_key)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def _text(self, parent, row, col, title, editable):
        f = ttk.Frame(parent)
        f.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text=title).grid(row=0, column=0, sticky="w")
        t = tk.Text(f, height=6, wrap="word", font="TkDefaultFont", undo=editable,
                    borderwidth=1, relief="solid", highlightthickness=0,
                    padx=6, pady=4)
        sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns")
        t._editable = editable
        if not editable:
            t.configure(state="disabled")
        return t

    # ---- colours (works for dark and light base themes) ----
    def _init_colors(self):
        st = ttk.Style(self)
        bg = st.lookup("TFrame", "background") or "#ffffff"
        try:
            r, g, b = (v / 65535 for v in self.winfo_rgb(bg))
        except tk.TclError:
            r = g = b = 1.0
        dark = (0.299 * r + 0.587 * g + 0.114 * b) < 0.5
        if dark:
            c = {"kept": "#4a3d1a", "flagged": "#4d2626", "edited": "#1f3a55",
                 "skipped": "#80848f"}
            field, fg = "#2a2b34", "#e6e6ea"
        else:
            c = {"kept": "#fff1c2", "flagged": "#f8d7d4", "edited": "#d9e8fb",
                 "skipped": "#80848f"}
            field, fg = "#ffffff", "#1a1a1a"
        # prefer the theme's own entry colours so Text matches the Entry widgets
        field = st.lookup("TEntry", "fieldbackground") or field
        fg = st.lookup("TEntry", "foreground") or fg
        t = self.tree
        t.tag_configure("kept", background=c["kept"])
        t.tag_configure("flagged", background=c["flagged"])
        t.tag_configure("edited", background=c["edited"])
        t.tag_configure("skipped", foreground=c["skipped"])
        for w in (self.t_ctx, self.t_src, self.t_tr):
            try:
                w.configure(background=field, foreground=fg, insertbackground=fg,
                            selectbackground="#3d6fb0", selectforeground="#ffffff")
            except tk.TclError:
                pass
        try:
            self.t_ctx.configure(foreground=st.lookup("TLabel", "foreground") or fg)
        except tk.TclError:
            pass

    def destroy(self):
        # cancel pending after() callbacks so they do not fire on a dead widget
        for j in (self._job, self._search_job, self._ctx_job):
            if j:
                try:
                    self.after_cancel(j)
                except tk.TclError:
                    pass
        self._gen += 1
        super().destroy()

    # ---- public API ----
    def load(self, project):
        """(Re)populate from `project` (None clears)."""
        self._prev.clear()
        self._cur = None
        if project is None:
            self._lines = []
        else:
            self._lines = sorted(project.lines, key=lambda l: l.order)
        self._idx = {l.id: i for i, l in enumerate(self._lines)}
        self._low = [(l.source + "\n" + l.translation).lower() for l in self._lines]
        self._scene_low = [l.scene.lower() for l in self._lines]
        scenes, seen = [], set()
        for l in self._lines:
            if l.scene not in seen:
                seen.add(l.scene)
                scenes.append(l.scene)
        self.cb_scene.configure(values=[""] + scenes)
        self._apply_filter()
        self._show_detail()

    def set_filter(self, status=None, scene=None, text=None):
        """Set any of the filters and apply immediately."""
        if self._search_job:
            self.after_cancel(self._search_job)
            self._search_job = None
        if status is not None:
            self.v_status.set(status)
        if scene is not None:
            self.v_scene.set(scene)
        if text is not None:
            self.v_text.set(text)
        if self._search_job:   # the var traces re-armed the debounce
            self.after_cancel(self._search_job)
            self._search_job = None
        self._apply_filter()

    def selected_ids(self):
        """Line ids of the selected rows, in play order."""
        idxs = sorted(int(i[1:]) for i in self.tree.selection())
        return [self._lines[i].id for i in idxs]

    def refresh_lines(self, ids):
        """Update only the rows of `ids` (and the detail panel if shown)."""
        touched_cur = False
        for lid in ids:
            i = self._idx.get(lid)
            if i is None:
                continue
            l = self._lines[i]
            self._low[i] = (l.source + "\n" + l.translation).lower()
            iid = "r%d" % i
            if self.tree.exists(iid):
                self.tree.item(iid, values=self._values(i), tags=self._tags(l))
            if i == self._cur:
                touched_cur = True
        if touched_cur:
            self._show_detail()

    # ---- rows ----
    @staticmethod
    def _shown_status(l):
        return "flagged" if l.flagged else l.status

    def _values(self, i):
        l = self._lines[i]
        return (l.order + 1, l.scene, l.speaker, _cell(l.source),
                _cell(l.translation), self._shown_status(l))

    def _tags(self, l):
        if l.flagged:
            return ("flagged",)
        return (l.status,) if l.status in ("kept", "edited", "skipped") else ()

    # ---- filtering + chunked fill ----
    def _debounce(self):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(200, self._apply_filter)

    def _apply_filter(self):
        self._search_job = None
        st = self.v_status.get()
        sc = self.v_scene.get().strip().lower()
        tx = self.v_text.get().strip().lower()
        lines, low, slow = self._lines, self._low, self._scene_low
        vis = []
        for i in range(len(lines)):
            l = lines[i]
            if st != "all":
                if st == "flagged":
                    if not l.flagged:
                        continue
                elif l.status != st:
                    continue
            if sc and sc not in slow[i]:
                continue
            if tx and tx not in low[i]:
                continue
            vis.append(i)
        self._visible = vis
        self.v_count.set("%d of %d lines" % (len(vis), len(lines)))
        self._fill()

    def _fill(self):
        self._gen += 1
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        kids = self.tree.get_children()
        if kids:
            self.tree.delete(*kids)
        self.loading = bool(self._visible)
        self._chunk(self._gen, 0)

    def _chunk(self, gen, pos):
        if gen != self._gen:
            return
        vis = self._visible
        end = min(pos + CHUNK, len(vis))
        ins, tags, vals = self.tree.insert, self._tags, self._values
        for i in vis[pos:end]:
            ins("", "end", iid="r%d" % i, values=vals(i), tags=tags(self._lines[i]))
        if end < len(vis):
            self._job = self.after(1, self._chunk, gen, end)
        else:
            self._job = None
            self.loading = False

    # ---- selection + detail ----
    def _on_select(self, _e=None):
        if self._ctx_job:
            self.after_cancel(self._ctx_job)
        self._ctx_job = self.after(60, self._show_detail)

    def _set_text(self, w, s):
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", s)
        if not w._editable:
            w.configure(state="disabled")

    def _show_detail(self):
        self._ctx_job = None
        sel = self.tree.selection()
        editable = len(sel) == 1
        if len(sel) != 1:
            self._cur = None
            self.v_head.set("No line selected" if not sel
                            else "%d lines selected" % len(sel))
            for w in (self.t_ctx, self.t_src, self.t_tr):
                self._set_text(w, "")
            self.t_tr.configure(state="disabled")
        else:
            i = int(sel[0][1:])
            l = self._lines[i]
            self._cur = i
            self.v_head.set("#%d   %s   %s   [%s]" % (
                l.order + 1, l.scene, l.speaker or "-", self._shown_status(l)))
            self._set_text(self.t_src, l.source)
            self._set_text(self.t_tr, l.translation)
            self._set_text(self.t_ctx, self._context_text(l))
        for b in (self.btn_save, self.btn_revert):
            b.state(["!disabled"] if editable else ["disabled"])

    def _context_text(self, line):
        p = self.get_project()
        try:
            k = max(0, int(self.v_k.get()))
        except (tk.TclError, ValueError):
            k = 0
        if p is None or k == 0:
            return ""
        pairs = p.context_before(line, k)
        return "\n".join("%s\n  -> %s" % (s, t) for s, t in pairs)

    # ---- actions ----
    def _status(self, msg):
        f = self.actions.get("status")
        if f:
            f(msg)

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def toggle_flag(self):
        ids = self.selected_ids()
        if not ids:
            self._status("Nothing selected")
            return
        lines = [self._lines[self._idx[i]] for i in ids]
        new = not all(l.flagged for l in lines)   # any unflagged -> flag all
        for l in lines:
            l.flagged = new
        self.refresh_lines(ids)
        self._status("%s %d line(s)" % ("Flagged" if new else "Unflagged", len(ids)))
        if self.actions.get("save"):
            self.actions["save"]()

    def save_edit(self):
        if self._cur is None:
            return
        l = self._lines[self._cur]
        new = self.t_tr.get("1.0", "end-1c")
        if new == l.translation:
            self._status("No change")
            return
        self._prev[l.id] = (l.translation, l.status)
        l.translation, l.status = new, "edited"
        self.refresh_lines([l.id])
        self._status("Edited line #%d" % (l.order + 1))
        if self.actions.get("save"):
            self.actions["save"]()

    def revert_line(self):
        if self._cur is None:
            return
        l = self._lines[self._cur]
        prev = self._prev.pop(l.id, None)
        if prev is None:
            self._status("Nothing to revert for this line")
            return
        l.translation, l.status = prev
        self.refresh_lines([l.id])
        self._status("Reverted line #%d" % (l.order + 1))
        if self.actions.get("save"):
            self.actions["save"]()

    def retranslate_selected(self):
        ids = self.selected_ids()
        if not ids:
            self._status("Nothing selected")
            return
        self._status("Re-translating %d line(s)…" % len(ids))
        self.actions["retranslate"](ids)

    # ---- key handlers (return "break" so Text/Tree defaults do not also fire) ----
    def _focus_search(self, _e):
        self.ent_search.focus_set()
        self.ent_search.selection_range(0, "end")
        return "break"

    def _on_save_key(self, _e):
        self.save_edit()
        return "break"

    def _on_rt_key(self, _e):
        self.retranslate_selected()
        return "break"

    def _on_flag_key(self, _e):
        self.toggle_flag()
        return "break"

    def _on_select_all_key(self, _e):
        self.select_all()
        return "break"
