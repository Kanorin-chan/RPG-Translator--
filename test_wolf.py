#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wolf reader tests. ALL data is synthetic, built here from the format as understood
from rewolf-trans/WolfTrans -- not from a real game. Run: PYTHONUTF8=1 python test_wolf.py"""

import os
import shutil
import tempfile
import unittest

import readers
from readers import wolf
from readers.base import PatchError


def u8(n): return bytes([n])
def u32(n): return n.to_bytes(4, "little")
def wstr(s, enc="cp932"):
    b = s.encode(enc)
    return u32(len(b) + 1) + b + b"\0"


def xor_crypt(data, seeds):
    # independent, deliberately plain restatement of the scheme
    data = bytearray(data)
    for seed, step in zip(seeds, (1, 2, 5)):
        for j in range(0, len(data), step):
            seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
            data[j] ^= (seed >> 28) & 7
    return bytes(data)


def cmd(cid, args=(), strs=(), indent=0, enc="cp932", move=False):
    b = u8(len(args) + 1) + u32(cid) + b"".join(u32(a) for a in args) + u8(indent)
    b += u8(len(strs)) + b"".join(wstr(s, enc) for s in strs)
    if move:  # terminator 1, 5 unknown, flags, 1 route (id 3, 1 arg, 01 00)
        return b + u8(1) + bytes(5) + u8(0) + u32(1) + u8(3) + u8(1) + u32(7) + b"\x01\x00"
    return b + u8(0)


def page(cmds, enc="cp932"):
    b = u32(0) + wstr("", enc) + bytes(4) + bytes(37) + bytes(4) + u8(0) + u8(0)
    b += u32(0)  # routes
    b += u32(len(cmds)) + b"".join(cmds) + wolf.COMMAND_END + bytes(3) + u8(0x7A)
    return b


def event(eid, name, pages, enc="cp932"):
    b = wolf.EVENT_START + u32(eid) + wstr(name, enc) + u32(1) + u32(2) + u32(len(pages)) + wolf.EVENT_END
    for p in pages:
        b += u8(0x79) + p
    return u8(0x6F) + b + u8(0x70)


def mps(events):
    return (wolf.JP_HEADER + u32(0) + u32(2) + u32(1) + u32(len(events)) + bytes(24)
            + b"".join(events) + u8(0x66))


def common(events_spec, enc="cp932"):
    b = wolf.CE_HEADER + u32(len(events_spec))
    for eid, name, cmds, strarg in events_spec:
        b += u8(0x8E) + u32(eid) + u32(0) + bytes(7) + wstr(name, enc)
        b += u32(len(cmds)) + b"".join(cmds) + wstr("", enc) + wstr("desc", enc) + u8(0x8F)
        b += u32(1) + wstr(strarg, enc)           # strArgs
        b += u32(1) + u8(5)                       # byteArgs
        b += u32(1) + u32(1) + wstr("選択肢", enc)  # spOptionArgs
        b += u32(1) + u32(1) + u32(9)             # spOptionValArgs
        b += u32(1) + u32(4)                      # intArgs
        b += bytes(5) + b"".join(wstr("", enc) for _ in range(100))
        b += u8(0x91) + wstr("", enc) + u8(0x91)
    return b + u8(0x8F)


def db_files(enc="cp932", encrypted=False):
    """One type, 2 fields (int 'HP', string 'name' type0 + string 'help' type0), 2 entries."""
    proj = u32(1) + wstr("アイテム", enc)
    proj += u32(3) + wstr("HP", enc) + wstr("名前", enc) + wstr("説明", enc)
    proj += u32(2) + wstr("薬草", enc) + wstr("毒消し", enc)
    proj += wstr("", enc)
    proj += u32(3) + u8(0) + u8(0) + u8(0)
    proj += u32(3) + wstr("", enc) * 3
    proj += u32(3) + u32(0) + u32(0) + u32(0)
    proj += u32(3) + u32(0) + u32(0) + u32(0)
    proj += u32(3) + u32(0) + u32(0) + u32(0)
    dat = u32(1) + wolf.DB_SEP + u32(0) + u32(3)
    dat += u32(0x3E8) + u32(wolf.STRING_START) + u32(wolf.STRING_START + 1)
    dat += u32(2)
    for hp, nm, ds in ((10, "薬草", "傷を治す。\n少し。"), (0, "毒消し", "毒を治す")):
        dat += u32(hp) + wstr(nm, enc) + wstr(ds, enc)
    dat += u8(0xC1)
    if encrypted:
        hdr = bytes([0x41, 9, 9, 0x13, 9, 9, 0x27, 9, 9, 9])
        body = xor_crypt(wolf.DB_HEADER[10:] + dat, [hdr[i] for i in wolf.DB_SEEDS])
        return proj, hdr + body
    return proj, wolf.DB_HEADER + dat


def game_dat(title, enc="cp932", encrypted=False):
    body = u32(2) + b"\x01\x02" + u32(9) + wstr(title, enc) + wstr("0000-0000", enc)
    body += u32(0) + wstr("MS Gothic") + wstr("") * 3 + wstr("pc") + wstr("1.0")
    tail = b"TAILBYTES"
    if encrypted:
        total = 10 + len(body) + 4 + len(tail)
        hdr = bytes([0x55, 1, 2, 3, 4, 5, 6, 7, 0x66, 9])
        payload = body + u32(total - 1) + tail
        return hdr + xor_crypt(payload, [hdr[i] for i in wolf.GAME_SEEDS])
    head = b"\x00" + wolf.GAME_MAGIC
    return head + body + u32(len(head) + len(body) + 4 + len(tail) - 1) + tail


def rd(path):
    with open(path, "rb") as f:
        return f.read()


def write(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def make_game(root, enc="cp932", db_encrypted=False, game_encrypted=False):
    m1 = mps([event(1, "村長", [page([
        cmd(101, [0], ["こんにちは。\n旅人さん。"], enc=enc),
        cmd(102, [0, 2], ["はい", "いいえ"], enc=enc),
        cmd(201, [], [], move=True),
        cmd(122, [0], ["勇者"], enc=enc),
        cmd(122, [1], ["abc"], enc=enc),
        cmd(103, [], ["作者メモ"], enc=enc),
        cmd(999, [], ["謎の命令"], enc=enc),
        cmd(101, [0], ["  "], enc=enc),
    ], enc)], enc), event(2, "", [page([cmd(101, [0], ["二つ目"], enc=enc)], enc)], enc)])
    m10 = mps([event(1, "x", [page([cmd(101, [0], ["地図十"], enc=enc)], enc)], enc)])
    m2 = mps([event(1, "x", [page([cmd(101, [0], ["地図二"], enc=enc)], enc)], enc)])
    write(root, "Data/MapData/Map10.mps", m10)
    write(root, "Data/MapData/Map1.mps", m1)
    write(root, "Data/MapData/Map2.mps", m2)
    write(root, "Data/BasicData/CommonEvent.dat", common(
        [(0, "共通", [cmd(101, [0], ["共通の台詞"], enc=enc)], "引数")], enc))
    proj, dat = db_files(enc, db_encrypted)
    write(root, "Data/BasicData/DataBase.project", proj)
    write(root, "Data/BasicData/DataBase.dat", dat)
    write(root, "Data/BasicData/Game.dat", game_dat("冒険の書", enc, game_encrypted))


class WolfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "game")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def extract(self, **kw):
        r = wolf.Reader(**kw)
        return r, r.extract(self.root)

    # -- crypto / detect
    def test_crypt_roundtrip_and_changes(self):
        d = bytes(range(256)) * 3
        e = bytearray(d)
        wolf.crypt(e, [7, 8, 9])
        self.assertNotEqual(bytes(e), d)
        self.assertEqual(bytes(e), xor_crypt(d, [7, 8, 9]))
        wolf.crypt(e, [7, 8, 9])
        self.assertEqual(bytes(e), d)

    def test_detect_and_registry(self):
        self.assertFalse(wolf.Reader.detect(self.root))
        make_game(self.root)
        self.assertTrue(wolf.Reader.detect(self.root))
        self.assertEqual(wolf.archive_status(self.root), "extracted")
        self.assertIs(readers.READERS["wolf"], wolf.Reader)

    def test_archive_only(self):
        os.makedirs(self.root)
        write(self.root, "Data.wolf", b"\x01\x02")
        self.assertFalse(wolf.Reader.detect(self.root))
        self.assertEqual(wolf.archive_status(self.root), "archive-encrypted")
        self.assertEqual(wolf.archive_status(os.path.join(self.tmp, "nothing")), "none")

    def test_rpgmaker_data_dir_not_detected(self):
        write(self.root, "data/Map001.json", b"{}")
        self.assertFalse(wolf.Reader.detect(self.root))

    # -- extract
    def test_extract_order_kinds(self):
        make_game(self.root)
        r, lines = self.extract()
        self.assertEqual(r.warnings, [])
        self.assertEqual([l.order for l in lines], list(range(len(lines))))
        got = [(l.kind, l.source) for l in lines]
        self.assertEqual(got, [
            ("dialogue", "こんにちは。\n旅人さん。"), ("choice", "はい"), ("choice", "いいえ"),
            ("other", "勇者"), ("script", "作者メモ"), ("script", "謎の命令"),
            ("dialogue", "二つ目"),
            ("dialogue", "地図二"), ("dialogue", "地図十"),   # natural sort: Map1, Map2, Map10
            ("dialogue", "共通の台詞"), ("script", "引数"), ("script", "選択肢"),
            ("name", "薬草"), ("desc", "傷を治す。\n少し。"), ("name", "毒消し"), ("desc", "毒を治す"),
            ("other", "冒険の書"),
        ])
        self.assertEqual(lines[0].id, "Data/MapData/Map1.mps#e1/p0/c0/s0")
        self.assertEqual(lines[0].scene, "Map1/ev1:村長")
        self.assertEqual(lines[6].scene, "Map1/ev2")
        self.assertEqual(len({l.id for l in lines}), len(lines))
        self.assertEqual(r.game_encoding, "cp932")

    def test_encrypted_db_and_game(self):
        make_game(self.root, db_encrypted=True, game_encrypted=True)
        r, lines = self.extract()
        self.assertEqual(r.warnings, [])
        srcs = [l.source for l in lines]
        self.assertIn("薬草", srcs)
        self.assertIn("冒険の書", srcs)
        a = len(srcs)
        shutil.rmtree(self.root)
        make_game(self.root)
        self.assertEqual(a, len(self.extract()[1]))

    def test_bad_file_warns_not_crashes(self):
        make_game(self.root)
        write(self.root, "Data/MapData/Map3.mps", wolf.JP_HEADER + b"junk")
        r, lines = self.extract()
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("Map3.mps", r.warnings[0][0])
        self.assertTrue(lines)

    # -- apply
    def test_nothing_translated_writes_nothing(self):
        make_game(self.root)
        r, lines = self.extract()
        self.assertEqual(r.apply(self.root, self.out, lines), [])
        for l in lines[:3]:
            l.translation = l.source  # same as source: still untouched
        self.assertEqual(r.apply(self.root, self.out, lines), [])
        self.assertFalse(os.path.exists(self.out))

    def _roundtrip(self, enc_kw, text, **mk):
        make_game(self.root, **mk)
        r, lines = self.extract(**enc_kw)
        before = {f: rd(os.path.join(self.root, *f.split("/")))
                  for f in {l.file for l in lines}}
        pick = [l for l in lines if l.file.endswith(("Map1.mps", "CommonEvent.dat", "DataBase.dat", "Game.dat"))]
        for l in pick:
            l.translation = text + l.source[:1]
        written = r.apply(self.root, self.out, lines)
        self.assertEqual(sorted(written), sorted({l.file for l in pick}))
        for f, b in before.items():  # source tree untouched
            self.assertEqual(rd(os.path.join(self.root, *f.split("/"))), b)
        self.assertFalse(os.path.exists(os.path.join(self.out, "Data/MapData/Map2.mps")))
        r2 = wolf.Reader(**enc_kw)
        lines2 = r2.extract(self.out) if False else None
        # re-extract from a tree made of the patched files over the originals
        merged = os.path.join(self.tmp, "merged")
        shutil.copytree(self.root, merged)
        for f in written:
            shutil.copyfile(os.path.join(self.out, *f.split("/")), os.path.join(merged, *f.split("/")))
        lines2 = r2.extract(merged)
        self.assertEqual(r2.warnings, [])
        return pick, lines, lines2, written

    def test_utf8_translation_roundtrip(self):
        pick, lines, lines2, written = self._roundtrip({"write_encoding": "utf-8"}, "你好 ")
        self.assertEqual(len(lines), len(lines2))
        for a, b in zip(lines, lines2):
            self.assertEqual(a.id, b.id)
            self.assertEqual(b.source, a.translation or a.source)

    def test_encrypted_files_reencrypted_and_game_size_fixed(self):
        pick, lines, lines2, written = self._roundtrip(
            {"write_encoding": "utf-8"}, "你好 ", db_encrypted=True, game_encrypted=True)
        for f in ("Data/BasicData/DataBase.dat", "Data/BasicData/Game.dat"):
            self.assertNotEqual(rd(os.path.join(self.out, *f.split("/")))[0], 0)
        g = rd(os.path.join(self.out, "Data/BasicData/Game.dat"))
        payload, hdr = wolf._decrypt(g, wolf.GAME_SEEDS)
        szoff = [l for l in lines if l.file.endswith("Game.dat")][0].locator["szoff"]
        # title grew; size field = new file length - 1
        shift = len(g) - len(rd(os.path.join(self.root, "Data/BasicData/Game.dat")))
        self.assertGreater(shift, 0)
        self.assertEqual(int.from_bytes(payload[szoff + shift:][:4], "little"), len(g) - 1)

    def test_game_size_fixed_unencrypted(self):
        make_game(self.root)
        r, lines = self.extract()
        g = [l for l in lines if l.file.endswith("Game.dat")][0]
        self.assertTrue(g.locator["szfix"])
        g.translation = "冒険の書の長いタイトル"
        r.apply(self.root, self.out, [g])
        data = rd(os.path.join(self.out, "Data/BasicData/Game.dat"))
        shift = len(data) - len(rd(os.path.join(self.root, "Data/BasicData/Game.dat")))
        self.assertGreater(shift, 0)
        self.assertEqual(int.from_bytes(data[g.locator["szoff"] + shift:][:4], "little"), len(data) - 1)
        self.assertEqual(data[-9:], b"TAILBYTES")

    def test_cp932_vs_utf8_write(self):
        make_game(self.root)
        r, lines = self.extract()
        l = lines[0]
        l.translation = "こんにちは世界"            # encodable in cp932
        r = wolf.Reader(write_encoding="cp932")
        r.apply(self.root, self.out, [l])
        raw = rd(os.path.join(self.out, "Data/MapData/Map1.mps"))
        self.assertIn("こんにちは世界".encode("cp932"), raw)
        l.translation = "你好，世界"                # Chinese: not in cp932
        with self.assertRaises(PatchError):
            wolf.Reader(write_encoding="cp932").apply(self.root, self.out, [l])
        wolf.Reader(write_encoding="utf-8").apply(self.root, self.out, [l])
        raw = rd(os.path.join(self.out, "Data/MapData/Map1.mps"))
        self.assertIn("你好，世界".encode("utf-8"), raw)
        wolf.Reader(write_encoding="gbk").apply(self.root, self.out, [l])
        raw = rd(os.path.join(self.out, "Data/MapData/Map1.mps"))
        self.assertIn("你好，世界".encode("gbk"), raw)

    def test_utf8_game_detected(self):
        make_game(self.root, enc="utf-8")
        r, lines = self.extract()
        self.assertEqual(r.game_encoding, "utf-8")
        self.assertEqual(lines[0].source, "こんにちは。\n旅人さん。")

    def test_stale_locator(self):
        make_game(self.root)
        r, lines = self.extract()
        l = lines[0]
        l.translation = "x"
        p = os.path.join(self.root, "Data/MapData/Map1.mps")
        data = rd(p).replace("こんにちは".encode("cp932"), "こんばんは".encode("cp932"))
        with open(p, "wb") as f:
            f.write(data)
        with self.assertRaises(PatchError):
            r.apply(self.root, self.out, [l])
        l.locator = dict(l.locator, off=l.locator["off"] + 1)
        with self.assertRaises(PatchError):
            r.apply(self.root, self.out, [l])
        l.locator = {}
        with self.assertRaises(PatchError):
            r.apply(self.root, self.out, [l])


if __name__ == "__main__":
    unittest.main(verbosity=1)
