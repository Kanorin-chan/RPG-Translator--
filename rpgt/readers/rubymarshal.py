#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure-Python Ruby Marshal 4.8 loader/dumper, lossless for unmodified data.

Loading keeps everything needed to re-emit identical bytes: raw string bytes,
ivar order, object identity (links) and the symbol table. The dumper re-derives
links/symlinks from object identity, exactly as Ruby does.
Format reference: Ruby's doc/marshal.rdoc.
"""

import struct


class Symbol(str):
    ivars = None  # dict when the symbol was marshalled inside an 'I' (encoding)

    def __repr__(self):
        return ":" + str.__str__(self)


class RFloat(float):
    raw = None  # original bytes; re-used while the value is unchanged

    def __new__(cls, value, raw=None):
        o = float.__new__(cls, value)
        o.raw = raw
        return o


class RBignum(int):
    """An int dumped as 'l'. Registered in the object table like Ruby's Bignum."""


class RString:
    def __init__(self, raw=b"", ivars=None):
        self.raw = raw
        self.ivars = ivars  # dict or None; E / encoding live here

    @property
    def encoding_name(self):
        """'utf-8', 'us-ascii', a Ruby encoding name, or None (binary / unmarked)."""
        iv = self.ivars or {}
        if "E" in iv:
            return "utf-8" if iv["E"] is True else "us-ascii"
        e = iv.get("encoding")
        if isinstance(e, RString):
            return e.raw.decode("ascii", "replace")
        return None

    def set_bytes(self, raw):
        self.raw = raw

    def mark_utf8(self):
        """Switch to UTF-8: drop any E/encoding ivar, append E true."""
        iv = {k: v for k, v in (self.ivars or {}).items() if k not in ("E", "encoding")}
        iv[Symbol("E")] = True
        self.ivars = iv

    def __repr__(self):
        return "RString(%r)" % (self.raw,)


class RArray(list):
    ivars = None


class RHash(list):
    """List of (key, value) pairs in file order; keys may be any object."""
    ivars = None
    default = None
    has_default = False

    def get(self, key, dflt=None):
        for k, v in self:
            if k == key and type(k) is not bool:
                return v
        return dflt

    def set(self, key, value):
        for i, (k, _) in enumerate(self):
            if k == key:
                self[i] = (k, value)
                return
        self.append((key, value))


class RObject:
    def __init__(self, cls, ivars=None):
        self.cls = cls
        self.ivars = ivars if ivars is not None else {}

    def __repr__(self):
        return "RObject(%s)" % self.cls


class RStruct:
    def __init__(self, cls, members=None):
        self.cls = cls
        self.members = members if members is not None else {}


class RUserDef:
    """'u': class-specific binary payload (RPG::Table, Color, Tone...), kept raw."""
    def __init__(self, cls, raw, ivars=None):
        self.cls = cls
        self.raw = raw
        self.ivars = ivars


class RUserMarshal:
    def __init__(self, cls, data):
        self.cls = cls
        self.data = data


class RClass:
    def __init__(self, name, tag="c"):
        self.name = name
        self.tag = tag  # 'c' class, 'm' module, 'M' old either


class RRegexp:
    def __init__(self, source, options, ivars=None):
        self.source = source
        self.options = options
        self.ivars = ivars


class RExtended:
    """'e' wrapper: object extended with modules."""
    def __init__(self, modules, obj):
        self.modules = modules
        self.obj = obj


class RUserClass:
    """'C' wrapper: instance of a user subclass of String/Array/Hash/Regexp."""
    def __init__(self, cls, obj):
        self.cls = cls
        self.obj = obj


# ---------------------------------------------------------------- loader

class _Loader:
    def __init__(self, data):
        self.d = data
        self.p = 0
        self.objs = []
        self.syms = []

    def byte(self):
        b = self.d[self.p]
        self.p += 1
        return b

    def long(self):
        c = self.byte()
        if c > 127:
            c -= 256
        if c == 0:
            return 0
        if c > 4:
            return c - 5
        if c < -4:
            return c + 5
        if c > 0:
            x = 0
            for i in range(c):
                x |= self.byte() << (8 * i)
            return x
        n = -c
        x = -1
        for i in range(n):
            x &= ~(0xFF << (8 * i))
            x |= self.byte() << (8 * i)
        return x

    def bytes_(self):
        n = self.long()
        b = self.d[self.p:self.p + n]
        if len(b) != n:
            raise ValueError("marshal: truncated data")
        self.p += n
        return b

    def reg(self, o):
        self.objs.append(o)
        return o

    def symbol(self):
        t = chr(self.byte())
        if t == "I":
            s = self.symbol()
            s.ivars = self.ivars()
            return s
        if t == ":":
            s = Symbol(self.bytes_().decode("utf-8", "surrogateescape"))
            self.syms.append(s)
            return s
        if t == ";":
            return self.syms[self.long()]
        raise ValueError("marshal: expected symbol, got %r at %d" % (t, self.p))

    def ivars(self):
        out = {}
        for _ in range(self.long()):
            k = self.symbol()
            out[k] = self.obj()
        return out

    def obj(self):
        t = chr(self.byte())
        if t == "0":
            return None
        if t == "T":
            return True
        if t == "F":
            return False
        if t == "i":
            return self.long()
        if t == ":":
            self.p -= 1
            return self.symbol()
        if t == ";":
            return self.syms[self.long()]
        if t == "@":
            return self.objs[self.long()]
        if t == "I":
            if self.d[self.p] == ord("u"):
                self.p += 1
                return self.userdef(True)
            o = self.obj()
            inner = o
            while isinstance(inner, (RExtended, RUserClass)):
                inner = inner.obj
            if isinstance(inner, (RObject, RStruct, RUserMarshal, RClass)) or \
                    inner is None or isinstance(inner, (bool, int)) and \
                    not isinstance(inner, RBignum):
                raise ValueError("marshal: ivars on unsupported %r" % type(inner))
            inner.ivars = self.ivars()
            return o
        if t == '"':
            return self.reg(RString(self.bytes_()))
        if t == "f":
            raw = self.bytes_()
            return self.reg(RFloat(_parse_float(raw), raw))
        if t == "l":
            sign = self.byte()
            n = self.long()
            v = int.from_bytes(self.d[self.p:self.p + 2 * n], "little")
            self.p += 2 * n
            return self.reg(RBignum(-v if sign == ord("-") else v))
        if t == "[":
            a = self.reg(RArray())
            for _ in range(self.long()):
                a.append(self.obj())
            return a
        if t in "{}":
            h = self.reg(RHash())
            for _ in range(self.long()):
                k = self.obj()
                h.append((k, self.obj()))
            if t == "}":
                h.has_default = True
                h.default = self.obj()
            return h
        if t == "o":
            cls = self.symbol()
            o = self.reg(RObject(cls))
            o.ivars = self.ivars()
            return o
        if t == "S":
            cls = self.symbol()
            s = self.reg(RStruct(cls))
            s.members = self.ivars()
            return s
        if t == "u":
            return self.userdef(False)
        if t == "U":
            cls = self.symbol()
            u = self.reg(RUserMarshal(cls, None))
            u.data = self.obj()
            return u
        if t in "cmM":
            return self.reg(RClass(self.bytes_().decode("utf-8", "surrogateescape"), t))
        if t == "/":
            src = self.bytes_()
            return self.reg(RRegexp(src, self.byte()))
        if t == "e":
            mods = [self.symbol()]
            inner = self.obj()
            if isinstance(inner, RExtended):
                return RExtended(mods + inner.modules, inner.obj)
            return RExtended(mods, inner)
        if t == "C":
            cls = self.symbol()
            return RUserClass(cls, self.obj())
        raise ValueError("marshal: unsupported type %r at %d" % (t, self.p - 1))

    def userdef(self, wrapped):
        cls = self.symbol()
        raw = self.bytes_()
        iv = self.ivars() if wrapped else None
        return self.reg(RUserDef(cls, raw, iv))


def _parse_float(raw):
    s = raw.split(b"\0")[0].decode("ascii")
    return {"inf": float("inf"), "-inf": float("-inf"), "nan": float("nan")}.get(s) \
        if s in ("inf", "-inf", "nan") else float(s)


def loads(data):
    if data[:2] != b"\x04\x08":
        raise ValueError("marshal: bad header %r" % (data[:2],))
    ld = _Loader(data)
    ld.p = 2
    o = ld.obj()
    if ld.p != len(data):
        raise ValueError("marshal: %d trailing bytes" % (len(data) - ld.p))
    return o


# ---------------------------------------------------------------- dumper

def _long(x):
    if x == 0:
        return b"\x00"
    if 0 < x < 123:
        return bytes([x + 5])
    if -124 < x < 0:
        return bytes([(x - 5) & 0xFF])
    buf = bytearray()
    for i in range(1, 5):
        buf.append(x & 0xFF)
        x >>= 8
        if x == 0:
            return bytes([i]) + bytes(buf)
        if x == -1:
            return bytes([(-i) & 0xFF]) + bytes(buf)
    raise ValueError("marshal: fixnum out of range")


def _float_bytes(f):
    if isinstance(f, RFloat) and f.raw is not None:
        try:
            if _parse_float(f.raw) == f or (f != f and f.raw == b"nan"):
                return f.raw
        except ValueError:
            pass
    if f != f:
        return b"nan"
    if f in (float("inf"), float("-inf")):
        return b"inf" if f > 0 else b"-inf"
    if f == int(f) and abs(f) < 1e16:
        return (b"-0" if str(f).startswith("-") and f == 0 else str(int(f)).encode())
    return repr(float(f)).encode()


class _Dumper:
    def __init__(self):
        self.out = bytearray(b"\x04\x08")
        self.objs = {}
        self.syms = {}
        self.keep = []  # keeps temp objects alive so id() stays unique

    def reg(self, o):
        self.objs[id(o)] = len(self.objs)
        self.keep.append(o)

    def bytes_(self, b):
        self.out += _long(len(b))
        self.out += b

    def symbol(self, s):
        if s in self.syms:
            self.out += b";" + _long(self.syms[s])
            return
        iv = getattr(s, "ivars", None)
        if iv is not None:
            self.out += b"I"
        self.out += b":"
        self.bytes_(s.encode("utf-8", "surrogateescape"))
        self.syms[s] = len(self.syms)
        if iv is not None:
            self.ivars(iv)

    def ivars(self, d):
        self.out += _long(len(d))
        for k, v in d.items():
            self.symbol(k)
            self.write(v)

    def write(self, o):
        out = self.out
        if o is None:
            out += b"0"
            return
        if o is True:
            out += b"T"
            return
        if o is False:
            out += b"F"
            return
        if type(o) is int and -(1 << 30) <= o < (1 << 30):
            out += b"i" + _long(o)
            return
        if isinstance(o, Symbol):
            self.symbol(o)
            return
        if isinstance(o, str):  # plain str -> UTF-8 string
            o = RString(o.encode("utf-8"), {Symbol("E"): True})
            self.keep.append(o)
        elif isinstance(o, bytes):
            o = RString(o)
            self.keep.append(o)
        elif isinstance(o, dict):
            h = RHash(o.items())
            self.keep.append(h)
            o = h
        # peel e / C wrappers; they are never linked, only the inner object is
        wraps = []
        inner = o
        while isinstance(inner, (RExtended, RUserClass)):
            wraps.append(inner)
            inner = inner.obj
        if id(inner) in self.objs:
            out += b"@" + _long(self.objs[id(inner)])
            return
        iv = None if isinstance(inner, (RObject, RStruct, RUserMarshal, RClass)) \
            else getattr(inner, "ivars", None)
        if isinstance(inner, RUserDef):
            if iv is not None:
                out += b"I"
            for w in wraps:
                self.wrap(w)
            out += b"u"
            self.symbol(inner.cls)
            self.bytes_(inner.raw)
            if iv is not None:
                self.ivars(iv)
            self.reg(inner)
            return
        if iv is not None:
            out += b"I"
        for w in wraps:
            self.wrap(w)
        self.body(inner)
        if iv is not None:
            self.ivars(iv)

    def wrap(self, w):
        if isinstance(w, RExtended):
            for m in w.modules:
                self.out += b"e"
                self.symbol(m)
        else:
            self.out += b"C"
            self.symbol(w.cls)

    def body(self, o):
        out = self.out
        if isinstance(o, RString):
            self.reg(o)
            out += b'"'
            self.bytes_(o.raw)
        elif isinstance(o, float):
            self.reg(o)
            out += b"f"
            self.bytes_(_float_bytes(o))
        elif isinstance(o, int):
            self.reg(o)
            out += b"l" + (b"-" if o < 0 else b"+")
            m = abs(o)
            n = (m.bit_length() + 15) // 16
            out += _long(n)
            out += m.to_bytes(2 * n, "little")
        elif isinstance(o, RArray) or type(o) is list:
            self.reg(o)
            out += b"[" + _long(len(o))
            for x in o:
                self.write(x)
        elif isinstance(o, RHash):
            self.reg(o)
            out += (b"}" if o.has_default else b"{") + _long(len(o))
            for k, v in o:
                self.write(k)
                self.write(v)
            if o.has_default:
                self.write(o.default)
        elif isinstance(o, RObject):
            self.reg(o)
            out += b"o"
            self.symbol(o.cls)
            self.ivars(o.ivars)
        elif isinstance(o, RStruct):
            self.reg(o)
            out += b"S"
            self.symbol(o.cls)
            self.ivars(o.members)
        elif isinstance(o, RUserMarshal):
            out += b"U"
            self.symbol(o.cls)
            self.reg(o)
            self.write(o.data)
        elif isinstance(o, RClass):
            self.reg(o)
            out += o.tag.encode()
            self.bytes_(o.name.encode("utf-8", "surrogateescape"))
        elif isinstance(o, RRegexp):
            self.reg(o)
            out += b"/"
            self.bytes_(o.source)
            out.append(o.options)
        else:
            raise TypeError("marshal: cannot dump %r" % type(o))


def dumps(obj):
    d = _Dumper()
    d.write(obj)
    return bytes(d.out)
