#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Point a game's XUnity.AutoTranslator at our local CustomTranslate server.

Usage:
    python3 configure_game.py "<path to game folder OR its Config.ini>"
    python3 configure_game.py "<...>" --lang zh-Hant
    python3 configure_game.py "<...>" --revert

What it changes (in place, section-aware, everything else preserved):
    [Service] Endpoint       = CustomTranslate
    [Custom]  Url            = http://127.0.0.1:<xunity_port>/translate
    [Custom]  DisableSpamChecks = True
    [Custom]  EnableShortDelay  = True
    [General] Language        = zh-Hant   (--lang; FromLanguage left as-is)

The original file is backed up to Config.ini.bak (never clobbering an existing
.bak). --revert restores from that backup.

This is a careful line/section-aware editor — it does NOT rewrite the file from
a template, so the many game-specific keys, comments and ordering survive.
Standard library only. Run with PYTHONUTF8=1 on Windows.
"""

import argparse
import os
import shutil
import sys

import core

# Config filenames XUnity uses across versions.
_CONFIG_NAMES = ("Config.ini", "AutoTranslatorConfig.ini")
# Known relative locations, checked in order. The BepInEx plugin actually loads
# BepInEx\config\AutoTranslatorConfig.ini, so it is checked FIRST — the other two
# are legacy/standalone paths the BepInEx plugin ignores, kept only as fallbacks
# for hand-made installs.
_KNOWN_RELPATHS = (
    os.path.join("BepInEx", "config", "AutoTranslatorConfig.ini"),
    os.path.join("BepInEx", "AutoTranslator", "Config.ini"),
    os.path.join("AutoTranslator", "Config.ini"),
)


# --------------------------------------------------------------------------- #
# locating the config
# --------------------------------------------------------------------------- #
def find_config(path):
    p = os.path.abspath(path)
    if os.path.isfile(p):
        return p
    if not os.path.isdir(p):
        return None
    for rel in _KNOWN_RELPATHS:
        cand = os.path.join(p, rel)
        if os.path.isfile(cand):
            return cand
    # bounded walk: look for the config names anywhere under a BepInEx tree
    for root, _dirs, files in os.walk(p):
        for name in _CONFIG_NAMES:
            if name in files and "BepInEx" in root:
                return os.path.join(root, name)
    # last resort: any matching name anywhere under path
    for root, _dirs, files in os.walk(p):
        for name in _CONFIG_NAMES:
            if name in files:
                return os.path.join(root, name)
    return None


# --------------------------------------------------------------------------- #
# newline / encoding aware line handling
# --------------------------------------------------------------------------- #
def _read_lines(path):
    """Return (lines_with_endings, bom, default_newline).

    lines keep their own trailing newline so untouched lines are byte-preserved.
    """
    with open(path, "rb") as f:
        raw = f.read()
    bom = b""
    if raw.startswith(b"\xef\xbb\xbf"):
        bom = raw[:3]
        raw = raw[3:]
    text = raw.decode("utf-8")
    default_nl = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(keepends=True), bom, default_nl


def _write_lines(path, lines, bom):
    with open(path, "wb") as f:
        if bom:
            f.write(bom)
        f.write("".join(lines).encode("utf-8"))


def _split_ending(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


# --------------------------------------------------------------------------- #
# the editor
# --------------------------------------------------------------------------- #
def apply_changes(lines, default_nl, desired):
    """Apply {section: {key: value}} to the ini lines, section-aware.

    Returns (new_lines, changes) where changes is a list of
    (section, key, old_value_or_None, new_value)."""
    # normalize desired keys for case-insensitive matching
    want = {sec.lower(): {k.lower(): (k, v) for k, v in kv.items()}
            for sec, kv in desired.items()}
    remaining = {sec: dict(kv) for sec, kv in want.items()}  # track unset keys

    changes = []
    out = []
    cur_section = None
    # remember index of the last content line of each seen section, to allow
    # inserting a missing key into an existing section.
    section_last_idx = {}

    for line in lines:
        core_text, end = _split_ending(line)
        stripped = core_text.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            cur_section = stripped[1:-1].strip().lower()
            out.append(line)
            section_last_idx[cur_section] = len(out) - 1
            continue

        # a key=value line inside a section we care about?
        if cur_section in want and "=" in core_text and not stripped.startswith(";") \
                and not stripped.startswith("#"):
            key_part, _sep, val_part = core_text.partition("=")
            key_norm = key_part.strip().lower()
            if key_norm in remaining.get(cur_section, {}):
                orig_key, new_val = remaining[cur_section].pop(key_norm)
                old_val = val_part
                if val_part != new_val:
                    changes.append((cur_section, key_part.strip(), old_val, new_val))
                # rebuild preserving the original key spelling + spacing style
                new_core = "%s=%s" % (key_part, new_val) if _sep else core_text
                # keep exactly the left side of '=' as it was
                out.append(new_core + (end or default_nl))
            else:
                out.append(line)
            if cur_section is not None:
                section_last_idx[cur_section] = len(out) - 1
            continue

        out.append(line)
        if cur_section is not None:
            section_last_idx[cur_section] = len(out) - 1

    # any keys not found in their (existing) section -> append into that section;
    # sections not present at all -> append a fresh section at EOF.
    for sec, kv in remaining.items():
        if not kv:
            continue
        if sec in section_last_idx:
            insert_at = section_last_idx[sec] + 1
            new_block = []
            for _knorm, (orig_key, new_val) in kv.items():
                new_block.append("%s=%s%s" % (orig_key, new_val, default_nl))
                changes.append((sec, orig_key, None, new_val))
            out[insert_at:insert_at] = new_block
            # shift later recorded indices (only matters if multiple missing
            # sections share ordering; recompute lazily by re-scanning next loop)
            for s2 in section_last_idx:
                if section_last_idx[s2] >= insert_at:
                    section_last_idx[s2] += len(new_block)
        else:
            # ensure previous content ends with a newline
            if out and not out[-1].endswith(("\n", "\r")):
                c, _e = _split_ending(out[-1])
                out[-1] = c + default_nl
            # section header spelled from the desired dict (Title-case original)
            header = None
            for orig_sec in desired:
                if orig_sec.lower() == sec:
                    header = orig_sec
                    break
            out.append("[%s]%s" % (header or sec, default_nl))
            for _knorm, (orig_key, new_val) in kv.items():
                out.append("%s=%s%s" % (orig_key, new_val, default_nl))
                changes.append((sec, orig_key, None, new_val))

    return out, changes


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def do_configure(cfg_path, lang):
    cfg = core.load_config()
    port = int(cfg.get("xunity_port", 4404))
    host = cfg.get("xunity_host", "127.0.0.1")
    url = "http://%s:%d/translate" % (host, port)

    desired = {
        "Service": {"Endpoint": "CustomTranslate"},
        "Custom": {
            "Url": url,
            "DisableSpamChecks": "True",
            "EnableShortDelay": "True",
        },
        "General": {"Language": lang},
    }

    lines, bom, default_nl = _read_lines(cfg_path)
    new_lines, changes = apply_changes(lines, default_nl, desired)

    # backup (never clobber an existing .bak)
    bak = cfg_path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(cfg_path, bak)
        bak_note = "backup written: %s" % bak
    else:
        bak_note = "backup already exists (kept): %s" % bak

    _write_lines(cfg_path, new_lines, bom)

    print("Config: %s" % cfg_path)
    print(bak_note)
    print("")
    if not changes:
        print("No changes needed — already pointed at CustomTranslate.")
    else:
        print("Changed keys (before -> after):")
        for sec, key, old, new in changes:
            if old is None:
                print("  [%s] %s   (added) -> %s" % (sec, key, new))
            else:
                print("  [%s] %s : %s -> %s"
                      % (sec, key, old if old != "" else "<empty>", new))
    print("")
    print("Next steps:")
    print("  1. Start the server:   PYTHONUTF8=1 python3 server.py")
    print("     (leave it running while you play)")
    print("  2. (Re)launch the game. XUnity will now translate via %s" % url)
    print("  To undo:  python3 configure_game.py \"%s\" --revert" % cfg_path)


def do_revert(cfg_path):
    bak = cfg_path + ".bak"
    if not os.path.exists(bak):
        print("No backup found at %s — nothing to revert." % bak, file=sys.stderr)
        sys.exit(1)
    shutil.copy2(bak, cfg_path)
    print("Reverted %s from %s" % (cfg_path, bak))


def main():
    ap = argparse.ArgumentParser(
        description="Point a game's XUnity.AutoTranslator at the local server."
    )
    ap.add_argument("path", help="game folder OR its Config.ini")
    ap.add_argument("--lang", default="zh-Hant",
                    help="target [General] Language (default: zh-Hant)")
    ap.add_argument("--revert", action="store_true",
                    help="restore Config.ini from its .bak")
    args = ap.parse_args()

    cfg_path = find_config(args.path)
    if not cfg_path:
        print("Could not find a XUnity Config.ini under: %s" % args.path,
              file=sys.stderr)
        print("Looked for BepInEx\\AutoTranslator\\Config.ini and "
              "BepInEx\\config\\AutoTranslatorConfig.ini.", file=sys.stderr)
        sys.exit(1)

    if args.revert:
        do_revert(cfg_path)
    else:
        do_configure(cfg_path, args.lang)


if __name__ == "__main__":
    main()
