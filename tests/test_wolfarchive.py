#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wolfarchive tests. Archives are SYNTHETIC: built by the small DXA v8 writer below, from the
format as documented in wolfarchive.py -- not copied from a game. The writer reuses
wolfarchive.make_key / huffman_tree (their real-data behaviour is checked in realcheck_wolf.py,
outside the repo). Run: PYTHONUTF8=1 python test_wolfarchive.py"""

import os
import shutil
import struct
import tempfile
import unittest

from rpgt.readers import wolfarchive as wa

NOT_SET = wa.NOT_SET


# ---------------------------------------------------------------- encoders (test-only)

def lz_encode(data, match=True):
    """Valid DxLib LZ stream. Literal runs plus greedy back-references (len>=4, dist<=256)."""
    used = set(data)
    key = next((b for b in range(256) if b not in used), 0)
    out = bytearray()
    i = 0
    while i < len(data):
        best = (0, 0)
        if match:
            for d in range(1, min(i, 256) + 1):
                n = 0
                while i + n < len(data) and n < 0x1FFF + 4 and data[i + n] == data[i + n - d]:
                    n += 1
                if n > best[0]:
                    best = (n, d)
        if best[0] >= 4:
            n, d = best
            cm = n - 4
            code = ((cm & 0x1F) << 3) | (4 if cm >> 5 else 0) | 0  # 1-byte distance
            out.append(key)
            out.append(code + 1 if code >= key else code)
            if cm >> 5:
                out.append(cm >> 5)
            out.append(d - 1)
            i += n
            continue
        b = data[i]
        out += bytes([b, b]) if b == key else bytes([b])
        i += 1
    return struct.pack("<II", len(data), len(out) + 9) + bytes([key]) + bytes(out)


def huffman_encode(data):
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    top = max(freq) or 1
    weight = [(f * 60000) // top for f in freq]  # fits the u16 weights; zero-weight symbols stay legal
    weight = [max(1, w) if f else w for w, f in zip(weight, freq)]
    child, root = wa.huffman_tree(weight)
    code = {}
    stack = [(root, [])]
    while stack:
        node, bits = stack.pop()
        if node < 256:
            code[node] = bits
        else:
            a, b = child[node]
            stack.append((a, bits + [0]))
            stack.append((b, bits + [1]))
    bits = []

    def put(v, n):
        bits.extend((v >> (n - 1 - k)) & 1 for k in range(n))

    nb = max(1, len(data).bit_length())
    put(nb - 1, 6)
    put(len(data), nb)
    put(0, 6)  # packed size: the decoder ignores it
    put(0, 1)
    prev = 0
    for i, w in enumerate(weight):
        delta = w if i == 0 else abs(w - prev)
        minus = 0 if i == 0 else int(w < prev)
        nbits = max(2, ((max(1, delta.bit_length()) + 1) // 2) * 2)
        put(nbits // 2 - 1, 3)
        put(minus, 1)
        put(delta, nbits)
        prev = w
    while len(bits) % 8:
        bits.append(0)
    head = bytes(sum(bits[k + j] << (7 - j) for j in range(8)) for k in range(0, len(bits), 8))
    body = []
    for b in data:
        body.extend(code[b])  # LSB-first stream
    while len(body) % 8:
        body.append(0)
    return head + bytes(sum(body[k + j] << j for j in range(8)) for k in range(0, len(body), 8))


def xor(data, key, pos):
    return wa._xor(data, key, pos)


# ---------------------------------------------------------------- archive writer

def name_blob(name):
    raw = name.encode("cp932")
    up = raw.upper()
    n4 = (len(up) + 1 + 3) // 4
    return struct.pack("<HH", n4, sum(up) & 0xFFFF) + up.ljust(n4 * 4, b"\0") + raw + b"\0"


def build_archive(tree, keystring=b"", crypt=0x13A, no_key=False, mode="lz", huff_kb=1, head_press=True):
    """tree: {"a.dat": bytes, "Sub": {"b.dat": bytes}}. mode: raw | lz | huff | mix (rotates)."""
    names = bytearray()
    files = bytearray()   # 72-byte heads, grouped per directory
    dirs = bytearray()    # 32-byte records
    data = bytearray()
    modes = ["raw", "lz", "huff"] if mode == "mix" else [mode]
    counter = [0]

    def add_name(n):
        off = len(names)
        names.extend(name_blob(n))
        return off

    def emit_dir(entries, dir_uppers, own_fh, parent_dir_off, dir_off):
        # reserve this directory's record, then lay out its file heads contiguously
        fh_off = len(files)
        files.extend(b"\0" * (72 * len(entries)))
        dirs.extend(struct.pack("<QQQQ", own_fh, parent_dir_off, len(entries), fh_off))
        for i, (nm, val) in enumerate(entries):
            at = fh_off + 72 * i
            noff = add_name(nm)
            if isinstance(val, dict):
                sub_off = len(dirs)
                files[at:at + 72] = struct.pack("<QQQQQQQQQ", noff, wa.ATTR_DIR, 0, 0, 0, sub_off, 0, NOT_SET, NOT_SET)
                emit_dir(list(val.items()), [nm.encode("cp932").upper()] + dir_uppers, at, dir_off, sub_off)
            else:
                m = modes[counter[0] % len(modes)]
                counter[0] += 1
                up = nm.encode("cp932").upper()
                key = None if no_key else wa.make_key(keystring + up + b"".join(dir_uppers))
                size, press, huff = len(val), NOT_SET, NOT_SET
                comp = val
                if m in ("lz", "huff") and size:
                    comp = lz_encode(val) if m == "lz" or True else val
                    press = len(comp)
                if m == "raw":
                    stored = xor(val, key, size)
                elif m == "lz":
                    stored = xor(comp, key, size)
                else:  # huffman over the front/back huff_kb of the (LZ) data
                    kb = huff_kb * 1024
                    if len(comp) > kb * 2:
                        hd = huffman_encode(comp[:kb] + comp[-kb:])
                        mid = comp[kb:-kb]
                    else:
                        hd, mid = huffman_encode(comp), b""
                    huff = len(hd)
                    stored = xor(hd, key, size) + xor(mid, key, size + huff)
                addr = len(data)
                data.extend(stored)
                files[at:at + 72] = struct.pack("<QQQQQQQQQ", noff, 0x20, 0, 0, 0, addr, size, press, huff)

    emit_dir(list(tree.items()), [], NOT_SET, NOT_SET, 0)
    tbl = bytes(names)
    file_off = len(tbl)
    tbl += bytes(files)
    dir_off = len(tbl)
    tbl += bytes(dirs)
    # the root record's FileHeadAddress must be relative to the file table: it already is (emitted first)
    block = tbl
    if head_press:
        block = huffman_encode(lz_encode(tbl))
    flags = (crypt << 16) | (1 if no_key else 0) | (0 if head_press else 2)
    key0 = None if no_key else wa.make_key(keystring)
    name_off = 64 + len(data)
    head = b"DX" + struct.pack("<HIQQQQIIB", 8, len(tbl), 64, name_off, file_off, dir_off, 932, flags, huff_kb)
    head += b"\0" * (64 - len(head))
    return head + bytes(data) + xor(block, key0, 0)


# ---------------------------------------------------------------- tests

SAMPLE = {
    "Game.dat": b"G" * 3000 + bytes(range(256)) * 20,       # compressible + all byte values
    "MapData": {"Map1.mps": bytes(range(200)) * 3, "Empty.mps": b""},
    "note.txt": "ほげ".encode("cp932") * 2000,
    "tiny.dat": b"abc",
}


def flat(tree, pre=""):
    out = {}
    for k, v in tree.items():
        if isinstance(v, dict):
            out.update(flat(v, pre + k + "/"))
        else:
            out[pre + k] = v
    return out


class WolfArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def arc(self, name="t.wolf", **kw):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(build_archive(kw.pop("tree", SAMPLE), **kw))
        return p

    def test_codec_roundtrips(self):
        blobs = [b"", b"x", b"abcabcabcabcabcabc" * 50, bytes(range(256)) * 9, b"\0" * 5000]
        for b in blobs:
            self.assertEqual(wa.lz_decode(lz_encode(b)), b)
            self.assertEqual(wa.lz_decode(lz_encode(b, match=False)), b)
            if b:
                self.assertEqual(wa.huffman_decode(huffman_encode(b)), b)

    def test_lz_has_real_matches(self):
        data = b"hello world, " * 40
        self.assertLess(len(lz_encode(data)), len(data) // 2)  # the encoder really emits references
        self.assertEqual(wa.lz_decode(lz_encode(data)), data)

    def test_key_derivation_shape(self):
        k = wa.make_key(b"abcdefgh")
        self.assertEqual(len(k), 7)
        self.assertEqual(wa.make_key(b"ab"), wa.make_key(b"abDXLIBARC"))  # short strings get the default appended

    def test_xor_offset(self):
        k = wa.make_key(b"somekey!")
        d = bytes(range(50))
        self.assertEqual(xor(xor(d, k, 13), k, 13), d)
        whole = xor(d, k, 0)
        self.assertEqual(xor(d[10:], k, 10), whole[10:])  # position-dependent, as in DxLib

    def test_modes_roundtrip_with_wolf_key(self):
        for mode in ("raw", "lz", "huff", "mix"):
            with self.subTest(mode=mode):
                p = self.arc(keystring=wa.WOLF_KEYS[0x13A], mode=mode)
                self.assertTrue(wa.is_dxa(p))
                self.assertEqual(wa.detect_key(p), wa.WOLF_KEYS[0x13A])
                out = os.path.join(self.tmp, "out_" + mode)
                written = wa.unpack(p, out)
                want = flat(SAMPLE)
                self.assertEqual(sorted(written), sorted(want))
                for rel, data in want.items():
                    with open(os.path.join(out, *rel.split("/")), "rb") as f:
                        self.assertEqual(f.read(), data, rel)

    def test_huffman_front_back_split(self):
        big = {"big.bin": os.urandom(1500) + b"A" * 8000 + os.urandom(1500)}
        p = self.arc(tree=big, mode="huff", huff_kb=1, keystring=wa.WOLF_KEYS[0x13A])
        es = wa.list_entries(p)
        self.assertEqual(es[0].size, 11000)
        out = os.path.join(self.tmp, "o")
        wa.unpack(p, out)
        with open(os.path.join(out, "big.bin"), "rb") as f:
            self.assertEqual(f.read(), big["big.bin"])

    def test_no_key_and_uncompressed_head_and_zero_key(self):
        for kw in ({"no_key": True}, {"head_press": False, "keystring": b""}, {"crypt": 0, "keystring": b""}):
            with self.subTest(kw=kw):
                p = self.arc(mode="mix", **kw)
                self.assertEqual(wa.detect_key(p), kw.get("keystring", b""))
                self.assertEqual(sorted(e.path for e in wa.list_entries(p)), sorted(flat(SAMPLE)))

    def test_wrong_key_is_an_error_not_garbage(self):
        p = self.arc(keystring=b"secret-key-1234")
        self.assertIsNone(wa.detect_key(p))
        with self.assertRaises(wa.DxaError):
            wa.list_entries(p, b"nope-nope-nope")
        with self.assertRaises(wa.DxaError):
            wa.unpack(p, os.path.join(self.tmp, "x"))
        self.assertEqual(len(wa.list_entries(p, b"secret-key-1234")), 5)  # explicit key works

    def test_unsupported_and_foreign(self):
        p = self.arc(crypt=0x14B, keystring=wa.WOLF_KEYS[0x13A])
        with self.assertRaises(wa.DxaError) as cm:
            wa.detect_key(p)
        self.assertIn("not supported", str(cm.exception))
        junk = os.path.join(self.tmp, "j.wolf")
        with open(junk, "wb") as f:
            f.write(b"\x01\x02\x03")
        self.assertFalse(wa.is_dxa(junk))
        self.assertFalse(wa.is_dxa(os.path.join(self.tmp, "missing")))

    def test_only_filter_and_sjis_names(self):
        tree = {"日本語.mps": b"jp" * 100, "x.png": b"png" * 100, "d": {"y.dat": b"y"}}
        p = self.arc(tree=tree, keystring=wa.WOLF_KEYS[0x13A], mode="mix")
        out = os.path.join(self.tmp, "o")
        self.assertEqual(sorted(wa.unpack(p, out, only=["*.mps", "d/*"])), ["d/y.dat", "日本語.mps"])
        self.assertFalse(os.path.exists(os.path.join(out, "x.png")))

    def test_anti_unpack_notice_stripped(self):
        tree = {"CommonEvent.dat": wa.ANTI_UNPACK + b"REAL", "Other.dat": wa.ANTI_UNPACK + b"keep"}
        p = self.arc(tree=tree, keystring=wa.WOLF_KEYS[0x13A], mode="raw")
        out = os.path.join(self.tmp, "o")
        wa.unpack(p, out)
        with open(os.path.join(out, "CommonEvent.dat"), "rb") as f:
            self.assertEqual(f.read(), b"REAL")
        with open(os.path.join(out, "Other.dat"), "rb") as f:
            self.assertEqual(f.read(), wa.ANTI_UNPACK + b"keep")

    def test_unpack_game_data_layout_and_defaults(self):
        data = os.path.join(self.tmp, "Data")
        os.makedirs(data)
        k = wa.WOLF_KEYS[0x13A]
        for name, tree in (("BasicData.wolf", {"Game.dat": b"g" * 50, "a.png": b"p"}),
                           ("MapData.wolf", {"Map1.mps": b"m" * 50}),
                           ("BGM.wolf", {"song.ogg": b"o" * 50}),
                           ("Extra.wolf", {"Late.mps": b"l" * 50, "pic.png": b"p"}),
                           ("Pics.wolf", {"c.png": b"c"})):
            with open(os.path.join(data, name), "wb") as f:
                f.write(build_archive(tree, keystring=k))
        out = os.path.join(self.tmp, "mirror")
        res = wa.unpack_game_data(data, out)
        self.assertEqual(res["_errors"], {})
        self.assertEqual(sorted(k for k in res if k != "_errors"), ["BasicData.wolf", "Extra.wolf", "MapData.wolf"])
        self.assertTrue(os.path.isfile(os.path.join(out, "BasicData", "Game.dat")))
        self.assertTrue(os.path.isfile(os.path.join(out, "MapData", "Map1.mps")))
        self.assertTrue(os.path.isfile(os.path.join(out, "Extra", "Late.mps")))
        self.assertFalse(os.path.exists(os.path.join(out, "BasicData", "a.png")))  # media skipped
        self.assertFalse(os.path.exists(os.path.join(out, "BGM")))
        res = wa.unpack_game_data(data, os.path.join(self.tmp, "m2"), archives=["bgm"], exts=None)
        self.assertEqual(res["BGM.wolf"], ["BGM/song.ogg"])

    def test_monolithic_data_wolf_goes_to_data_root(self):
        data = os.path.join(self.tmp, "g")
        os.makedirs(data)
        with open(os.path.join(data, "Data.wolf"), "wb") as f:
            f.write(build_archive({"BasicData": {"Game.dat": b"g" * 20}, "MapData": {"M.mps": b"m"}},
                                  keystring=wa.WOLF_KEYS[0x13A]))
        out = os.path.join(self.tmp, "mirror")
        res = wa.unpack_game_data(data, out)
        self.assertEqual(sorted(res["Data.wolf"]), ["BasicData/Game.dat", "MapData/M.mps"])
        self.assertTrue(os.path.isfile(os.path.join(out, "BasicData", "Game.dat")))

    def test_path_traversal_refused(self):
        p = self.arc(tree={"..": {"evil.dat": b"x"}}, keystring=b"", mode="raw", no_key=True)
        with self.assertRaises(wa.DxaError):
            wa.unpack(p, os.path.join(self.tmp, "o"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
