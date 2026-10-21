#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Repair an already-written translation output.

Why this exists
---------------
`translate.py` resumes by treating "value != key" as "already done". So when the
skip-filter is tightened (e.g. to stop translating internal identifiers like
EV107 -> "事件 018"), entries that were WRONGLY translated in an earlier run are
still considered finished and never revisited.

This pass walks an existing output and resets every entry that the CURRENT
skip-filter says should never have been sent to the model, putting its value
back to its key. After that, resuming leaves them alone permanently.

It never invents translations and never touches entries that are legitimately
translated.

Usage:
    PYTHONUTF8=1 python3 repair_output.py <output.json> [--dry-run] [--mode dynamic|strict_ja]

Writes a .bak beside the file before changing anything (unless --dry-run).
"""

import argparse
import os
import shutil
import sys

from rpgt import core


def main():
    ap = argparse.ArgumentParser(description="Reset wrongly-translated entries.")
    ap.add_argument("output", help="the .translated.json to repair (edited in place)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--mode", choices=["dynamic", "strict_ja"], default=None,
                    help="skip-filter mode (default: config.json 'mode')")
    args = ap.parse_args()

    if not os.path.exists(args.output):
        print("ERROR: no such file: %s" % args.output, file=sys.stderr)
        sys.exit(2)

    cfg = core.load_config()
    mode = core.get_mode(cfg, args.mode)
    data = core.load_json(args.output)

    # Must mirror translate.py's offline policy exactly, or we would reset
    # entries that path legitimately translated.
    req_cjk = bool(cfg.get("offline_require_cjk", True))

    # An entry is bogus if the current filter says "never send this" yet it
    # holds something other than its own key -> a translation we should not have.
    bogus = [k for k, v in data.items()
             if v != k and not core.should_translate(
                 k, mode, origin="offline", require_cjk=req_cjk)]

    print("Mode: %s | %d entries | %d wrongly translated" %
          (mode, len(data), len(bogus)))
    for k in bogus[:15]:
        print("   %-20s -> %-20s   (reset to %r)" % (k, data[k], k))
    if len(bogus) > 15:
        print("   ... and %d more" % (len(bogus) - 15))

    if not bogus:
        print("Nothing to repair.")
        return
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    bak = args.output + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(args.output, bak)
        print("\nBackup written: %s" % bak)
    else:
        print("\nBackup already exists, keeping it: %s" % bak)

    for k in bogus:
        data[k] = k
    core.atomic_write_json(args.output, data)
    print("Repaired %d entries. Resuming will now leave them alone." % len(bogus))


if __name__ == "__main__":
    main()
