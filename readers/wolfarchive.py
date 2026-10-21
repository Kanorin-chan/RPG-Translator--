#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reader for Wolf RPG `.wolf` archives = DxLib "DX" archives (DXA), version 8.

Format facts come from the DxLib archive code (zlib licence, Takumi Yamada) as vendored in
Sinflower/UberWolf and WolfDec (3rdParty/DXLib/DXArchive.*, Huffman.*; UberWolf is MIT) and
UberWolf's key table (WolfDec.cpp). Read for format facts only; this module is original Python.
Layout, all little-endian:
  0x00 head (64 bytes, NOT encrypted): 'DX', u16 ver=8, u32 head_size, u64 data_start,
       u64 name_table_off, u64 file_table_off (relative to the table block), u64 dir_table_off
       (relative to the table block), u32 codepage, u32 flags (low 16: bit0 no-key, bit1 head
       not compressed; high 16: Wolf "crypt version", e.g. 0x13A = Wolf 3.14), u8 huffman_kb, 15 reserved.
  data_start..name_table_off: file data.
  name_table_off..EOF: the table block (names, 72-byte file heads, 32-byte dir records), stored
       Huffman(LZ(block)) and then XORed with the 7-byte key at key-offset 0.
Key: 7 bytes from a key STRING: crc32(even-index bytes) LE (4 bytes) + crc32(odd-index bytes) LE
(3 bytes). Per file the string is  keystring + UPPERCASE(file name) + UPPERCASE(each parent dir name,
innermost first, root excluded); the XOR offset is the file's DataSize (+ section offset), not 0.
Wolf's keystring is a per-version constant (see WOLF_KEYS), so a keyed archive from a known Wolf
version needs no password. Crypt versions >= 0x14B (Wolf 3.31+) use AES/ChaCha with a key
derived from Game.dat: NOT supported here (reported as an error, never guessed).
Standard library only.
"""

import fnmatch
import os
import struct
import zlib
from collections import namedtuple

MAGIC = b"DX"
HEAD_SIZE = 64
FILEHEAD_SIZE = 72
NOT_SET = 0xFFFFFFFFFFFFFFFF
ATTR_DIR = 0x10
MIN_COMPRESS = 4
ANTI_UNPACK = b"Extracting data from encrypted files violates the guidelines.\x00"

# Wolf keystrings by crypt version (high half of flags). From UberWolf WolfDec.cpp (public constants).
# 0 = pre-3.0 archives that carry no version; tried in order by detect_key.
WOLF_KEYS = {
    0x12C: bytes([0x0F, 0x53, 0xE1, 0x3E, 0x8E, 0xB5, 0x41, 0x91, 0x52, 0x16, 0x55, 0xAE, 0x34, 0xC9, 0x8F,
                  0x79, 0x59, 0x2F, 0x59, 0x6B, 0x95, 0x19, 0x9B, 0x1B, 0x35, 0x9A, 0x2F, 0xDE, 0xC9, 0x7C,
                  0x12, 0x96, 0xC3, 0x14, 0xB5, 0x0F, 0x53, 0xE1, 0x3E, 0x8E]),
    0x13A: bytes([0x31, 0xF9, 0x01, 0x36, 0xA3, 0xE3, 0x8D, 0x3C, 0x7B, 0xC3, 0x7D, 0x25, 0xAD, 0x63, 0x28,
                  0x19, 0x1B, 0xF7, 0x8E, 0x6C, 0xC4, 0xE5, 0xE2, 0x76, 0x82, 0xEA, 0x4F, 0xED, 0x61, 0xDA,
                  0xE0, 0x44, 0x5B, 0xB6, 0x46, 0x3B, 0x06, 0xD5, 0xCE, 0xB6, 0x78, 0x58, 0xD0, 0x7C, 0x82]),
}
FALLBACK_KEYS = [b"WLFRPrO!p(;s5((8P@((UFWlu$#5(=", b"nGui9('&1=@3#a", b"Ph=X3^]o2A(,1=@3#a", b"DXBDXARC"]
UNSUPPORTED_CRYPT = 0x14B  # versions at/above this (and 0x64/0xC8/>=1000) are AES/ChaCha

# Default unpack: archives that hold what the Wolf reader needs, and only these file types.
DEFAULT_ARCHIVES = ("basicdata", "mapdata", "systemfile")  # plus "data" (monolithic)
DEFAULT_EXTS = (".mps", ".dat", ".project")

Entry = namedtuple("Entry", "path size packed is_dir")


class DxaError(Exception):
    pass


# ---------------------------------------------------------------- key

def make_key(keystring):
    """7 key bytes from a key string (DxLib KeyCreate)."""
    if len(keystring) < 4:
        keystring = keystring + b"DXLIBARC"
    k0 = zlib.crc32(keystring[0::2]) & 0xFFFFFFFF
    k1 = zlib.crc32(keystring[1::2]) & 0xFFFFFFFF
    return struct.pack("<I", k0) + struct.pack("<I", k1)[:3]


def _xor(data, key, pos):
    """XOR with the repeating 7-byte key starting at key offset `pos`."""
    if key is None or not data:
        return bytes(data)
    n = len(data)
    pos %= 7
    rot = key[pos:] + key[:pos]
    reps = (n + 6) // 7
    ks = (rot * reps)[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(ks, "little")).to_bytes(n, "little")


# ---------------------------------------------------------------- decompression

def lz_decode(src):
    """DxLib LZ: u32 out_size, u32 packed_size(incl. 9-byte head), u8 keycode, body."""
    if len(src) < 9:
        raise DxaError("lz: truncated")
    dest_size, packed = struct.unpack_from("<II", src, 0)
    keycode = src[8]
    out = bytearray()
    sp, end = 9, min(packed, len(src))
    while sp < end:
        b = src[sp]
        if b != keycode:
            out.append(b)
            sp += 1
            continue
        if src[sp + 1] == keycode:
            out.append(keycode)
            sp += 2
            continue
        code = src[sp + 1]
        if code > keycode:
            code -= 1
        sp += 2
        conbo = code >> 3
        if code & 4:
            conbo |= src[sp] << 5
            sp += 1
        conbo += MIN_COMPRESS
        sz = code & 3
        if sz == 0:
            index = src[sp]
            sp += 1
        elif sz == 1:
            index = src[sp] | (src[sp + 1] << 8)
            sp += 2
        else:
            index = src[sp] | (src[sp + 1] << 8) | (src[sp + 2] << 16)
            sp += 3
        index += 1
        if index > len(out):
            raise DxaError("lz: back-reference before start")
        if index >= conbo:
            s = len(out) - index
            out += out[s:s + conbo]
        else:
            for _ in range(conbo):  # overlapping copy repeats the pattern
                out.append(out[-index])
    if len(out) != dest_size:
        raise DxaError("lz: got %d bytes, header says %d" % (len(out), dest_size))
    return bytes(out)


class _Bits:
    def __init__(self, b):
        self.b, self.pos = b, 0  # pos in bits, MSB first

    def read(self, n):
        v = 0
        for _ in range(n):
            byte = self.b[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def nbytes(self):
        return (self.pos + 7) >> 3


def huffman_decode(src):
    """DxLib Huffman (256 symbols, weights stored as deltas). Returns decoded bytes."""
    bs = _Bits(src)
    try:
        orig = bs.read(bs.read(6) + 1)
        bs.read(bs.read(6) + 1)  # packed size, unused
        weight = []
        for i in range(256):
            bits = (bs.read(3) + 1) * 2
            minus = bs.read(1)
            v = bs.read(bits)
            weight.append(v if i == 0 else ((weight[-1] - v) if minus else (weight[-1] + v)) & 0xFFFF)
    except IndexError:
        raise DxaError("huffman: truncated header")
    body = bs.nbytes()
    if orig > (1 << 32):
        raise DxaError("huffman: implausible size")
    child, root = huffman_tree(weight)
    return _huff_walk(src[body:], orig, child, root)


def huffman_tree(weight):
    """-> (child pairs by node id, root id). Same pairing rule as DxLib so the tree is identical:
    scan in index order, first minimum wins, 0-branch = lowest weight."""
    w = list(weight) + [0] * 255
    child = [None] * 511
    parent = [-1] * 511
    alive = list(range(256))
    nxt = 256
    while len(alive) > 1:
        m1 = m2 = -1
        for i in alive:
            if m1 == -1 or w[m1] > w[i]:
                m2, m1 = m1, i
            elif m2 == -1 or w[m2] > w[i]:
                m2 = i
        w[nxt] = w[m1] + w[m2]
        child[nxt] = (m1, m2)
        parent[m1] = parent[m2] = nxt
        alive = [i for i in alive if i != m1 and i != m2] + [nxt]
        nxt += 1
    return child, nxt - 1


def _huff_walk(data, orig, child, root):
    # decode table on the first TB bits (LSB-first stream); longer codes fall back to a tree walk
    TB = 10
    table = [None] * (1 << TB)
    stack = [(root, 0, 0)]
    while stack:
        node, code, ln = stack.pop()
        if node < 256:
            if ln <= TB:
                for hi in range(1 << (TB - ln)):
                    table[code | (hi << ln)] = (node, ln)
            continue
        if ln >= TB:
            continue
        a, b = child[node]
        stack.append((a, code, ln + 1))
        stack.append((b, code | (1 << ln), ln + 1))
    out = bytearray()
    acc = nbits = pos = 0
    n = len(data)
    mask = (1 << TB) - 1
    while len(out) < orig:
        while nbits < 32 and pos < n:
            acc |= data[pos] << nbits
            nbits += 8
            pos += 1
        hit = table[acc & mask]
        if hit is not None:
            sym, ln = hit
            if ln > nbits:
                raise DxaError("huffman: ran out of data")
        else:
            node, ln = root, 0
            while node > 255:
                if ln >= nbits:
                    raise DxaError("huffman: ran out of data")
                node = child[node][(acc >> ln) & 1]
                ln += 1
            sym = node
        out.append(sym)
        acc >>= ln
        nbits -= ln
    return bytes(out)


# ---------------------------------------------------------------- archive

class _Archive:
    def __init__(self, path, key):
        self.path = path
        self.f = open(path, "rb")
        try:
            self._open(key)
        except Exception:
            self.f.close()
            raise

    def close(self):
        self.f.close()

    def _open(self, key):
        raw = self.f.read(HEAD_SIZE)
        if len(raw) < HEAD_SIZE or raw[:2] != MAGIC:
            raise DxaError("not a DXA archive")
        (ver, self.head_size, self.data_start, self.name_off, self.file_off, self.dir_off,
         self.codepage, self.flags, self.huff_kb) = struct.unpack_from("<HIQQQQIIB", raw, 2)
        if ver != 8:
            raise DxaError("DXA version %d not supported (only 8)" % ver)
        self.crypt_version = self.flags >> 16
        cv = self.crypt_version
        if cv >= UNSUPPORTED_CRYPT or cv in (0x64, 0xC8):
            raise DxaError("Wolf crypt version 0x%X (AES/ChaCha, key derived from Game.dat) is not supported" % cv)
        self.no_key = bool(self.flags & 1)
        self.keystring = b"" if key is None else bytes(key)
        self.key = None if self.no_key else make_key(self.keystring)
        self.f.seek(0, 2)
        size = self.f.tell()
        if not (HEAD_SIZE <= self.name_off < size) or self.data_start > self.name_off:
            raise DxaError("bad table offsets")
        self.f.seek(self.name_off)
        blob = self.f.read(size - self.name_off)
        blob = _xor(blob, self.key, 0)
        try:
            tbl = blob if self.flags & 2 else lz_decode(huffman_decode(blob))
        except (DxaError, IndexError, struct.error) as e:
            raise DxaError("table block did not decode (wrong key?): %s" % e)
        if len(tbl) != self.head_size or self.file_off >= len(tbl) or self.dir_off >= len(tbl):
            raise DxaError("table block size mismatch (wrong key?)")
        self.tbl = tbl
        self.entries = []  # (path, filehead tuple, key_dirs)
        try:
            self._walk(self.dir_off, "", [])
        except (struct.error, IndexError, ValueError, OverflowError, MemoryError) as e:
            raise DxaError("corrupt tables (wrong key?): %s" % e)

    def _name(self, addr):
        n4 = struct.unpack_from("<H", self.tbl, addr)[0] * 4
        upper = self.tbl[addr + 4:addr + 4 + n4].split(b"\0", 1)[0]
        orig = self.tbl[addr + 4 + n4:].split(b"\0", 1)[0]
        return orig, upper

    def _walk(self, dir_addr, prefix, dir_uppers):
        _, _, nfiles, fh_addr = struct.unpack_from("<QQQQ", self.tbl, dir_addr)
        for i in range(nfiles):
            at = self.file_off + fh_addr + i * FILEHEAD_SIZE
            (name_addr, attr, _c, _a, _w, data_addr, data_size, press, huff) = \
                struct.unpack_from("<QQQQQQQQQ", self.tbl, at)
            orig, upper = self._name(name_addr)
            name = orig.decode("cp932" if self.codepage == 932 else "utf-8", "replace")
            if attr & ATTR_DIR:
                self._walk(self.dir_off + data_addr, prefix + name + "/", [upper] + dir_uppers)
            else:
                self.entries.append((prefix + name, (data_addr, data_size, press, huff), upper, dir_uppers))

    def _file_key(self, upper, dir_uppers):
        if self.no_key:
            return None
        return make_key(self.keystring + upper + b"".join(dir_uppers))

    def _read(self, off, n, key, kpos):
        self.f.seek(self.data_start + off)
        b = self.f.read(n)
        if len(b) != n:
            raise DxaError("archive truncated")
        return _xor(b, key, kpos)

    def read_file(self, entry):
        _, (addr, size, press, huff), upper, dirs = entry
        if size == 0:
            return b""
        key = self._file_key(upper, dirs)
        total = size if press == NOT_SET else press
        if huff != NOT_SET:
            kb = self.huff_kb * 1024
            buf = huffman_decode(self._read(addr, huff, key, size))
            if self.huff_kb != 0xFF and total > kb * 2:
                mid = self._read(addr + huff, total - kb * 2, key, size + huff)
                buf = buf[:kb] + mid + buf[kb:]
        else:
            buf = self._read(addr, total, key, size)
        if press != NOT_SET:
            buf = lz_decode(buf)
        if len(buf) != size:
            raise DxaError("size mismatch for %s (wrong key?)" % entry[0])
        # Wolf prepends a notice to Game/Database/CommonEvent.dat; the reader must not see it
        if entry[0].lower().rsplit("/", 1)[-1] in ("game.dat", "cdatabase.dat", "database.dat", "commonevent.dat") \
                and buf.startswith(ANTI_UNPACK):
            buf = buf[len(ANTI_UNPACK):]
        return buf


def _open(path, key):
    return _Archive(path, key)


# ---------------------------------------------------------------- public API

def is_dxa(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == MAGIC
    except OSError:
        return False


def _candidates(path):
    with open(path, "rb") as f:
        h = f.read(HEAD_SIZE)
    flags = struct.unpack_from("<I", h, 0x2C)[0] if len(h) >= 0x30 else 0
    cv = flags >> 16
    out = []
    if flags & 1:
        return [b""]
    if cv in WOLF_KEYS:
        out.append(WOLF_KEYS[cv])
    out += [b""] + FALLBACK_KEYS + [k for v, k in WOLF_KEYS.items() if v != cv]
    return out


def detect_key(path):
    """Key string that decodes the archive's table block, or None if none of the known ones do.
    b"" means 'no password' (the zero key / NO_KEY archives); the version-matched Wolf string is
    tried first (see WOLF_KEYS). Raises DxaError for unsupported (AES/ChaCha) archives."""
    last = None
    for k in _candidates(path):
        try:
            _open(path, k).close()
            return k
        except DxaError as e:
            last = e
            if "not supported" in str(e) or "not a DXA" in str(e):
                raise
    return None


def _resolve(path, key):
    if key is None:
        key = detect_key(path)
        if key is None:
            raise DxaError("no known key decodes %s" % os.path.basename(path))
    return key


def list_entries(path, key=None):
    """-> list[Entry(path, size, packed, is_dir)] of every file (directories are implied by path)."""
    a = _open(path, _resolve(path, key))
    try:
        return [Entry(p, fh[1], fh[2] if fh[2] != NOT_SET else fh[1], False) for p, fh, _, _ in a.entries]
    finally:
        a.close()


def _select(only):
    if only is None:
        return lambda p: True
    pats = [o.lower() for o in ([only] if isinstance(only, str) else only)]
    return lambda p: any(fnmatch.fnmatch(p.lower(), q) for q in pats)


def unpack(archive_path, out_dir, key=None, only=None):
    """Extract into out_dir (entry paths as stored, forward slashes). `only`: glob or list of
    globs on the lower-cased entry path (e.g. "*.mps"). Returns the written relative paths."""
    a = _open(archive_path, _resolve(archive_path, key))
    want = _select(only)
    written = []
    out_abs = os.path.abspath(out_dir)
    try:
        for e in a.entries:
            rel = e[0]
            if not want(rel):
                continue
            parts = rel.split("/")
            if ".." in parts or ":" in rel or rel.startswith("/"):
                raise DxaError("unsafe path in archive: %r" % rel)
            dst = os.path.join(out_abs, *parts)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            data = a.read_file(e)
            tmp = dst + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            written.append(rel)
    finally:
        a.close()
    return written


def unpack_game_data(data_dir, out_dir, archives=None, key=None, exts=DEFAULT_EXTS):
    """Unpack what the Wolf reader needs from <game>/Data/*.wolf into out_dir (a Data folder
    mirror). archives=None -> BasicData/MapData/SystemFile plus any other archive whose table
    lists a .mps/.dat/.project (media archives are skipped, only their table is read);
    archives=[names] -> exactly those (stem or file name). Returns {archive: [rel paths]} and
    {archive: error} under key "_errors"."""
    result, errors = {}, {}
    names = sorted(f for f in os.listdir(data_dir) if f.lower().endswith(".wolf"))
    if archives is not None:
        norm = {os.path.splitext(a.lower())[0] for a in archives}
        names = [n for n in names if os.path.splitext(n.lower())[0] in norm]
    only = ["*" + e for e in exts] if exts else None
    for n in names:
        p = os.path.join(data_dir, n)
        stem = os.path.splitext(n)[0]
        try:
            k = _resolve(p, key)
            if archives is None and stem.lower() not in DEFAULT_ARCHIVES + ("data",):
                if not any(e.path.lower().endswith(tuple(exts)) for e in list_entries(p, k)):
                    continue
            # split archives hold their files at the root (Wolf expects Data/<stem>/...); a
            # monolithic Data.wolf already contains BasicData/, MapData/ ...
            sub = "" if stem.lower() == "data" else stem + "/"
            written = unpack(p, os.path.join(out_dir, sub), k, only)
            result[n] = [sub + w for w in written]
        except (DxaError, OSError) as e:
            errors[n] = str(e)
    result["_errors"] = errors
    return result
