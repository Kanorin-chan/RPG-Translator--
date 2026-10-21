#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPG Maker XP / VX / VX Ace reader (Ruby Marshal .rxdata/.rvdata/.rvdata2).

Event-command codes match the MV reader: 101 header + 401 lines are ONE dialogue
Line joined with "\\n"; 102 choices; 105/405 scroll text (VX/Ace only). XP differs:
its 101 carries the first text line itself. Script (355/655) and comment (108/408)
commands and Scripts.* are never read. Class/field names follow the RPG Maker
Ruby class layout (RPG::Map, RPG::Event, RPG::EventCommand ...).

Strings without an encoding ivar are Shift-JIS (cp932) in XP/VX. On write we keep
a string's own encoding when the translation fits it; otherwise that one string
becomes UTF-8 with the E=true ivar. (XP/VX engines may mis-draw such UTF-8 text.)
"""

import codecs
import os
import re

from project import Line
from readers import rubymarshal as rm
from readers.base import PatchError, Reader as _Base

_EXTS = (".rvdata2", ".rvdata", ".rxdata")
_ARCHIVES = ("game.rgssad", "game.rgss2a", "game.rgss3a")
_DB = ("Actors", "Classes", "Skills", "Items", "Weapons", "Armors", "Enemies", "States")
_DB_FIELDS = (("@name", "name"), ("@nickname", "name"), ("@description", "desc"),
              ("@message1", "term"), ("@message2", "term"), ("@message3", "term"),
              ("@message4", "term"))
_SYS_STR = ("@game_title", "@currency_unit")
_SYS_TREE = ("@elements", "@skill_types", "@weapon_types", "@armor_types",
             "@terms", "@words")


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _listdir(d):
    try:
        return {n.lower(): n for n in os.listdir(d)}
    except OSError:
        return {}


def _find_data(root):
    """(data_dir, ext, {lowername: realname}) or None."""
    cands = []
    top = _listdir(root)
    if "data" in top and os.path.isdir(os.path.join(root, top["data"])):
        cands.append(os.path.join(root, top["data"]))
    cands.append(root)
    for d in cands:
        names = _listdir(d)
        for ext in _EXTS:
            if all(s + ext in names for s in ("map001", "system", "mapinfos")):
                return d, ext, names
    return None


def archive_status(root):
    """'archive-encrypted' when only an RGSS archive exists (we never decrypt)."""
    if _find_data(root):
        return None
    names = _listdir(root)
    return "archive-encrypted" if any(a in names for a in _ARCHIVES) else None


# --- string codec policy -------------------------------------------------

def _codec(rs):
    e = rs.encoding_name
    if e is None:
        return "cp932"
    if e == "utf-8":
        return "utf-8"
    if e == "us-ascii":
        return "ascii"
    try:
        return codecs.lookup(e.lower().replace("-", "_")).name
    except LookupError:
        return "utf-8"


def _decode(rs):
    """Text of an RString, or None when it is not valid text."""
    for c in (_codec(rs), "utf-8"):
        try:
            return rs.raw.decode(c)
        except UnicodeDecodeError:
            pass
    return None


def _put(rs, text):
    try:
        rs.set_bytes(text.encode(_codec(rs)))
    except UnicodeEncodeError:
        rs.set_bytes(text.encode("utf-8"))
        rs.mark_utf8()


def _new_str(tmpl, text):
    rs = rm.RString(tmpl.raw, dict(tmpl.ivars) if tmpl.ivars is not None else None)
    _put(rs, text)
    return rs


# --- navigation ----------------------------------------------------------

def _step(o, s):
    if isinstance(s, dict):
        return o.get(s["h"])
    if isinstance(s, int):
        return o[s]
    if isinstance(o, rm.RObject):
        return o.ivars[s]
    raise KeyError(s)


def _walk(o, path):
    try:
        for s in path:
            o = _step(o, s)
    except (KeyError, IndexError, TypeError, AttributeError):
        raise PatchError("locator path no longer exists: %r" % (path,))
    return o


def _pid(path):
    return "/".join(str(s["h"]) if isinstance(s, dict) else str(s).lstrip("@")
                    for s in path)


def _code(cmd):
    return cmd.ivars["@code"]


def _p0(cmd):
    return cmd.ivars["@parameters"][0]


class Reader(_Base):
    name = "rpgmruby"
    label = "RPG Maker XP/VX/VX Ace"

    @classmethod
    def detect(cls, root):
        return _find_data(root) is not None

    # ------------------------------------------------------------ extract
    def extract(self, root):
        found = _find_data(root)
        if not found:
            raise FileNotFoundError("no RPG Maker XP/VX/Ace Data folder in %s" % root)
        ddir, ext, names = found
        self._xp = ext == ".rxdata"
        out = []
        self._emit = _Emitter(root, ddir, out)
        for mid, fname in self._map_files(ddir, ext, names):
            self._map(fname, mid, names, ddir)
        self._need("CommonEvents", ext, names, ddir, self._common)
        self._need("Troops", ext, names, ddir, self._troops)
        for db in _DB:
            self._need(db, ext, names, ddir, self._database)
        self._need("System", ext, names, ddir, self._system)
        return out

    def _need(self, stem, ext, names, ddir, fn):
        real = names.get(stem.lower() + ext)
        if real:
            fn(real, stem, ddir)

    def _map_files(self, ddir, ext, names):
        have = {}
        for low, real in names.items():
            m = re.fullmatch(r"map(\d+)" + re.escape(ext), low)
            if m:
                have[int(m.group(1))] = real
        order, seen = [], set()
        info = rm.loads(_read(os.path.join(ddir, names["mapinfos" + ext])))
        kids = {}
        for k, v in info:
            if isinstance(v, rm.RObject):
                kids.setdefault(v.ivars.get("@parent_id", 0), []).append(
                    (v.ivars.get("@order", 0), k))

        def dfs(pid):
            for _, mid in sorted(kids.get(pid, [])):
                if mid in seen:
                    continue
                seen.add(mid)
                if mid in have:
                    order.append((mid, have[mid]))
                dfs(mid)
        dfs(0)
        for mid in sorted(set(have) - seen):
            order.append((mid, have[mid]))
        return order

    def _load(self, ddir, fname):
        return rm.loads(_read(os.path.join(ddir, fname)))

    def _map(self, fname, mid, names, ddir):
        e = self._emit
        e.begin(fname, ddir)
        m = self._load(ddir, fname)
        scene = "Map%03d" % mid
        dn = m.ivars.get("@display_name")
        if isinstance(dn, rm.RString):
            e.string(dn, "name", scene, ["@display_name"])
        for eid, ev in m.ivars["@events"]:
            sc = "%s/EV%03d" % (scene, eid)
            for pi, pg in enumerate(ev.ivars["@pages"]):
                self._cmds(pg.ivars["@list"], ["@events", {"h": eid}, "@pages", pi, "@list"], sc)

    def _cmds(self, lst, lpath, scene):
        e, xp = self._emit, self._xp
        i, n = 0, len(lst)
        while i < n:
            c = _code(lst[i])
            if c in (101, 105) and not (xp and c == 105):
                j = i + 1
                body = 401 if c == 101 else 405
                while j < n and _code(lst[j]) == body:
                    j += 1
                if xp and c == 101:
                    e.block(lst, lpath, scene, i, j - i, True, 401)
                elif j > i + 1:
                    e.block(lst, lpath, scene, i + 1, j - i - 1, False, body)
                i = j
            elif c in (401, 405) and not (xp and c == 405):
                j = i + 1
                while j < n and _code(lst[j]) == c:
                    j += 1
                e.block(lst, lpath, scene, i, j - i, False, c)
                i = j
            elif c == 102:
                ch = _p0(lst[i])
                if isinstance(ch, rm.RArray):
                    for k, s in enumerate(ch):
                        if isinstance(s, rm.RString):
                            e.string(s, "choice", scene, lpath + [i, "@parameters", 0, k],
                                     idsuffix="c%d.%d" % (i, k), lpath=lpath)
                i += 1
            else:
                i += 1

    def _common(self, fname, stem, ddir):
        self._emit.begin(fname, ddir)
        for i, ce in enumerate(self._load(ddir, fname)):
            if ce is not None:
                self._cmds(ce.ivars["@list"], [i, "@list"], "CommonEvents/%04d" % i)

    def _troops(self, fname, stem, ddir):
        self._emit.begin(fname, ddir)
        for i, tr in enumerate(self._load(ddir, fname)):
            if tr is None:
                continue
            for pi, pg in enumerate(tr.ivars.get("@pages") or []):
                self._cmds(pg.ivars["@list"], [i, "@pages", pi, "@list"],
                           "Troops/%04d" % i)

    def _database(self, fname, stem, ddir):
        e = self._emit
        e.begin(fname, ddir)
        for i, o in enumerate(self._load(ddir, fname)):
            if o is None:
                continue
            for key, kind in _DB_FIELDS:
                s = o.ivars.get(key)
                if isinstance(s, rm.RString):
                    e.string(s, kind, stem, [i, key])

    def _system(self, fname, stem, ddir):
        e = self._emit
        e.begin(fname, ddir)
        sysobj = self._load(ddir, fname)
        for key in _SYS_STR:
            s = sysobj.ivars.get(key)
            if isinstance(s, rm.RString):
                e.string(s, "term", "System", [key])

        def walk(o, path):
            if isinstance(o, rm.RString):
                e.string(o, "term", "System", path)
            elif isinstance(o, rm.RArray):
                for i, v in enumerate(o):
                    walk(v, path + [i])
            elif isinstance(o, rm.RObject):
                for k, v in o.ivars.items():
                    walk(v, path + [str(k)])
        for key in _SYS_TREE:
            if key in sysobj.ivars:
                walk(sysobj.ivars[key], [key])

    # -------------------------------------------------------------- apply
    def apply(self, root, out_root, lines):
        by_file = {}
        for ln in lines:
            loc = ln.locator or {}
            if "file" not in loc or not ln.translation or ln.translation == ln.source:
                continue
            by_file.setdefault(loc["file"], []).append(ln)
        done = {}
        for rel, lns in by_file.items():
            src = os.path.join(root, *rel.split("/"))
            try:
                raw = _read(src)
            except OSError as ex:
                raise PatchError("cannot read %s: %s" % (rel, ex))
            data = rm.loads(raw)
            if rm.dumps(data) != raw:
                raise PatchError("%s does not re-dump byte-identically; refusing" % rel)
            xp = rel.lower().endswith(".rxdata")
            # descending idx per list so block re-splits never shift pending edits
            lns.sort(key=lambda l: (_pid(l.locator["path"]), l.locator.get("idx", 0)),
                     reverse=True)
            for ln in lns:
                self._apply_one(data, ln, xp)
            done[rel] = rm.dumps(data)
        for rel, blob in done.items():
            dst = os.path.join(out_root, *rel.split("/"))
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            with open(dst, "wb") as f:
                f.write(blob)
        return list(done)

    def _apply_one(self, data, ln, xp):
        loc, tr = ln.locator, ln.translation
        if loc.get("t") == "str":
            rs = _walk(data, loc["path"])
            if not isinstance(rs, rm.RString) or _decode(rs) != loc["src"]:
                raise PatchError("stale locator: %s" % ln.id)
            _put(rs, tr)
            return
        lst = _walk(data, loc["path"])
        if not isinstance(lst, rm.RArray):
            raise PatchError("stale locator: %s" % ln.id)
        if loc["t"] == "choice":
            ch = lst[loc["idx"]] if loc["idx"] < len(lst) else None
            try:
                rs = ch.ivars["@parameters"][0][loc["ch"]]
                ok = _code(ch) == 102 and _decode(rs) == loc["src"]
            except (AttributeError, KeyError, IndexError, TypeError):
                ok = False
            if not ok:
                raise PatchError("stale locator: %s" % ln.id)
            _put(rs, tr)
            return
        idx, n = loc["idx"], loc["n"]
        old = list(lst[idx:idx + n])
        head = loc["head101"]
        want = [101] + [401] * (n - 1) if head else [loc["code"]] * n
        if len(old) != n or [_code(c) for c in old] != want:
            raise PatchError("stale locator: %s" % ln.id)
        texts = [_p0(c) for c in old]
        if not all(isinstance(t, rm.RString) for t in texts) or \
                "\n".join(_decode(t) or "" for t in texts) != loc["src"]:
            raise PatchError("stale locator: %s" % ln.id)
        parts = tr.split("\n")
        new = []
        for k, part in enumerate(parts):
            if k < n:
                cmd = old[k]
                _put(_p0(cmd), part)
            else:
                tmpl = old[-1]
                iv = dict(tmpl.ivars)
                iv["@code"] = loc["code"]
                p = rm.RArray([_new_str(_p0(tmpl), part)])
                iv["@parameters"] = p
                cmd = rm.RObject(tmpl.cls, iv)
            new.append(cmd)
        lst[idx:idx + n] = new


class _Emitter:
    def __init__(self, root, ddir, out):
        self.out = out
        self.reldir = os.path.relpath(ddir, root).replace("\\", "/")
        self.rel = ""

    def begin(self, fname, ddir):
        d = "" if self.reldir == "." else self.reldir + "/"
        self.rel = d + fname

    def _add(self, kind, scene, source, idpath, loc):
        loc["file"] = self.rel
        self.out.append(Line(id="%s#%s" % (self.rel, idpath), file=self.rel,
                             scene=scene, order=len(self.out), kind=kind,
                             source=source, locator=loc))

    def string(self, rs, kind, scene, path, idsuffix=None, lpath=None):
        text = _decode(rs)
        if text is None or not text.strip():
            return
        if idsuffix is None:
            self._add(kind, scene, text, _pid(path), {"t": "str", "path": path, "src": text})
        else:
            self._add(kind, scene, text, _pid(lpath) + "/" + idsuffix,
                      {"t": "choice", "path": lpath, "idx": path[len(lpath)],
                       "ch": path[-1], "src": text})

    def block(self, lst, lpath, scene, idx, n, head101, code):
        parts = [_decode(_p0(c)) if isinstance(_p0(c), rm.RString) else None
                 for c in lst[idx:idx + n]]
        if any(p is None for p in parts):
            return
        text = "\n".join(parts)
        if not text.strip():
            return
        self._add("dialogue", scene, text, "%s/c%d" % (_pid(lpath), idx),
                  {"t": "block", "path": lpath, "idx": idx, "n": n,
                   "head101": head101, "code": code, "src": text})


READER = Reader
