#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for readers.rubymarshal and readers.rpgmruby.
Fixtures are synthetic (built by hand / by our own dumper); no real game was used.
Run: PYTHONUTF8=1 python test_rpgmruby.py"""

import copy
import hashlib
import os
import shutil
import tempfile
import unittest

from readers import rubymarshal as rm
from readers import rpgmruby
from readers.base import PatchError

H = b"\x04\x08"


def _fx():
    """(name, bytes, builder-of-equivalent-object or None) -- hand-written wire bytes."""
    a = rm.RString(b"a")
    f = rm.RFloat(1.5, b"1.5")
    sh = rm.RObject("Foo")
    sh.ivars["@self"] = sh
    sym = rm.Symbol("foo")
    return [
        ("nil", H + b"0", lambda: None),
        ("true", H + b"T", lambda: True),
        ("false", H + b"F", lambda: False),
        ("fix0", H + b"i\x00", lambda: 0),
        ("fix1", H + b"i\x06", lambda: 1),
        ("fix122", H + b"i\x7f", lambda: 122),
        ("fix123", H + b"i\x01\x7b", lambda: 123),
        ("fix-1", H + b"i\xfa", lambda: -1),
        ("fix-123", H + b"i\x80", lambda: -123),
        ("fix-124", H + b"i\xff\x84", lambda: -124),
        ("fix255", H + b"i\x01\xff", lambda: 255),
        ("fix256", H + b"i\x02\x00\x01", lambda: 256),
        ("fix-256", H + b"i\xff\x00", lambda: -256),
        ("fix-257", H + b"i\xfe\xff\xfe", lambda: -257),
        ("fix65536", H + b"i\x03\x00\x00\x01", lambda: 65536),
        ("fixmax", H + b"i\x04\xff\xff\xff\x3f", lambda: 2 ** 30 - 1),
        ("fixmin", H + b"i\xfc\x00\x00\x00\xc0", lambda: -2 ** 30),
        ("big+", H + b"l+\x07\x00\x00\x00\x40", lambda: 2 ** 30),
        ("big-", H + b"l-\x07\x01\x00\x00\x40", lambda: -2 ** 30 - 1),
        ("float", H + b"f\x081.5", lambda: rm.RFloat(1.5, b"1.5")),
        ("float0", H + b"f\x060", lambda: rm.RFloat(0.0, b"0")),
        ("inf", H + b"f\x08inf", lambda: rm.RFloat(float("inf"), b"inf")),
        ("-inf", H + b"f\t-inf", lambda: rm.RFloat(float("-inf"), b"-inf")),
        ("nan", H + b"f\x08nan", lambda: rm.RFloat(float("nan"), b"nan")),
        ("bin string", H + b'"\x08abc', lambda: rm.RString(b"abc")),
        ("utf8 string", H + b'I"\x08abc\x06:\x06ET',
         lambda: rm.RString(b"abc", {rm.Symbol("E"): True})),
        ("ascii string", H + b'I"\x08abc\x06:\x06EF',
         lambda: rm.RString(b"abc", {rm.Symbol("E"): False})),
        ("named enc", H + b'I"\x06a\x06:\rencoding"\x0eShift_JIS',
         lambda: rm.RString(b"a", {rm.Symbol("encoding"): rm.RString(b"Shift_JIS")})),
        ("symbol", H + b":\x08foo", lambda: sym),
        ("symlink", H + b"[\x07:\x08foo;\x00", lambda: rm.RArray([sym, sym])),
        ("sym with enc", H + b"I:\x08\xe3\x81\x82\x06:\x06ET", None),
        ("array", H + b"[\x07i\x06i\x07", lambda: rm.RArray([1, 2])),
        ("empty array", H + b"[\x00", lambda: rm.RArray()),
        ("array ivars", H + b"I[\x06i\x06\x06:\x07@ai\x07", None),
        ("obj link", H + b'[\x07"\x06a@\x06', lambda: rm.RArray([a, a])),
        ("float link", H + b"[\x07f\x081.5@\x06", lambda: rm.RArray([f, f])),
        ("hash", H + b"{\x06i\x06i\x07", lambda: _h([(1, 2)])),
        ("hash default", H + b"}\x06i\x06i\x07i\x08", lambda: _h([(1, 2)], 3)),
        ("hash default nil", H + b"}\x00" + b"0", lambda: _h([], None, True)),
        ("object", H + b"o:\x08Foo\x06:\x07@ai\x06", lambda: rm.RObject("Foo", {"@a": 1})),
        ("self cycle", H + b"o:\x08Foo\x06:\n@self@\x00", lambda: sh),
        ("struct", H + b"S:\x08Bar\x06:\x06xi\x06", lambda: rm.RStruct("Bar", {"x": 1})),
        ("userdef", H + b"u:\nTable\x08\x01\x02\x03", lambda: rm.RUserDef("Table", b"\x01\x02\x03")),
        ("userdef ivars", H + b"Iu:\nTable\x08abc\x06:\x06ET", None),
        ("usermarshal", H + b"U:\x08Foo[\x06i\x06",
         lambda: rm.RUserMarshal("Foo", rm.RArray([1]))),
        ("class", H + b"c\x0bString", lambda: rm.RClass("String", "c")),
        ("module", H + b"m\x0bKernel", lambda: rm.RClass("Kernel", "m")),
        ("regexp", H + b"I/\x08a.b\x01\x06:\x06ET", None),
        ("extended", H + b"e:\x08Foo[\x00", lambda: rm.RExtended(["Foo"], rm.RArray())),
        ("userclass", H + b'C:\x08Foo"\x06a', lambda: rm.RUserClass("Foo", rm.RString(b"a"))),
        ("ivar+extended+userclass", H + b'Ie:\x08FooC:\x08Bar"\x06a\x06:\x06ET', None),
        ("encname shared", H + b'[\x07I"\x06a\x06:\rencoding"\x0eShift_JISI"\x06b\x06;\x00@\x07', None),
    ]


def _h(pairs, default=None, has=False):
    h = rm.RHash(pairs)
    if default is not None or has:
        h.default, h.has_default = default, True
    return h


class MarshalTests(unittest.TestCase):
    def test_roundtrip_every_fixture(self):
        for name, data, _ in _fx():
            with self.subTest(name):
                self.assertEqual(rm.dumps(rm.loads(data)), data)

    def test_dump_from_objects_matches_wire_bytes(self):
        for name, data, build in _fx():
            if build is None:
                continue
            with self.subTest(name):
                self.assertEqual(rm.dumps(build()), data)

    def test_loaded_values(self):
        L = lambda n: rm.loads(dict((x[0], x[1]) for x in _fx())[n])
        self.assertEqual(L("fix-257"), -257)
        self.assertEqual(L("big-"), -2 ** 30 - 1)
        self.assertIsInstance(L("big+"), rm.RBignum)
        self.assertEqual(L("float"), 1.5)
        self.assertTrue(L("nan") != L("nan"))
        s = L("named enc")
        self.assertEqual(s.encoding_name, "Shift_JIS")
        self.assertEqual(L("utf8 string").encoding_name, "utf-8")
        self.assertIsNone(L("bin string").encoding_name)

    def test_link_and_symlink_identity(self):
        arr = rm.loads(dict((x[0], x[1]) for x in _fx())["obj link"])
        self.assertIs(arr[0], arr[1])
        cyc = rm.loads(dict((x[0], x[1]) for x in _fx())["self cycle"])
        self.assertIs(cyc.ivars["@self"], cyc)
        sy = rm.loads(dict((x[0], x[1]) for x in _fx())["symlink"])
        self.assertIs(sy[0], sy[1])

    def test_shared_encoding_name_survives_mutating_first_owner(self):
        data = dict((x[0], x[1]) for x in _fx())["encname shared"]
        arr = rm.loads(data)
        self.assertIs(arr[0].ivars["encoding"], arr[1].ivars["encoding"])
        arr[0].set_bytes(b"zz")
        arr[0].mark_utf8()  # drops its encoding string; second string still full
        out = rm.loads(rm.dumps(arr))
        self.assertEqual(out[1].encoding_name, "Shift_JIS")
        self.assertEqual(out[0].raw, b"zz")
        self.assertEqual(out[0].encoding_name, "utf-8")

    def test_mutated_string_redumps(self):
        s = rm.loads(H + b'I"\x08abc\x06:\x06ET')
        s.set_bytes("あ".encode())
        self.assertEqual(rm.dumps(s), H + b'I"\x08\xe3\x81\x82\x06:\x06ET')

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            rm.loads(b"\x04\x07" + b"0")
        with self.assertRaises(ValueError):
            rm.loads(H + b"0x")
        with self.assertRaises(ValueError):
            rm.loads(H + b"d")


# ---------------------------------------------------------------- builders

def S(text, how="u8"):
    if how == "cp932":
        return rm.RString(text.encode("cp932"))
    return rm.RString(text.encode("utf-8"), {rm.Symbol("E"): True})


def O(cls, **kw):
    return rm.RObject(cls, {"@" + k: v for k, v in kw.items()})


def C(code, indent, *params):
    return O("RPG::EventCommand", code=code, indent=indent,
             parameters=rm.RArray(list(params)))


def say(lines, how, xp, indent=0):
    if xp:
        cmds = [C(101, indent, S(lines[0], how))]
        rest = lines[1:]
    else:
        cmds = [C(101, indent, S("", how), 0, 0, 2)]
        rest = lines
    return cmds + [C(401, indent, S(t, how)) for t in rest]


def build_event_list(how, xp):
    lst = say(["おはよう", "今日もいい天気", "ですね"], how, xp)
    lst.append(C(102, 0, rm.RArray([S("はい", how), S("いいえ", how)]), 2))
    lst.append(C(402, 0, 0, S("はい", how)))
    lst += say(["はい、そうです"], how, xp, indent=1)
    lst.append(C(0, 1))
    lst.append(C(404, 0))
    lst.append(C(355, 0, S("p 'スクリプト'", how)))
    lst.append(C(108, 0, S("コメント", how)))
    lst.append(C(0, 0))
    return rm.RArray(lst)


def build_files(ext):
    xp, ace = ext == ".rxdata", ext == ".rvdata2"
    how = "u8" if ace else "cp932"
    page = O("RPG::Event::Page", list=build_event_list(how, xp))
    ev = O("RPG::Event", id=1, name=S("EV001"), x=1, y=1, pages=rm.RArray([page]))
    mp = O("RPG::Map", width=10, height=10, events=_h([(1, ev)]),
           data=rm.RUserDef("Table", b"\x00" * 16))
    if ace:
        mp.ivars["@display_name"] = S("始まりの村", how)
    maps = {"Map001": mp, "Map002": copy.deepcopy(mp) if False else O(
        "RPG::Map", width=1, height=1, events=_h([]), data=rm.RUserDef("Table", b""))}
    info = _h([(1, O("RPG::MapInfo", name=S("村", how), parent_id=0, order=2)),
               (2, O("RPG::MapInfo", name=S("森", how), parent_id=0, order=1))])
    ce = rm.RArray([None, O("RPG::CommonEvent", id=1, name=S("ce"),
                            list=rm.RArray(say(["共通イベント"], how, xp) + [C(0, 0)]))])
    actors = rm.RArray([None, O("RPG::Actor", id=1, name=S("アリス", how),
                                character_name=S("Actor1", how))])
    items = rm.RArray([None, O("RPG::Item", id=1, name=S("薬草", how),
                               description=S("体力を回復する", how))])
    system = O("RPG::System", game_title=S("ゲーム", how),
               terms=O("RPG::System::Terms", basic=rm.RArray([S("レベル", how)])))
    troops = rm.RArray([None, O("RPG::Troop", id=1, name=S("t"), pages=rm.RArray([
        O("RPG::Troop::Page", list=rm.RArray(say(["敵が現れた"], how, xp) + [C(0, 0)]))]))])
    return {"Map001": maps["Map001"], "Map002": maps["Map002"], "MapInfos": info,
            "CommonEvents": ce, "Actors": actors, "Items": items, "System": system,
            "Troops": troops}


def write_game(root, ext, under_data=True):
    d = os.path.join(root, "Data") if under_data else root
    os.makedirs(d, exist_ok=True)
    for name, o in build_files(ext).items():
        with open(os.path.join(d, name + ext), "wb") as f:
            f.write(rm.dumps(o))
    with open(os.path.join(d, "Scripts" + ext), "wb") as f:
        f.write(b"not marshal at all")  # must never be parsed
    return d


def snapshot(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root)] = hashlib.sha1(open(p, "rb").read()).hexdigest()
    return out


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def game(self, ext, **kw):
        root = os.path.join(self.tmp.name, "g" + ext.strip("."))
        os.makedirs(root, exist_ok=True)
        write_game(root, ext, **kw)
        return root

    def each_ext(self):
        return [(e, self.game(e)) for e in (".rxdata", ".rvdata", ".rvdata2")]

    def full(self, root, out):
        """Whole game with the patched files laid over it (apply() writes only changes)."""
        dst = out + "_full"
        shutil.copytree(root, dst)
        shutil.copytree(out, dst, dirs_exist_ok=True)
        return dst

    def test_detect_variants_and_root_layout(self):
        for ext, root in self.each_ext():
            self.assertTrue(rpgmruby.Reader.detect(root))
            self.assertIsNone(rpgmruby.archive_status(root))
        flat = self.game(".rvdata2", under_data=False) if False else None
        root = os.path.join(self.tmp.name, "flat")
        write_game(root, ".rvdata2", under_data=False)
        self.assertTrue(rpgmruby.Reader.detect(root))
        ln = rpgmruby.Reader().extract(root)[0]
        self.assertEqual(ln.file, "Map002.rvdata2" if False else ln.file)
        self.assertFalse(ln.file.startswith("Data/"))

    def test_encrypted_archive_only(self):
        root = os.path.join(self.tmp.name, "enc")
        os.makedirs(root)
        open(os.path.join(root, "Game.rgss3a"), "wb").write(b"RGSSAD\0\3")
        self.assertFalse(rpgmruby.Reader.detect(root))
        self.assertEqual(rpgmruby.archive_status(root), "archive-encrypted")
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        self.assertFalse(rpgmruby.Reader.detect(empty))
        self.assertIsNone(rpgmruby.archive_status(empty))

    def test_extract_order_kinds_and_skips(self):
        for ext, root in self.each_ext():
            lines = rpgmruby.Reader().extract(root)
            self.assertEqual([l.order for l in lines], list(range(len(lines))))
            src = [l.source for l in lines]
            self.assertIn("おはよう\n今日もいい天気\nですね", src)
            self.assertIn("はい、そうです", src)
            self.assertNotIn("p 'スクリプト'", src)
            self.assertNotIn("コメント", src)
            self.assertTrue(all(l.speaker == "" for l in lines))
            files = [l.file for l in lines]
            # MapInfos tree puts Map002 (order 1) before Map001 (order 2); empty map has no lines
            self.assertEqual(files[0], "Data/Map001" + ext)
            first_non_map = next(i for i, f in enumerate(files) if "Map" not in f)
            self.assertTrue(files[first_non_map].startswith("Data/CommonEvents"))
            tail = [l.file.split("/")[1].split(".")[0] for l in lines[first_non_map:]]
            self.assertEqual(sorted(set(tail), key=tail.index),
                             ["CommonEvents", "Troops", "Actors", "Items", "System"])
            kinds = {l.source: l.kind for l in lines}
            self.assertEqual(kinds["はい"], "choice")
            self.assertEqual(kinds["アリス"], "name")
            self.assertEqual(kinds["体力を回復する"], "desc")
            self.assertEqual(kinds["レベル"], "term")
            self.assertNotIn("Actor1", src)
            self.assertEqual(len({l.id for l in lines}), len(lines))
            self.assertTrue(all(l.kind in __import__("project").KINDS for l in lines))
            if ext == ".rvdata2":
                self.assertIn("始まりの村", src)
            # scenes contiguous
            seen, prev = set(), None
            for l in lines:
                if l.scene != prev:
                    self.assertNotIn(l.scene, seen)
                    seen.add(l.scene)
                    prev = l.scene

    def patch(self, root, edits):
        r = rpgmruby.Reader()
        lines = r.extract(root)
        for l in lines:
            if l.source in edits:
                l.translation = edits[l.source]
        out = os.path.join(self.tmp.name, "out_" + os.path.basename(root))
        return r, lines, out, r.apply(root, out, lines)

    def test_resplit_shrink_and_grow_keeps_indent(self):
        for ext, root in self.each_ext():
            before = snapshot(root)
            _, _, out, written = self.patch(root, {
                "おはよう\n今日もいい天気\nですね": "Good morning\nNice weather",
                "はい、そうです": "Yes\nit\nis"})
            self.assertEqual(snapshot(root), before)  # root untouched
            self.assertEqual(written, ["Data/Map001" + ext])
            self.assertEqual(os.listdir(os.path.join(out, "Data")), ["Map001" + ext])
            m = rm.loads(open(os.path.join(out, "Data", "Map001" + ext), "rb").read())
            lst = m.ivars["@events"].get(1).ivars["@pages"][0].ivars["@list"]
            codes = [(c.ivars["@code"], c.ivars["@indent"]) for c in lst]
            xp = ext == ".rxdata"
            self.assertEqual(codes[:3], [(101, 0), (401, 0), (102, 0)] if xp else [(101, 0), (401, 0), (401, 0)])
            tail = [c for c in codes if c[1] == 1 and c[0] in (101, 401)]
            self.assertEqual(tail, [(101, 1)] + [(401, 1)] * (2 if xp else 3))
            # extract again from patched copy: text round-trips
            again = rpgmruby.Reader().extract(self.full(root, out))
            src = [l.source for l in again]
            self.assertIn("Good morning\nNice weather", src)
            self.assertIn("Yes\nit\nis", src)

    def test_xp_single_line_drops_all_401(self):
        root = self.game(".rxdata")
        _, _, out, _ = self.patch(root, {"おはよう\n今日もいい天気\nですね": "Hello"})
        m = rm.loads(open(os.path.join(out, "Data", "Map001.rxdata"), "rb").read())
        lst = m.ivars["@events"].get(1).ivars["@pages"][0].ivars["@list"]
        self.assertEqual([c.ivars["@code"] for c in lst[:2]], [101, 102])

    def test_encodings_cp932_kept_utf8_fallback(self):
        root = self.game(".rvdata")
        _, _, out, _ = self.patch(root, {"おはよう\n今日もいい天気\nですね": "ASCII only\nこんにちは\nñandú"})
        m = rm.loads(open(os.path.join(out, "Data", "Map001.rvdata"), "rb").read())
        lst = m.ivars["@events"].get(1).ivars["@pages"][0].ivars["@list"]
        s1, s2 = lst[1].ivars["@parameters"][0], lst[2].ivars["@parameters"][0]
        self.assertIsNone(s1.ivars)                 # still plain cp932
        self.assertEqual(s1.raw, "ASCII only".encode())
        self.assertIsNone(s2.encoding_name)        # 'こんにちは' fits cp932
        self.assertEqual(s2.raw, "こんにちは".encode("cp932"))
        s3 = lst[3 - 1 + 0].ivars  # sanity: index math below via re-extract
        again = rpgmruby.Reader().extract(self.full(root, out))
        self.assertIn("ASCII only\nこんにちは\nñandú", [l.source for l in again])
        s4 = lst[3].ivars["@parameters"][0]
        self.assertEqual(s4.encoding_name, "utf-8")  # ñ not in cp932 -> UTF-8 + E true
        self.assertTrue(s4.ivars["E"])

    def test_utf8_game_keeps_utf8(self):
        root = self.game(".rvdata2")
        _, _, out, _ = self.patch(root, {"はい、そうです": "はい、了解"})
        m = rm.loads(open(os.path.join(out, "Data", "Map001.rvdata2"), "rb").read())
        lst = m.ivars["@events"].get(1).ivars["@pages"][0].ivars["@list"]
        c = [c for c in lst if c.ivars["@indent"] == 1 and c.ivars["@code"] == 401][0]
        s = c.ivars["@parameters"][0]
        self.assertEqual(s.raw, "はい、了解".encode())
        self.assertEqual(s.encoding_name, "utf-8")

    def test_choice_and_database_patch(self):
        for ext, root in self.each_ext():
            _, _, out, written = self.patch(root, {"いいえ": "No", "薬草": "Herb", "レベル": "Lv"})
            self.assertEqual(sorted(written), sorted(
                ["Data/Map001" + ext, "Data/Items" + ext, "Data/System" + ext]))
            again = [l.source for l in rpgmruby.Reader().extract(self.full(root, out))]
            for t in ("No", "Herb", "Lv"):
                self.assertIn(t, again)

    def test_untouched_files_byte_identical_and_not_written(self):
        for ext, root in self.each_ext():
            r = rpgmruby.Reader()
            lines = r.extract(root)
            out = os.path.join(self.tmp.name, "noop" + ext)
            self.assertEqual(r.apply(root, out, lines), [])
            self.assertFalse(os.path.exists(out))
            for l in lines:  # translation equal to source also counts as unchanged
                l.translation = l.source
            self.assertEqual(r.apply(root, out, lines), [])
            for f in os.listdir(os.path.join(root, "Data")):
                if f.startswith("Scripts"):
                    continue
                raw = open(os.path.join(root, "Data", f), "rb").read()
                self.assertEqual(rm.dumps(rm.loads(raw)), raw)

    def test_stale_locator_raises(self):
        root = self.game(".rvdata2")
        r = rpgmruby.Reader()
        lines = r.extract(root)
        blk = next(l for l in lines if l.source.startswith("おはよう"))
        blk.translation = "x"
        # game updated: rewrite the map with different text
        files = build_files(".rvdata2")
        lst = files["Map001"].ivars["@events"].get(1).ivars["@pages"][0].ivars["@list"]
        lst[1].ivars["@parameters"][0] = S("変更された")
        open(os.path.join(root, "Data", "Map001.rvdata2"), "wb").write(rm.dumps(files["Map001"]))
        with self.assertRaises(PatchError):
            r.apply(root, os.path.join(self.tmp.name, "o1"), lines)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "o1")))
        # stale db string
        root2 = self.game(".rvdata")
        lines2 = r.extract(root2)
        it = next(l for l in lines2 if l.source == "薬草")
        it.translation = "Herb"
        it.locator = dict(it.locator, src="違う")
        with self.assertRaises(PatchError):
            r.apply(root2, os.path.join(self.tmp.name, "o2"), lines2)
        # stale choice index
        lines3 = r.extract(root2)
        ch = next(l for l in lines3 if l.kind == "choice")
        ch.translation = "Y"
        ch.locator = dict(ch.locator, ch=9)
        with self.assertRaises(PatchError):
            r.apply(root2, os.path.join(self.tmp.name, "o3"), lines3)

    def test_refuses_file_that_does_not_redump_identically(self):
        root = self.game(".rvdata2")
        p = os.path.join(root, "Data", "Items.rvdata2")
        raw = open(p, "rb").read()
        open(p, "wb").write(raw + b"")  # still fine
        r = rpgmruby.Reader()
        lines = r.extract(root)
        # float written non-canonically but equal -> RFloat.raw keeps it identical, so craft trailing junk instead
        open(p, "wb").write(raw + b"\x00")
        with self.assertRaises(ValueError):
            r.extract(root)
        open(p, "wb").write(raw)
        next(l for l in lines if l.source == "薬草").translation = "Herb"
        self.assertEqual(r.apply(root, os.path.join(self.tmp.name, "o4"), lines),
                         ["Data/Items.rvdata2"])


if __name__ == "__main__":
    unittest.main()
