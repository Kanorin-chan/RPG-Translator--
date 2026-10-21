"""Reader registry. Importing a reader module must not fail the whole package."""

import importlib

_MODULES = ("rpgmmv", "rpgmruby", "wolf")
READERS = {}

for _m in _MODULES:
    try:
        _mod = importlib.import_module("." + _m, __name__)
        READERS[_mod.READER.name] = _mod.READER
    except ImportError:
        pass  # reader not built yet


def detect(root):
    """Return the first registered reader class whose detect(root) is True, else None."""
    for r in READERS.values():
        if r.detect(root):
            return r
    return None
