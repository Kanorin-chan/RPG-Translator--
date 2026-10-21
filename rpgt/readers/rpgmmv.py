#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPG Maker MV / MZ reader: data/*.json event commands + database + plugins.js."""

import json
import os
import re

from rpgt.project import Line
from rpgt.readers.base import PatchError, Reader as _Base

_CJK = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
_ASSET = re.compile(r"\.(png|jpe?g|webp|ogg|m4a|wav|mp3|json|js|webm|mp4)$", re.I)
_MAP = re.compile(r"^Map\d+\.json$")
_NAME_TAG = re.compile(r"\\N<([^>\n]*)>")  # name-window tag at the start of a message

# file -> [(field, kind)]
_DB = {
    "Actors.json": [("name", "name"), ("nickname", "name"), ("profile", "desc")],
    "Classes.json": [("name", "name")],
    "Skills.json": [("name", "name"), ("description", "desc"),
                    ("message1", "term"), ("message2", "term")],
    "Items.json": [("name", "name"), ("description", "desc")],
    "Weapons.json": [("name", "name"), ("description", "desc")],
    "Armors.json": [("name", "name"), ("description", "desc")],
    "Enemies.json": [("name", "name")],
    "States.json": [("name", "name")] + [("message%d" % n, "term") for n in (1, 2, 3, 4)],
}
_SYS_LISTS = ("armorTypes", "equipTypes", "skillTypes", "weaponTypes", "elementTypes")


# ---------- file io with byte-faithful re-encoding ----------

def _encode(data, st):
    asc = st["ascii"]
    if st["kind"] == "lines" and isinstance(data, list):
        s = "[\n" + ",\n".join(json.dumps(e, ensure_ascii=asc, separators=(",", ":"))
                               for e in data) + "\n]"
    elif st["kind"] in ("indent2", "indent4", "tab"):
        ind = {"indent2": 2, "indent4": 4, "tab": "\t"}[st["kind"]]
        s = json.dumps(data, ensure_ascii=asc, indent=ind)
    else:
        s = json.dumps(data, ensure_ascii=asc, separators=(",", ":"))
    if st["nl"] != "\n":
        s = s.replace("\n", st["nl"])  # raw newlines are structure only; string ones are escaped
    return s


def _rb(path):
    with open(path, "rb") as f:
        return f.read()


def _load(path):
    raw = _rb(path).decode("utf-8")
    bom = raw.startswith("﻿")
    if bom:
        raw = raw[1:]
    body = raw.rstrip(" \t\r\n")
    data = json.loads(body)
    nl = "\r\n" if "\r\n" in raw else "\n"
    st = {"bom": bom, "nl": nl, "trail": raw[len(body):], "ascii": False, "kind": "compact"}
    # pick the first style that reproduces the file exactly; else compact
    for asc in (False, True):
        for kind in ("compact", "lines", "indent2", "indent4", "tab"):
            cand = dict(st, ascii=asc, kind=kind)
            if _encode(data, cand) == body:
                return data, cand
    return data, st


def _save(path, data, st):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = ("﻿" if st["bom"] else "") + _encode(data, st) + st["trail"]
    with open(path, "wb") as f:
        f.write(s.encode("utf-8"))


# ---------- plugins.js span scanner (edits strings in place, rest untouched) ----------

def _ws(t, i):
    while t[i] in " \t\r\n":
        i += 1
    return i


def _walk(t, i, path, out):
    i = _ws(t, i)
    c = t[i]
    if c == '"':
        j = i + 1
        while t[j] != '"':
            j += 2 if t[j] == "\\" else 1
        out.append((path, i, j + 1))
        return j + 1
    if c in "[{":
        close = "]" if c == "[" else "}"
        i = _ws(t, i + 1)
        if t[i] == close:
            return i + 1
        n = 0
        while True:
            if c == "[":
                key = n
            else:
                j = i + 1
                while t[j] != '"':
                    j += 2 if t[j] == "\\" else 1
                key = json.loads(t[i:j + 1])
                i = _ws(t, j + 1)
                if t[i] != ":":
                    raise ValueError("expected ':' at %d" % i)
                i += 1
            i = _walk(t, i, path + [key], out)
            i = _ws(t, i)
            if t[i] == ",":
                i = _ws(t, i + 1)
                n += 1
                continue
            if t[i] == close:
                return i + 1
            raise ValueError("bad array/object at %d" % i)
    while t[i] not in ",]} \t\r\n":
        i += 1
    return i


def _scan_plugins(text):
    m = re.search(r"var\s+\$plugins\s*=\s*", text)
    if not m:
        raise ValueError("no $plugins")
    start = text.index("[", m.end())
    out = []
    _walk(text, start, [], out)
    return out


# ---------- the reader ----------

class Reader(_Base):
    name = "rpgmmv"
    label = "RPG Maker MV / MZ"

    @staticmethod
    def _data_rel(root):
        for rel in ("www/data", "data"):
            d = os.path.join(root, *rel.split("/"))
            if (os.path.isfile(os.path.join(d, "System.json"))
                    and os.path.isfile(os.path.join(d, "MapInfos.json"))):
                return rel
        return None

    @classmethod
    def detect(cls, root):
        return cls._data_rel(root) is not None

    # --- extract ---

    def extract(self, root):
        rel = self._data_rel(root)
        if rel is None:
            raise PatchError("not an RPG Maker MV/MZ folder: %s" % root)
        self._lines = []
        dd = os.path.join(root, *rel.split("/"))

        def has(n):
            return os.path.isfile(os.path.join(dd, n))

        self._system(root, rel, dd)
        for n in self._map_files(dd):
            self._map(rel, dd, n)
        if has("CommonEvents.json"):
            data, _ = _load(os.path.join(dd, "CommonEvents.json"))
            for i, ce in enumerate(data):
                if ce:
                    self._cmds(rel + "/CommonEvents.json", "CommonEvents:%s" % ce.get("id", i),
                               [i, "list"], ce.get("list") or [])
        if has("Troops.json"):
            data, _ = _load(os.path.join(dd, "Troops.json"))
            for i, tr in enumerate(data):
                for p, page in enumerate((tr or {}).get("pages") or []):
                    self._cmds(rel + "/Troops.json", "Troops:%s" % tr.get("id", i),
                               [i, "pages", p, "list"], page.get("list") or [])
        for fn, fields in _DB.items():
            if has(fn):
                data, _ = _load(os.path.join(dd, fn))
                for i, obj in enumerate(data):
                    for f, kind in fields:
                        v = (obj or {}).get(f)
                        if isinstance(v, str):
                            self._add(rel + "/" + fn, fn[:-5], kind, v, [i, f])
        self._plugins(root, rel)
        for n, l in enumerate(self._lines):
            l.order = n
        out, self._lines = self._lines, []
        return out

    def _map_files(self, dd):
        names = {n for n in os.listdir(dd) if _MAP.match(n)}
        order = []
        try:
            infos, _ = _load(os.path.join(dd, "MapInfos.json"))
            kids = {}
            for m in infos:
                if m:
                    kids.setdefault(m.get("parentId", 0), []).append(m)

            def dfs(pid, seen):
                for m in sorted(kids.get(pid, []), key=lambda m: (m.get("order", 0), m["id"])):
                    if m["id"] in seen:
                        continue
                    seen.add(m["id"])
                    order.append("Map%03d.json" % m["id"])
                    dfs(m["id"], seen)
            dfs(0, set())
        except (ValueError, KeyError, TypeError):
            order = []
        res = [n for n in order if n in names]
        return res + sorted(names - set(res))

    def _add(self, file, scene, kind, source, path, speaker="", **loc):
        if not source.strip():
            return None
        line = Line(id="%s#%s" % (file, "/".join(str(p) for p in path)), file=file,
                    scene=scene, order=0, kind=kind, source=source, speaker=speaker,
                    locator=dict(path=list(path), **loc))
        self._lines.append(line)
        return line

    def _system(self, root, rel, dd):
        f = rel + "/System.json"
        s, _ = _load(os.path.join(dd, "System.json"))
        for k, kind in (("gameTitle", "name"), ("currencyUnit", "term")):
            if isinstance(s.get(k), str):
                self._add(f, "System", kind, s[k], [k])
        for k in _SYS_LISTS:
            for i, v in enumerate(s.get(k) or []):
                if isinstance(v, str):
                    self._add(f, "System", "term", v, [k, i])
        t = s.get("terms") or {}
        for k, v in t.items():
            if isinstance(v, list):
                for i, x in enumerate(v):
                    if isinstance(x, str):
                        self._add(f, "System", "term", x, ["terms", k, i])
            elif isinstance(v, dict):
                for mk, x in v.items():
                    if isinstance(x, str):
                        self._add(f, "System", "term", x, ["terms", k, mk])

    def _map(self, rel, dd, n):
        f = rel + "/" + n
        m, _ = _load(os.path.join(dd, n))
        if isinstance(m.get("displayName"), str):
            self._add(f, n[:-5], "name", m["displayName"], ["displayName"])
        for i, ev in enumerate(m.get("events") or []):
            if not ev:
                continue
            for p, page in enumerate(ev.get("pages") or []):
                self._cmds(f, "%s:ev%s" % (n[:-5], ev.get("id", i)),
                           ["events", i, "pages", p, "list"], page.get("list") or [])

    def _cmds(self, f, scene, lpath, cmds):
        i = 0
        while i < len(cmds):
            c = cmds[i]
            code = c.get("code") if isinstance(c, dict) else None
            params = c.get("parameters") or [] if isinstance(c, dict) else []
            if code in (101, 105):
                cont = 401 if code == 101 else 405
                j, texts = i + 1, []
                while (j < len(cmds) and cmds[j].get("code") == cont
                       and cmds[j].get("parameters")
                       and isinstance(cmds[j]["parameters"][0], str)):
                    texts.append(cmds[j]["parameters"][0])
                    j += 1
                spk = params[4] if code == 101 and len(params) > 4 and isinstance(params[4], str) else ""
                text, tag = "\n".join(texts), ""
                m = _NAME_TAG.match(text) if code == 101 else None
                if m and m.group(1).strip() and text[m.end():].strip():
                    tag, spk, text = m.group(0), m.group(1), text[m.end():]  # tag stays out of the source
                extra = {"tag": tag} if tag else {}
                ln = self._add(f, scene, "dialogue", text, lpath + [i], speaker=spk,
                               list=lpath, start=i, head=code, cont=cont, n=len(texts), **extra)
                if ln and code == 105:  # scroll text is on-screen but the spec says skip
                    ln.status, ln.note = "skipped", "scroll text (105/405)"
                i = j
                continue
            if code == 102 and params and isinstance(params[0], list):
                for k, ch in enumerate(params[0]):
                    if isinstance(ch, str):
                        self._add(f, scene, "choice", ch, lpath + [i, "parameters", 0, k])
            elif code == 357 and len(params) > 3 and isinstance(params[3], dict):
                for k, v in params[3].items():
                    if isinstance(v, str) and _CJK.search(v) and not v.lstrip().startswith(("[", "{")):
                        self._add(f, scene, "plugin", v, lpath + [i, "parameters", 3, k])
            i += 1

    def _plugins(self, root, rel):
        prel = (os.path.dirname(rel) + "/js/plugins.js").lstrip("/")
        p = os.path.join(root, *prel.split("/"))
        if not os.path.isfile(p):
            return
        text = _rb(p).decode("utf-8-sig")
        try:
            spans = _scan_plugins(text)
        except (ValueError, IndexError):
            return  # unparseable plugins.js: leave it alone
        for path, a, b in spans:
            if len(path) == 3 and path[1] == "parameters":
                v = json.loads(text[a:b])
                if (_CJK.search(v) and not _ASSET.search(v)
                        and not v.lstrip().startswith(("[", "{"))):
                    self._add(prel, "plugins", "plugin", v, path, plugins_js=True)

    # --- apply ---

    def apply(self, root, out_root, lines):
        by_file = {}
        for l in lines:
            if l.translation and l.translation != l.source and l.locator.get("path") is not None:
                by_file.setdefault(l.file, []).append(l)
        written = []
        for file, ls in sorted(by_file.items()):
            src = os.path.join(root, *file.split("/"))
            if not os.path.isfile(src):
                raise PatchError("missing file: %s" % file)
            if all(l.locator.get("plugins_js") for l in ls):
                self._apply_plugins(src, os.path.join(out_root, *file.split("/")), ls)
            else:
                data, st = _load(src)
                self._apply_json(data, ls)
                _save(os.path.join(out_root, *file.split("/")), data, st)
            written.append(file)
        return written

    @staticmethod
    def _get(data, path):
        for k in path:
            data = data[k]
        return data

    def _apply_json(self, data, ls):
        msgs = [l for l in ls if "start" in l.locator]
        try:
            for l in ls:
                if "start" in l.locator:
                    continue
                path = l.locator["path"]
                parent = self._get(data, path[:-1])
                if parent[path[-1]] != l.source:
                    raise PatchError("stale locator for %s" % l.id)
                parent[path[-1]] = l.translation
            # highest start first so earlier indices stay valid after re-splits
            msgs.sort(key=lambda l: l.locator["start"], reverse=True)
            msgs.sort(key=lambda l: str(l.locator["list"]))
            for l in msgs:
                self._apply_msg(data, l)
        except (KeyError, IndexError, TypeError) as e:
            raise PatchError("stale locator (%s: %r)" % (l.id, e))

    def _apply_msg(self, data, l):
        loc = l.locator
        cmds = self._get(data, loc["list"])
        s, n = loc["start"], loc["n"]
        if cmds[s]["code"] != loc["head"] or any(
                cmds[s + 1 + k]["code"] != loc["cont"] for k in range(n)):
            raise PatchError("stale locator for %s" % l.id)
        if s + 1 + n < len(cmds) and cmds[s + 1 + n]["code"] == loc["cont"]:
            raise PatchError("stale locator for %s (group grew)" % l.id)
        tag = loc.get("tag", "")
        if "\n".join(cmds[s + 1 + k]["parameters"][0] for k in range(n)) != tag + l.source:
            raise PatchError("stale locator for %s" % l.id)
        st = loc.get("speaker_tr")  # translated name, set by the runner
        if tag and st and ">" not in st and "\n" not in st:
            tag = "\\N<%s>" % st
        new = (tag + l.translation).split("\n")
        old = cmds[s + 1:s + 1 + n]
        indent = cmds[s].get("indent", 0)
        out = []
        for k, t in enumerate(new):
            if k < len(old):
                c = old[k]
            else:  # extra line: clone the last 4xx command's shape
                tmpl = old[-1] if old else {"code": loc["cont"], "indent": indent, "parameters": [""]}
                c = json.loads(json.dumps(tmpl))
            c["parameters"][0] = t
            out.append(c)
        cmds[s + 1:s + 1 + n] = out

    def _apply_plugins(self, src, dst, ls):
        text = _rb(src).decode("utf-8")
        bom = "﻿" if text.startswith("﻿") else ""
        text = text[len(bom):]
        try:
            spans = {tuple(p): (a, b) for p, a, b in _scan_plugins(text)}
        except (ValueError, IndexError) as e:
            raise PatchError("plugins.js unparseable: %s" % e)
        edits = []
        for l in ls:
            span = spans.get(tuple(l.locator["path"]))
            if span is None or json.loads(text[span[0]:span[1]]) != l.source:
                raise PatchError("stale locator for %s" % l.id)
            edits.append((span[0], span[1], json.dumps(l.translation, ensure_ascii=False)))
        for a, b, rep in sorted(edits, reverse=True):
            text = text[:a] + rep + text[b:]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write((bom + text).encode("utf-8"))


READER = Reader
