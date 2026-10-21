#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reader contract. One Reader per engine family; readers know file formats only
-- they never call the LLM and never decide what is worth translating."""


class PatchError(Exception):
    """A line's locator no longer matches the file (game updated / re-extract needed)."""


class Reader:
    name = ""            # registry key, e.g. "rpgmmv"
    label = ""           # human label for the UI

    @classmethod
    def detect(cls, root):
        """True when `root` looks like a game this reader can handle."""
        raise NotImplementedError

    def extract(self, root):
        """Return list[project.Line] in play order (order=0..n-1). Never writes."""
        raise NotImplementedError

    def apply(self, root, out_root, lines):
        """Write patched copies of ONLY the files that have translated lines into
        `out_root` (same relative layout as `root`). Never modify `root`.
        Use line.translation when non-empty, else leave the source untouched.
        Raise PatchError if a locator does not match. Return list of relative
        paths written."""
        raise NotImplementedError
