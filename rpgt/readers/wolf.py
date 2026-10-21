#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wolf RPG Editor reader: loose Data files and/or .wolf archives (unpacked by wolfarchive.py
into a temp cache; patched files are always written to the loose Data/... paths).

File-format logic (header byte + LCG seeds + per-interval XOR, map/common-event/
database layouts, command ids) is ported from rewolf-trans (MIT, KCFindstr):
src/wolf/*.ts and src/archive/*.ts. Game.dat layout is from format knowledge of
WolfTrans (MPL-2.0, elizagamedev); no WolfTrans code is copied. See WOLF_NOTES.md.

Design difference from rewolf: no parse->serialize round trip. The parser records the
byte span of every string, and apply() splices replacements into the original bytes,
so everything we do not understand (unknown commands, 'unknown' fields) is preserved
byte-for-byte by construction. Standard library only.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile

from rpgt.project import Line
from rpgt.readers import wolfarchive
from rpgt.readers.base import PatchError, Reader as _Base

WORK_ROOT = os.path.join(tempfile.gettempdir(), "rpgt_wolf")  # archive extraction cache
CRYPT_HEADER_SIZE = 10
DECRYPT_INTERVALS = (1, 2, 5)
DB_SEEDS = (0, 3, 6)
GAME_SEEDS = (0, 8, 6)

JP_HEADER = bytes([0] * 10 + [0x57, 0x4F, 0x4C, 0x46, 0x4D, 0, 0, 0, 0, 0, 0x64, 0, 0, 0,
                              0x65, 0x05, 0, 0, 0, 0x82, 0xC8, 0x82, 0xB5, 0])
EN_HEADER = bytes([0] * 10 + [0x57, 0x4F, 0x4C, 0x46, 0x4D, 0, 0, 0, 0, 0, 0x64, 0, 0, 0,
                              0x65, 0x03, 0, 0, 0, 0x4E, 0x6F, 0])
MAP_MAGIC = bytes(10) + b"WOLFM\0"
UTF8_FLAG = 0x55
EVENT_START = bytes([0x39, 0x30, 0, 0])
EVENT_END = bytes(4)
COMMAND_END = bytes([3, 0, 0, 0])
ROUTE_TERM = bytes([1, 0])
CE_HEADER = bytes([0x00, 0x57, 0x00, 0x00, 0x4F, 0x4C, 0x00, 0x46, 0x43, 0x00, 0x8F])
DB_HEADER = bytes([0x00, 0x57, 0x00, 0x00, 0x4F, 0x4C, 0x00, 0x46, 0x4D, 0x00, 0xC1])
CE_MAGIC = CE_HEADER[:10]  # the 11th byte is the editor version (0x8F old, 0xC9 in Wolf 3.1x)
DB_MAGIC = DB_HEADER[:10]
DB_STRING_INDICATOR = 0x0001D4C0
DB_SEP = bytes([0xFE, 0xFF, 0xFF, 0xFF])
STRING_START = 0x07D0
GAME_MAGIC = bytes([0x57, 0x00, 0x00, 0x4F, 0x4C, 0x00, 0x46, 0x4D, 0x00])
GAME_MAGIC_STR = b"0000-0000"

# Command ids that rewolf maps to a class (anything else is "unknown" -> script).
KNOWN_CIDS = {0, 99, 101, 102, 103, 105, 106, 107, 111, 112, 121, 122, 123, 124, 125, 126,
              130, 140, 150, 151, 160, 161, 162, 170, 171, 172, 173, 174, 175, 176, 177,
              178, 179, 180, 201, 202, 210, 211, 212, 213, 220, 221, 222, 230, 231, 240,
              241, 242, 250, 251, 270, 280, 281, 290, 300, 401, 402, 420, 421, 498, 499}

_CJK = re.compile("[぀-ヿ㐀-鿿ｦ-ﾟ]")
_FILELIKE = re.compile(r"[/\\]|\.\w{2,4}$")
_DESC_FIELD = re.compile("説明|解説|詳細|desc|help", re.I)


class ParseError(Exception):
    pass


# ---------------------------------------------------------------- crypto

def crypt(data, seeds):
    """In-place XOR; the same call encrypts and decrypts (wolf-crypko.ts)."""
    n = len(data)
    for seed, step in zip(seeds, DECRYPT_INTERVALS):
        s = seed
        for j in range(0, n, step):
            s = (s * 0x343FD + 0x269EC3) & 0xFFFFFFFF
            data[j] ^= (s >> 28) & 7


def _decrypt(raw, seed_indices):
    """-> (payload, crypt_header or None). First byte != 0 means encrypted."""
    if not raw:
        raise ParseError("empty file")
    if raw[0] == 0:
        return bytes(raw), None
    if len(raw) < CRYPT_HEADER_SIZE:
        raise ParseError("truncated crypt header")
    hdr = bytes(raw[:CRYPT_HEADER_SIZE])
    body = bytearray(raw[CRYPT_HEADER_SIZE:])
    crypt(body, [hdr[i] for i in seed_indices])
    return bytes(body), hdr


def _encrypt(payload, hdr, seed_indices):
    if hdr is None:
        return bytes(payload)
    body = bytearray(payload)
    crypt(body, [hdr[i] for i in seed_indices])
    return hdr + bytes(body)


# ---------------------------------------------------------------- cursor

class _Cur:
    def __init__(self, data):
        self.d = data
        self.p = 0

    @property
    def eof(self):
        return self.p == len(self.d)

    def take(self, n):
        if n < 0 or self.p + n > len(self.d):
            raise ParseError("unexpected end of data at 0x%x" % self.p)
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self):
        return self.take(1)[0]

    def u32(self):
        return int.from_bytes(self.take(4), "little")

    def count(self):
        # element counts are bounded by the bytes left; stops runaway loops on misparse
        n = self.u32()
        if n > len(self.d) - self.p:
            raise ParseError("implausible count %d at 0x%x" % (n, self.p - 4))
        return n

    def expect(self, b):
        at = self.p
        if self.take(len(b)) != b:
            raise ParseError("expected %s at 0x%x" % (b.hex(), at))

    def string(self):
        """-> (start, end, raw); start is the length prefix, end is past the NUL."""
        start = self.p
        n = self.u32()
        if n <= 0:
            raise ParseError("bad string length %d at 0x%x" % (n, start))
        raw = self.take(n - 1)
        if self.u8() != 0:
            raise ParseError("string not NUL-terminated at 0x%x" % start)
        return (start, self.p, bytes(raw))


def decode(raw):
    """-> (text, encoding). UTF-8 first: cp932 text almost never decodes as valid UTF-8."""
    if raw.isascii():
        return raw.decode("ascii"), "ascii"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp932"), "cp932"
    except UnicodeDecodeError:
        return raw.decode("cp932", "replace"), "cp932?"


def _blank(text):
    return not text.strip()


# ---------------------------------------------------------------- parsers
# Each parser returns raw items: (kind, path, scene, span, extra)

def _route(c):
    c.u8()
    for _ in range(c.u8()):
        c.u32()
    c.expect(ROUTE_TERM)


def _command(c):
    argc = c.u8()
    if argc < 1:
        raise ParseError("command with arg count 0 at 0x%x" % (c.p - 1))
    cid = c.u32()
    args = [c.u32() for _ in range(argc - 1)]
    c.u8()  # indent
    strs = [c.string() for _ in range(c.u8())]
    term = c.u8()
    if term == 1:  # Move command carries a route block
        c.take(5)
        c.u8()
        for _ in range(c.count()):
            _route(c)
    elif term != 0:
        raise ParseError("bad command terminator %d at 0x%x" % (term, c.p - 1))
    return cid, args, strs


def _commands(c):
    return [_command(c) for _ in range(c.count())]


def _command_items(cid, args, strs, path, scene, out):
    """Classify one command's strings (mapping documented in WOLF_NOTES.md)."""
    for i, sp in enumerate(strs):
        text, _ = decode(sp[2])
        if _blank(text):
            continue
        cjk = bool(_CJK.search(text))
        kind = None
        if cid == 101:
            kind = "dialogue" if i == 0 else ("script" if cjk else None)
        elif cid == 102:
            kind = "choice"
        elif cid == 150:
            is_text = bool(args) and ((args[0] >> 4) & 7) == 2
            kind = "other" if (is_text and i == 0 and cjk) else ("script" if cjk else None)
        elif cid == 122:
            kind = "other" if cjk else None
        elif cid in KNOWN_CIDS:
            kind = "script" if cjk else None  # 103/106/112/250/... and all other known cmds
        else:
            kind = "script" if cjk else None  # unknown cid: never treat as text
        if kind:
            out.append((kind, "%s/s%d" % (path, i), scene, sp, {"cid": cid}))


def parse_map(data, stem):
    c = _Cur(data)
    if data[:len(MAP_MAGIC)] != MAP_MAGIC:
        raise ParseError("unrecognised map header (other Wolf version?)")
    c.take(len(MAP_MAGIC))
    if c.u8() not in (0, UTF8_FLAG):  # byte 16: 0x55 = engine reads strings as UTF-8
        raise ParseError("unrecognised map header flag")
    c.take(3)
    ver = c.u32()
    if ver >= 0x65:
        raise ParseError("map version 0x%X is LZ4-packed (Wolf 3.5+), not supported" % ver)
    c.u8()
    c.string()  # editor-side note
    c.u32()  # tileset
    w, h, evc = c.u32(), c.u32(), c.u32()
    layers = 3
    if c.p + 4 <= len(c.d) and c.d[c.p:c.p + 4] == bytes.fromhex("ffffffff"):
        c.take(4)  # UTF-8 maps may omit the tile block
        layers = 0
    c.take(w * h * layers * 4)
    out = []
    n = 0
    while True:
        ind = c.u8()
        if ind != 0x6F:
            break
        n += 1
        c.expect(EVENT_START)
        eid = c.u32()
        name, _ = decode(c.string()[2])
        c.u32()
        c.u32()
        pages = c.u32()
        c.expect(EVENT_END)
        scene = "%s/ev%d%s" % (stem, eid, (":" + name) if name else "")
        pn = 0
        while c.u8() == 0x79:
            c.u32()
            c.string()
            c.take(4 + 37 + 4 + 1 + 1)
            for _ in range(c.count()):
                _route(c)
            for ci, (cid, args, strs) in enumerate(_commands(c)):
                _command_items(cid, args, strs, "e%d/p%d/c%d" % (eid, pn, ci), scene, out)
            feat = c.u32()  # page "features"; >3 adds one byte (UberWolf Map.hpp)
            c.take(3)
            if feat > 3:
                c.u8()
            if c.u8() != 0x7A:
                raise ParseError("bad page terminator at 0x%x" % (c.p - 1))
            pn += 1
        # the while-condition consumed the page-finish byte; it must be 0x70
        if c.d[c.p - 1] != 0x70:
            raise ParseError("bad page indicator at 0x%x" % (c.p - 1))
        if pn != pages:
            raise ParseError("event %d: expected %d pages, got %d" % (eid, pages, pn))
    if ind != 0x66:
        raise ParseError("bad event indicator %d" % ind)
    if not c.eof:
        raise ParseError("trailing bytes after map")
    if n != evc:
        raise ParseError("expected %d events, got %d" % (evc, n))
    return out


def parse_common(data):
    c = _Cur(data)
    _flagged_magic(c, CE_MAGIC, "CommonEvent.dat")
    ver = c.u8()
    if ver in (0x93, 0xCC):
        raise ParseError("CommonEvent version 0x%X is LZ4-packed (Wolf 3.5+), not supported" % ver)
    out = []
    for _ in range(c.count()):
        if c.u8() != 0x8E:
            raise ParseError("bad common event indicator at 0x%x" % (c.p - 1))
        eid = c.u32()
        c.u32()
        c.take(7)
        name, _ = decode(c.string()[2])
        scene = "CE%03d%s" % (eid, (":" + name) if name else "")
        for ci, (cid, args, strs) in enumerate(_commands(c)):
            _command_items(cid, args, strs, "ce%d/c%d" % (eid, ci), scene, out)
        c.string()  # unknown6
        c.string()  # description (editor-side)
        if c.u8() != 0x8F:
            raise ParseError("bad indicator2 at 0x%x" % (c.p - 1))

        def risky(sp, path):
            t, _ = decode(sp[2])
            if _CJK.search(t):
                out.append(("script", path, scene, sp, {"cid": -1}))

        for i in range(c.count()):
            risky(c.string(), "ce%d/strarg/%d" % (eid, i))
        c.take(c.count())  # byteArgs
        for i in range(c.count()):
            for j in range(c.count()):
                risky(c.string(), "ce%d/optarg/%d/%d" % (eid, i, j))
        for _ in range(c.count()):
            for _ in range(c.count()):
                c.u32()
        for _ in range(c.count()):
            c.u32()
        c.take(5)
        for i in range(100):
            risky(c.string(), "ce%d/cself/%d" % (eid, i))
        if c.u8() != 0x91:
            raise ParseError("bad indicator3 at 0x%x" % (c.p - 1))
        c.string()
        ind = c.u8()
        if ind != 0x91:
            if ind != 0x92:
                raise ParseError("bad indicator4 at 0x%x" % (c.p - 1))
            c.string()
            c.u32()
            if c.u8() != 0x92:
                raise ParseError("bad closing indicator4 at 0x%x" % (c.p - 1))
    if c.u8() < 0x89:
        raise ParseError("bad common event file end")
    if not c.eof:
        raise ParseError("trailing bytes after common events")
    return out


def _flagged_magic(c, magic, what):
    """10 bytes `00 57 00 00 4F 4C <00|55> 46 <43|4D> 00`: byte 6 = 0x55 marks a UTF-8 game."""
    raw = c.take(len(magic))
    if raw[6] == UTF8_FLAG:
        raw = raw[:6] + b"\0" + raw[7:]
    if raw != magic:
        raise ParseError("%s: unrecognised header %s" % (what, raw.hex()))


def _parse_project(proj):
    p = _Cur(proj)
    types = []
    for _ in range(p.count()):
        tname = decode(p.string()[2])[0]
        fnames = [decode(p.string()[2])[0] for _ in range(p.count())]
        dnames = [decode(p.string()[2])[0] for _ in range(p.count())]
        p.string()  # description
        ftypes = [p.u8() for _ in range(p.count())]
        for _ in range(p.count()):
            p.string()
        for _ in range(p.count()):
            for _ in range(p.count()):
                p.string()
        for _ in range(p.count()):
            for _ in range(p.count()):
                p.u32()
        for _ in range(p.count()):
            p.u32()
        types.append((tname, fnames, dnames, ftypes))
    return types


def parse_db(proj, dat, stem):
    types = _parse_project(proj)
    payload, hdr = _decrypt(dat, DB_SEEDS)
    d = _Cur(payload)
    if hdr is not None:
        dbver = d.u8()
    else:
        _flagged_magic(d, DB_MAGIC, "database .dat")
        dbver = d.u8()
    if d.u32() != len(types):
        raise ParseError("type count mismatch between .dat and .project")
    out = []
    for ti, (tname, fnames, dnames, ftypes) in enumerate(types):
        d.expect(DB_SEP)
        if d.u32() == DB_STRING_INDICATOR:
            d.string()
        infos = [d.u32() for _ in range(d.count())]
        if len(infos) > len(fnames):
            raise ParseError("type %d: .dat has more fields than .project" % ti)
        nstr = sum(1 for i in infos if i >= STRING_START)
        nint = len(infos) - nstr
        ds = d.u32()  # not count(): a type with no fields stores its entries in zero bytes
        if ds > len(dnames):
            raise ParseError("type %d: .dat has more entries than .project" % ti)
        scene = "DB:%s/t%d" % (stem, ti)
        for di in range(ds):
            for _ in range(nint):
                d.u32()
            strs = [d.string() for _ in range(nstr)]
            for fi, info in enumerate(infos):
                if info < STRING_START or (ftypes[fi] if fi < len(ftypes) else -1) != 0:
                    continue
                idx = info - STRING_START
                if idx >= nstr:
                    raise ParseError("type %d field %d: string slot out of range" % (ti, fi))
                sp = strs[idx]
                text, _ = decode(sp[2])
                if _blank(text):
                    continue
                if not (_CJK.search(text) or not _FILELIKE.search(text)):
                    continue
                kind = "desc" if (_DESC_FIELD.search(fnames[fi]) or "\n" in text) else "name"
                ctx = "%s / %s / %s" % (tname, dnames[di], fnames[fi])
                out.append((kind, "t%d/d%d/f%d" % (ti, di, idx), scene, sp, {"ctx": ctx}))
    d.u8()  # trailing version byte (equals dbver); rewolf only logs when absent
    return out


def parse_game(raw):
    payload, hdr = _decrypt(raw, GAME_SEEDS)
    c = _Cur(payload)
    if hdr is None:
        raw = c.take(1 + len(GAME_MAGIC))
        if raw[9] == UTF8_FLAG:  # same "U" marker as the other files, at index 9 here
            raw = raw[:9] + bytes(1)
        if raw != bytes(1) + GAME_MAGIC:
            raise ParseError("Game.dat: unrecognised header %s" % raw.hex())
    c.take(c.count())
    ver = c.u32()
    title = c.string()
    if c.string()[2] != GAME_MAGIC_STR:
        raise ParseError("Game.dat magic string mismatch")
    c.take(c.count())
    for _ in range(5):  # font, 3 subfonts, default pc graphic
        c.string()
    strs = [("title", title)]
    if ver >= 9:
        strs.append(("titleplus", c.string()))
    if ver > 9:  # road image, gauge image, start-up message, title message
        strs += [("roadimg", c.string()), ("gaugeimg", c.string()),
                 ("startup", c.string()), ("titlemsg", c.string())]
    if ver > 13:
        c.string()
    szoff = c.p
    szval = c.u32()
    ioff = None  # two u32 offsets after the word table; the game expects them shifted with the size
    try:
        c.u32()
        c.take(c.u32() * 2)
        ioff = c.p
        c.u32()
        c.u32()
    except ParseError:
        pass
    hl = CRYPT_HEADER_SIZE if hdr is not None else 0
    out = []
    for name, sp in strs:
        if name in ("roadimg", "gaugeimg") or _blank(decode(sp[2])[0]):
            continue  # image file names are not text
        out.append(("other", name, "Game", sp,
                    {"szoff": szoff, "szfix": szval == len(payload) + hl - 1, "ioff": ioff}))
    return out


# ---------------------------------------------------------------- file discovery

def _ci_child(dirpath, name):
    try:
        for e in os.listdir(dirpath):
            if e.lower() == name.lower():
                return os.path.join(dirpath, e)
    except OSError:
        pass
    return None


def _data_dir(root):
    d = _ci_child(root, "Data")
    if d and os.path.isdir(d):
        return d
    if _ci_child(root, "BasicData") or _ci_child(root, "MapData"):
        return root
    return None


def _natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _wolf_archives(root):
    """Every .wolf next to the game exe or inside Data/."""
    out = []
    for d in (root, _ci_child(root, "Data")):
        if d and os.path.isdir(d):
            out += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith(".wolf")]
    return out


def _cache_data_dir(root, errors=None):
    """Data mirror extracted from the .wolf archives, or None. Lives in the OS temp dir (never in
    the game folder), keyed by the archives' name/size/mtime so a shadow copy of the game (the
    patcher's verify step) and the real folder share one extraction."""
    arcs = _wolf_archives(root)
    if not arcs:
        return None
    sig = hashlib.sha1()
    for a in arcs:
        try:
            st = os.stat(a)
        except OSError:
            continue
        sig.update(("%s|%d|%d;" % (os.path.basename(a).lower(), st.st_size, st.st_mtime_ns)).encode())
    base = os.path.join(WORK_ROOT, sig.hexdigest()[:16])
    data = os.path.join(base, "Data")
    marker = os.path.join(base, "ok.json")
    if os.path.isfile(marker):
        return data
    tmp = base + ".part%d" % os.getpid()
    shutil.rmtree(tmp, ignore_errors=True)
    errs, got = {}, 0
    for d in sorted({os.path.dirname(a) for a in arcs}):
        res = wolfarchive.unpack_game_data(d, os.path.join(tmp, "Data"))
        errs.update(res.pop("_errors"))
        got += sum(len(v) for v in res.values())
    if errors is not None:
        errors.update(errs)
    if not got:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    with open(os.path.join(tmp, "ok.json"), "w", encoding="utf-8") as f:
        json.dump({"archives": [os.path.basename(a) for a in arcs], "errors": errs}, f)
    shutil.rmtree(base, ignore_errors=True)
    try:
        os.replace(tmp, base)
    except OSError:  # another process won the race
        shutil.rmtree(tmp, ignore_errors=True)
    return data if os.path.isfile(marker) else None


def _scan(root, use_archives=True, errors=None):
    """-> dict(maps=[(rel, path)], ce=(rel, path)|None, dbs=[((rel, proj), (rel, dat))], game=(rel, path)|None).
    `rel` is always the LOOSE-file location relative to root (what apply() writes); `path` is where
    the bytes are read from: a loose file if present, else the archive extraction."""
    res = {"maps": [], "ce": None, "dbs": [], "game": None}
    dd = _data_dir(root)
    prefix = os.path.relpath(dd, root).replace(os.sep, "/") if dd else "Data"
    prefix = "" if prefix == "." else prefix + "/"
    dirs = [dd] if dd else []
    if use_archives and _wolf_archives(root):
        cd = _cache_data_dir(root, errors)
        if cd:
            dirs.append(cd)  # later = lower priority: loose files shadow the archives

    def merged(sub):
        """lower-case file name -> (rel, path), loose first."""
        out = {}
        for d in dirs:
            sd = _ci_child(d, sub)
            if sd and os.path.isdir(sd):
                for f in os.listdir(sd):
                    out.setdefault(f.lower(), ("%s%s/%s" % (prefix, os.path.basename(sd) if d is dd else sub, f),
                                               os.path.join(sd, f)))
        return out

    maps = merged("MapData")
    res["maps"] = sorted((v for k, v in maps.items() if k.endswith(".mps")),
                         key=lambda v: _natkey(os.path.basename(v[0])))
    basic = merged("BasicData")
    res["ce"] = basic.get("commonevent.dat")
    res["game"] = basic.get("game.dat")
    for k in sorted(basic, key=_natkey):
        if k.endswith(".project") and k != "sysdatabasebasic.project":
            dat = basic.get(k[:-8] + ".dat")
            if dat:
                res["dbs"].append((basic[k], dat))
    return res


def archive_status(root):
    """'extracted' (loose files) | 'archive' (readable .wolf, unpacked on demand) |
    'archive-encrypted' (.wolf we cannot read) | 'none'."""
    s = _scan(root, use_archives=False)
    if s["maps"] or s["ce"] or s["dbs"] or s["game"]:
        return "extracted"
    arcs = _wolf_archives(root)
    if not arcs:
        return "none"
    for a in arcs:
        stem = os.path.splitext(os.path.basename(a))[0].lower()
        if stem in ("data", "basicdata", "mapdata"):
            try:
                if wolfarchive.is_dxa(a) and wolfarchive.detect_key(a) is not None:
                    return "archive"
            except (wolfarchive.DxaError, OSError):
                pass
    return "archive-encrypted"


# ---------------------------------------------------------------- reader

class Reader(_Base):
    name = "wolf"
    label = "Wolf RPG Editor (Data folder or .wolf archives)"

    def __init__(self, write_encoding="utf-8"):
        self.write_encoding = write_encoding
        self.warnings = []          # (relative file, message) for files that failed to parse
        self.game_encoding = "unknown"  # set by extract(): utf-8 / cp932 / ascii / mixed
        self._retired = []          # archives the last apply() unpacked and wants removed

    @classmethod
    def detect(cls, root):
        return archive_status(root) in ("extracted", "archive")

    def extract(self, root):
        self.warnings = []
        errs = {}
        s = _scan(root, errors=errs)
        self.warnings += [(k, "archive: " + v) for k, v in sorted(errs.items())]
        stem = lambda p: os.path.splitext(os.path.basename(p))[0]
        jobs = [("mps", m, lambda raw, m=m: parse_map(raw, stem(m[1]))) for m in s["maps"]]
        if s["ce"]:
            jobs.append(("ce", s["ce"], parse_common))
        for proj, dat in s["dbs"]:
            jobs.append(("db", dat, lambda raw, proj=proj, dat=dat: parse_db(_read(proj[1]), raw, stem(dat[1]))))
        if s["game"]:
            jobs.append(("game", s["game"], parse_game))

        lines, encs = [], set()
        for ftype, (rel, path), fn in jobs:
            try:
                raw = _read(path)
                items = fn(raw)
            except (ParseError, OSError) as e:
                self.warnings.append((rel, str(e)))
                continue
            for kind, ipath, scene, sp, extra in items:
                text, enc = decode(sp[2])
                if enc != "ascii":
                    encs.add(enc.rstrip("?"))
                loc = {"ftype": ftype, "off": sp[0], "len": sp[1] - sp[0],
                       "raw": sp[2].hex(), "enc": enc}
                loc.update(extra)
                lines.append(Line(id="%s#%s" % (rel, ipath), file=rel, scene=scene,
                                  order=len(lines), kind=kind, source=text, locator=loc))
        self.game_encoding = ("ascii" if not encs else
                              encs.pop() if len(encs) == 1 else "mixed")
        return lines

    def retired(self):
        return list(self._retired)

    def _unpack_archives(self, root, out_root):
        """The game keeps reading its `.wolf` archives and ignores loose files next to them
        (checked in-game on the Imouto game), so unpack the logic archives completely into
        out_root and report them as retired. Loose files already in the game win over the
        unpacked copy. Returns the relative paths written."""
        self._retired = []
        by_dir = {}
        for a in _wolf_archives(root):
            stem = os.path.splitext(os.path.basename(a))[0].lower()
            if stem == "data":
                raise PatchError("%s: a single Data.wolf holds the media too; unpacking it is not supported yet"
                                 % os.path.relpath(a, root))
            if stem in wolfarchive.DEFAULT_ARCHIVES:
                by_dir.setdefault(os.path.dirname(a), []).append(a)
        out = []
        for d, arcs in sorted(by_dir.items()):
            rd = os.path.relpath(d, root).replace(os.sep, "/")
            res = wolfarchive.unpack_game_data(d, os.path.join(out_root, *rd.split("/")),
                                               archives=[os.path.basename(a) for a in arcs], exts=None)
            errs = res.pop("_errors")
            if errs:
                raise PatchError("cannot unpack %s" % "; ".join("%s: %s" % kv for kv in sorted(errs.items())))
            for ws in res.values():
                for w in ws:
                    rel = "%s/%s" % (rd, w)
                    if os.path.exists(os.path.join(root, *rel.split("/"))):
                        os.remove(os.path.join(out_root, *rel.split("/")))  # keep the loose file
                    else:
                        out.append(rel)
            self._retired += ["%s/%s" % (rd, os.path.basename(a)) for a in arcs]
        return out

    def apply(self, root, out_root, lines):
        by_file = {}
        for l in lines:
            if not l.translation or l.translation == l.source:
                continue
            by_file.setdefault(l.file, []).append(l)
        written = []
        self._retired = []
        if by_file:
            written += self._unpack_archives(root, out_root)
        s = _scan(root)  # reads from loose files else the archive extraction; writes go to loose paths
        phys = {rel: path for rel, path in s["maps"] + [x for x in (s["ce"], s["game"]) if x]
                + [d for _, d in s["dbs"]]}
        for rel in sorted(by_file):
            src = phys.get(rel) or os.path.join(root, *rel.split("/"))
            try:
                raw = _read(src)
            except OSError as e:
                raise PatchError("%s: cannot read (%s)" % (rel, e))
            ftype = by_file[rel][0].locator.get("ftype")
            seeds = GAME_SEEDS if ftype == "game" else DB_SEEDS
            try:
                payload, hdr = _decrypt(raw, seeds) if ftype in ("db", "game") else (raw, None)
            except ParseError as e:
                raise PatchError("%s: %s" % (rel, e))
            edits = []
            for l in by_file[rel]:
                loc = l.locator
                off, n = loc.get("off"), loc.get("len")
                if not isinstance(off, int) or not isinstance(n, int):
                    raise PatchError("%s: line has no wolf locator" % l.id)
                if not self._span_ok(payload, off, n, loc.get("raw", "")):
                    raise PatchError("%s: bytes at 0x%x no longer match (re-extract)" % (l.id, off))
                try:
                    enc = l.translation.encode(self.write_encoding)
                except (UnicodeEncodeError, LookupError) as e:
                    raise PatchError("%s: cannot encode in %s (%s)" % (l.id, self.write_encoding, e))
                if b"\x00" in enc:
                    raise PatchError("%s: translation contains NUL" % l.id)
                edits.append((off, n, (len(enc) + 1).to_bytes(4, "little") + enc + b"\0", loc))
            edits.sort(key=lambda e: e[0])
            for a, b in zip(edits, edits[1:]):
                if a[0] + a[1] > b[0]:
                    raise PatchError("%s: overlapping edits" % rel)
            out, pos, delta_before = bytearray(), 0, []
            for off, n, new, _ in edits:
                out += payload[pos:off] + new
                pos = off + n
                delta_before.append((off, len(new) - n))
            out += payload[pos:]
            if ftype == "game":
                loc = edits[0][3]
                szoff = loc.get("szoff")
                if loc.get("szfix") and isinstance(szoff, int):
                    shift = sum(d for o, d in delta_before if o < szoff)
                    total = len(out) + (CRYPT_HEADER_SIZE if hdr is not None else 0)
                    out[szoff + shift:szoff + shift + 4] = (total - 1).to_bytes(4, "little")
                    ioff = loc.get("ioff")
                    if isinstance(ioff, int):
                        diff = len(out) - len(payload)
                        sh = sum(d for o, d in delta_before if o < ioff)
                        for k in (0, 4):
                            at = ioff + sh + k
                            v = int.from_bytes(out[at:at + 4], "little")
                            out[at:at + 4] = ((v + diff) & 0xFFFFFFFF).to_bytes(4, "little")
            data = _encrypt(out, hdr, seeds) if ftype in ("db", "game") else bytes(out)
            dst = os.path.join(out_root, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            if rel not in written:
                written.append(rel)
        return written

    @staticmethod
    def _span_ok(payload, off, n, rawhex):
        if off < 0 or n < 5 or off + n > len(payload):
            return False
        ln = int.from_bytes(payload[off:off + 4], "little")
        return (ln == n - 4 and payload[off + n - 1] == 0
                and payload[off + 4:off + n - 1].hex() == rawhex)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


READER = Reader
