# Contributing

Thanks for helping. The most useful contribution is a **compatibility report**:
run the tool on a real game and tell us what happened (use the *Game compatibility*
issue template).

## Ground rules
- Python 3.12, **standard library only** (plus optional `tkinterdnd2` for drag-and-drop).
- Run everything with `PYTHONUTF8=1`. CJK text breaks otherwise on Windows.
- Search before you write: `core.py` already has the filter, validator and prompt builders. Import them, do not copy them.
- Readers know file formats only. They never call the LLM and never decide what is worth translating.
- Comments are short and explain the non-obvious *why*.
- "It runs" is not done. Say what you verified and on which real game.

## Tests
```
PYTHONUTF8=1 python -m unittest discover -s tests -t .
```
No LLM or game files are needed. GUI tests skip when there is no display.

## Pull requests
Keep them small and say which engine/game you tried. Do not commit game files,
API keys, or translated game text.
