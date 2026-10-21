#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Engine-independent patcher: stage -> verify -> back up -> manifest -> atomic swap -> rollback."""

import hashlib
import json
import os
import shutil
import tempfile
import time

BACKUP_DIRNAME = "_rpgt_backups"


class PatchAborted(Exception):
    """Verification or swap failed; the game was left (or restored) as it was."""


class RollbackRefused(Exception):
    """A file changed since the patch; pass force=True to overwrite it anyway."""


def _log(on_log, msg):
    if on_log:
        on_log(msg)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _path(base, rel):
    parts = rel.split("/")
    if os.path.isabs(rel) or ".." in parts or ":" in rel:
        raise ValueError("unsafe relative path: %r" % rel)
    return os.path.join(base, *parts)


def _atomic_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".rpgt-tmp"  # same dir so os.replace is atomic
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _swap_one(src, dst):
    _atomic_copy(src, dst)  # separate name so tests can inject failure here only


def _missing_dirs(root, rels):
    """Parent directories of `rels` that do not exist yet (shallowest first), so rollback can
    remove the ones a new file caused to be created."""
    out = []
    for rel in rels:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in out and not os.path.isdir(_path(root, d)):
                out.append(d)
    return out


def _prune_dirs(root, rels):
    for d in sorted(rels, key=lambda r: r.count("/"), reverse=True):
        try:
            os.rmdir(_path(root, d))  # only succeeds when empty, so it never touches user data
        except OSError:
            pass


def _mkdtemp(root, prefix):
    parent = os.path.dirname(os.path.abspath(root))
    try:
        return tempfile.mkdtemp(prefix=prefix, dir=parent)
    except OSError:
        return tempfile.mkdtemp(prefix=prefix, dir=root)


def _shadow(root, stage, shadow, skip_abs):
    """Mirror root into shadow (hardlinks, copy fallback), then overlay staged files."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if os.path.abspath(os.path.join(dp, d)) not in skip_abs
                  and not d.startswith(".rpgt_")]
        rel = os.path.relpath(dp, root)
        tdir = shadow if rel == "." else os.path.join(shadow, rel)
        os.makedirs(tdir, exist_ok=True)
        for fn in fns:
            s, d = os.path.join(dp, fn), os.path.join(tdir, fn)
            try:
                os.link(s, d)
            except OSError:
                shutil.copy2(s, d)
    for dp, _, fns in os.walk(stage):
        for fn in fns:
            s = os.path.join(dp, fn)
            d = os.path.join(shadow, os.path.relpath(s, stage))
            if os.path.exists(d):
                os.remove(d)  # unlink the entry only; the hardlinked original is untouched
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d)


def _verify(root, reader, lines, stage, skip_abs, on_log):
    shadow = _mkdtemp(root, ".rpgt_verify_")
    try:
        _shadow(root, stage, shadow, skip_abs)
        old = reader.extract(root)
        new = reader.extract(shadow)
        # plugin-kind lines are checked by id: translated ones may stop matching the
        # reader's CJK filter and vanish from the re-extract, which is fine
        old_p = [l for l in old if l.kind != "plugin"]
        new_p = [l for l in new if l.kind != "plugin"]
        if len(old_p) != len(new_p):
            raise PatchAborted("verify: %d lines before, %d after patch" % (len(old_p), len(new_p)))
        pos = {l.id: i for i, l in enumerate(old_p)}
        new_id = {l.id: l for l in new if l.kind == "plugin"}
        bad = []
        for l in lines:
            if not l.translation:
                continue
            if l.kind == "plugin":
                got = new_id.get(l.id)
                if got is not None and got.source != l.translation:
                    bad.append(l.id)
                continue
            i = pos.get(l.id)
            # ids after a re-split shift, so compare by play-order position
            if i is None or new_p[i].file != l.file or new_p[i].source != l.translation:
                bad.append(l.id)
        if bad:
            raise PatchAborted("verify: %d lines did not round-trip, e.g. %s" % (len(bad), bad[:3]))
        _log(on_log, "verify ok (%d lines re-read)" % len(new))
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def patch_game(root, reader, project_lines, backup_dir=None, on_log=None):
    if isinstance(reader, type):
        reader = reader()
    root = os.path.abspath(root)
    backup_dir = os.path.abspath(backup_dir or os.path.join(root, BACKUP_DIRNAME))
    skip = {backup_dir}
    stage = _mkdtemp(root, ".rpgt_stage_")
    try:
        written = reader.apply(root, stage, project_lines)
        _log(on_log, "staged %d file(s)" % len(written))
        if not written:
            return {"version": 1, "reader": reader.name, "files": [], "status": "nothing-to-do"}
        _verify(root, reader, project_lines, stage, skip, on_log)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        bdir, n = os.path.join(backup_dir, stamp), 1
        while os.path.exists(bdir):
            n += 1
            bdir = os.path.join(backup_dir, "%s-%d" % (stamp, n))
        files = []
        for rel in sorted(written):
            if rel == "manifest.json":
                raise PatchAborted("cannot back up a root-level manifest.json")
            dst = _path(root, rel)
            files.append({"path": rel,
                          "before": sha256(dst) if os.path.isfile(dst) else None,
                          "after": sha256(_path(stage, rel))})
        for f in files:
            if f["before"] is not None:
                _atomic_copy(_path(root, f["path"]), _path(bdir, f["path"]))
        # before=None marks a file the patch CREATES (no original to back up; rollback deletes it)
        manifest = {"version": 1, "reader": reader.name, "created": stamp,
                    "root": root, "backup_path": bdir, "status": "pending", "files": files,
                    "new_dirs": _missing_dirs(root, [f["path"] for f in files if f["before"] is None])}
        _write_manifest(bdir, manifest)
        _log(on_log, "backed up %d file(s) to %s" % (len(files), bdir))

        done = []
        try:
            for f in files:
                dst = _path(root, f["path"])
                _swap_one(_path(stage, f["path"]), dst)
                done.append(f)
                if sha256(dst) != f["after"]:
                    raise OSError("hash mismatch after write: %s" % f["path"])
        except Exception as e:
            for f in done + [x for x in files if x not in done][:1]:
                dst = _path(root, f["path"])
                try:
                    if f["before"] is None:
                        if os.path.exists(dst):
                            os.remove(dst)
                    else:
                        _atomic_copy(_path(bdir, f["path"]), dst)
                except OSError as e2:
                    _log(on_log, "RESTORE FAILED %s: %s" % (f["path"], e2))
            _prune_dirs(root, manifest["new_dirs"])
            manifest["status"] = "failed-restored"
            _write_manifest(bdir, manifest)
            raise PatchAborted("swap failed, originals restored: %s" % e) from e
        manifest["status"] = "applied"
        _write_manifest(bdir, manifest)
        _log(on_log, "patched %d file(s)" % len(files))
        return manifest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _write_manifest(bdir, manifest):
    os.makedirs(bdir, exist_ok=True)
    p = os.path.join(bdir, "manifest.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(p + ".tmp", p)


def _load_manifest(m):
    if isinstance(m, dict):
        return m
    p = m if os.path.isfile(m) else os.path.join(m, "manifest.json")
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    d["backup_path"] = os.path.dirname(os.path.abspath(p))  # trust location over stored path
    return d


def rollback(root, manifest_or_path, on_log=None, force=False):
    m = _load_manifest(manifest_or_path)
    if m.get("status") != "applied":
        raise RollbackRefused("backup status is %r, nothing to roll back" % m.get("status"))
    root, bdir = os.path.abspath(root), m["backup_path"]
    todo, changed = [], []
    for f in m["files"]:
        dst = _path(root, f["path"])
        cur = sha256(dst) if os.path.isfile(dst) else None
        if cur == f["before"]:
            continue  # already original
        if cur != f["after"]:
            changed.append(f["path"])
        todo.append(f)
    if changed and not force:
        raise RollbackRefused("changed since patch: %s (use force=True)" % ", ".join(changed))
    for f in todo:  # check backups exist before touching anything
        if f["before"] is not None and not os.path.isfile(_path(bdir, f["path"])):
            raise RollbackRefused("backup file missing: %s" % f["path"])
    for f in todo:
        dst = _path(root, f["path"])
        if f["before"] is None:
            if os.path.exists(dst):
                os.remove(dst)
        else:
            _atomic_copy(_path(bdir, f["path"]), dst)
            if sha256(dst) != f["before"]:
                raise OSError("restored file hash mismatch: %s" % f["path"])
        _log(on_log, "restored %s" % f["path"] if f["before"] is not None else "removed %s" % f["path"])
    _prune_dirs(root, m.get("new_dirs", []))
    return [f["path"] for f in todo]


def list_backups(root, backup_dir=None):
    bd = os.path.abspath(backup_dir or os.path.join(root, BACKUP_DIRNAME))
    out = []
    if os.path.isdir(bd):
        for name in os.listdir(bd):
            p = os.path.join(bd, name, "manifest.json")
            if os.path.isfile(p):
                try:
                    d = _load_manifest(p)
                except (OSError, ValueError):
                    continue
                d["manifest_path"] = p
                out.append(d)
    return sorted(out, key=lambda d: os.path.basename(d["backup_path"]), reverse=True)
