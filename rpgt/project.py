#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Project file: the scene-ordered lines of ONE game plus their translation state.

This is the contract between readers (extract/patch), the runner (translate),
the patcher (backup/rollback) and the UI (review list). Standard library only.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict

# Line.kind values. Readers set it; should_translate(origin="extracted") uses it.
KINDS = ("dialogue", "choice", "name", "desc", "term", "plugin", "script", "other")

# Line.status values.
#   new       not translated yet
#   ok        translated and passed validate_or_recover
#   kept      translation failed twice; source kept (needs review)
#   skipped   filter said never send it
#   edited    human-edited; runner must not overwrite unless forced
STATUSES = ("new", "ok", "kept", "skipped", "edited")


@dataclass
class Line:
    id: str                    # stable across re-extract: "<file>#<reader-path>"
    file: str                  # path relative to the game root, forward slashes
    scene: str                 # scene/group id; lines of one scene are contiguous
    order: int                 # global play order (0..n-1)
    kind: str                  # one of KINDS
    source: str
    speaker: str = ""
    translation: str = ""
    status: str = "new"        # one of STATUSES
    flagged: bool = False      # auto (failed check) or manual
    note: str = ""             # why it was flagged/kept (e.g. "language_drift")
    locator: dict = field(default_factory=dict)  # reader-private patch-back info


@dataclass
class Project:
    root: str                  # game root directory
    engine: str                # reader name, e.g. "rpgmmv"
    lines: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # target_language, provider, timestamps…
    version: int = 1

    def by_id(self):
        return {l.id: l for l in self.lines}

    def scenes(self):
        """Yield (scene_id, [Line…]) in play order."""
        cur, buf = None, []
        for l in sorted(self.lines, key=lambda x: x.order):
            if l.scene != cur and buf:
                yield cur, buf
                buf = []
            cur = l.scene
            buf.append(l)
        if buf:
            yield cur, buf

    def context_before(self, line, k):
        """The k lines before `line` in play order, as (source, translation) pairs
        (oldest first) -- the shape core.context_block() expects. Untranslated
        neighbours contribute their source on both sides."""
        ordered = sorted(self.lines, key=lambda x: x.order)
        i = next((n for n, l in enumerate(ordered) if l.id == line.id), 0)
        out = []
        for l in ordered[max(0, i - k):i]:
            if l.kind in ("dialogue", "choice"):
                out.append((l.source, l.translation or l.source))
        return out

    def save(self, path):
        self.meta["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": self.version, "root": self.root, "engine": self.engine,
                 "meta": self.meta, "lines": [asdict(l) for l in self.lines]},
                f, ensure_ascii=False)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return cls(root=d["root"], engine=d["engine"], meta=d.get("meta", {}),
                   version=d.get("version", 1),
                   lines=[Line(**l) for l in d["lines"]])
