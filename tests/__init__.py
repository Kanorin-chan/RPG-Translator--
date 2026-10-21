"""Tests. Run from the project root:  python -m unittest discover -s tests -t .
Puts the project root (the modules under test) and this folder (shared helpers such as
test_wolf.make_game) on sys.path."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
