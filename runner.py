#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Project translator: runs core's translation primitives over a project.Project.

Unit of work is a SCENE, so context lives inside a scene and scenes can run on a
thread pool without sharing prompts. Standard library only (PYTHONUTF8=1).
"""

import concurrent.futures
import threading
import time

import core
import project as project_mod


# --------------------------------------------------------------------------- #
# fallback chain
# --------------------------------------------------------------------------- #
def _fallback_ex(fn, cfgs):
    """Run fn(cfg) over cfgs in order; RuntimeError moves on. Returns (result, cfg)."""
    last = None
    for cfg in cfgs:
        try:
            return fn(cfg), cfg
        except RuntimeError as e:
            last = e
    raise last


def _call_with_fallback(fn, cfgs):
    """fn(cfg) with cfgs[0], then each fallback on RuntimeError; raises the last error."""
    return _fallback_ex(fn, cfgs)[0]


# --------------------------------------------------------------------------- #
# per-line work (no shared state touched here)
# --------------------------------------------------------------------------- #
def _finish(cfg, glossary, mode, src, tr, ctx):
    """Validate one translation. Returns (status, text, note)."""
    if not tr.strip():
        return "kept", src, "empty_output"
    reason = core.rejection_reason(cfg, src, tr)
    fixed, ok = core.validate_or_recover(cfg, glossary, src, tr, mode, ctx)
    if ok:
        return "ok", fixed, ""
    return "kept", src, (reason or "retry_error") + "_after_retry"


def _solo(cfgs, glossary, mode, src, ctx, temp):
    try:
        tr, used = _fallback_ex(
            lambda c: core.translate_solo(c, glossary, src, temp, mode, ctx), cfgs)
    except Exception as e:
        core.log_failure(src, "<solo error: %r>" % e, "solo_exception")
        return "failed", src, "llm_error: %r" % (e,)
    return _finish(used, glossary, mode, src, tr, ctx)


def _translate_batch(cfgs, glossary, mode, lines, ctx, temp, mtpe):
    """One batch -> [(status, text, note)] aligned with lines. Never raises."""
    sources = [l.source for l in lines]
    trs, used = None, None
    try:
        msg = core.build_batch_user_msg(sources, glossary, mode, ctx)
        raw, used = _fallback_ex(
            lambda c: core.call_llm(c, msg, temp, mtpe * len(sources), mode), cfgs)
        trs = core.parse_markers(raw, len(sources))
    except Exception:
        trs = None  # marker miss or LLM error -> per-line solo; never zip mismatched lists
    out = []
    for i, src in enumerate(sources):
        if trs is not None and trs[i].strip():
            out.append(_finish(used, glossary, mode, src, trs[i], ctx))
        else:
            out.append(_solo(cfgs, glossary, mode, src, ctx, temp))
    return out


def _apply(line, res):
    status, text, note = res
    if status == "ok":
        line.translation, line.status, line.flagged, line.note = text, "ok", False, ""
    elif status == "kept":
        line.translation, line.status, line.flagged, line.note = text, "kept", True, note
    else:  # failed: leave status alone so a resume retries it
        line.note = note


def _ctx_arg(pairs):
    return pairs or None


# --------------------------------------------------------------------------- #
# speaker names: translated once per distinct name, stored in project.meta["names"]
# --------------------------------------------------------------------------- #
_NAME_INSTR = ("This is a character name from a Japanese game. Output ONLY its %s "
               "rendering (translate or transliterate), keeping any backslash codes "
               "exactly as they are:\n\n")


def _name_ok(raw, tr):
    return bool(tr) and "\n" not in tr and ">" not in tr and len(tr) <= 60


def _translate_name(cfgs, glossary, mode, raw, temp):
    """Translation of one speaker name, or None when the LLM failed/was rejected."""
    if glossary.get(raw):
        return glossary[raw]
    if not core.CJK_RE.search(core.CODE_RE.sub("", raw)):
        return raw  # only codes / no Japanese text: nothing to translate
    msg = (core.glossary_block(glossary)
           + _NAME_INSTR % cfgs[0].get("target_language", "Traditional Chinese") + raw)
    for t in (temp, float(cfgs[0].get("retry_temperature", 0.2))):
        try:
            out, used = _fallback_ex(
                lambda c: core.call_llm(c, msg, t, 200, mode), cfgs)
        except Exception as e:
            core.log_failure(raw, "<name error: %r>" % (e,), "name_exception")
            return None
        out = out.strip("\n").strip()
        if _name_ok(raw, out) and core.rejection_reason(used, raw, out) is None:
            return out
        core.log_failure(raw, out, "name_rejected")
    return None


def _ensure_names(project, lines, cfgs, glossary, mode, temp, stopping=lambda: False):
    """Fill project.meta["names"] for the speakers of `lines`, then copy the map onto
    every tagged line's locator["speaker_tr"] (read by the reader's apply())."""
    names = project.meta.setdefault("names", {})
    lines = [l for l in lines if l.speaker and l.locator.get("tag")]  # tagged speakers only
    for raw in sorted({l.speaker for l in lines if l.speaker not in names}):
        if stopping():
            break
        tr = _translate_name(cfgs, glossary, mode, raw, temp)
        if tr is not None:
            names[raw] = tr
    for l in project.lines:  # every tagged line, so already-ok lines pick up edited names too
        if l.locator.get("tag") and l.speaker in names:
            l.locator["speaker_tr"] = names[l.speaker]


# --------------------------------------------------------------------------- #
# run_project
# --------------------------------------------------------------------------- #
def run_project(project, cfgs, glossary, *, project_path=None, concurrency=1,
                batch_size=None, context_k=None, only_ids=None, force=False,
                on_progress=None, on_line=None, stop_flag=None):
    if not cfgs:
        raise ValueError("cfgs must hold at least one cfg")
    cfg0 = cfgs[0]
    mode = core.get_mode(cfg0)
    bsize = max(1, int(batch_size or cfg0.get("batch_size", 40)))
    if context_k is None:
        context_k = int(cfg0.get("context_window", 4))
    if not cfg0.get("context_enabled", True):
        context_k = 0
    mtpe = int(cfg0.get("max_tokens_per_entry", 60))
    temp = float(cfg0.get("temperature", 0.3))
    save_every = max(1, int(cfg0.get("save_every_batches", 5)))
    concurrency = max(1, int(concurrency or 1))
    only = set(only_ids) if only_ids is not None else None
    t0 = time.time()

    counts = {"ok": 0, "kept": 0, "skipped": 0, "failed": 0, "protected": 0}
    state = {"batches_done": 0, "scenes_done": 0, "stopped": False}
    lock = threading.Lock()

    # Plan up front (single thread) so progress has a known total.
    plan = []  # (scene_id, scene_lines, [batch, ...])
    total_batches = 0
    scenes_total = 0
    for sid, slines in project.scenes():
        scenes_total += 1
        todo = []
        for l in slines:
            if only is not None and l.id not in only:
                continue
            if l.status == "edited" and not force:
                counts["protected"] += 1
                continue
            if l.status == "ok" and not force:
                counts["protected"] += 1
                continue
            if not core.should_translate(l.source, mode, origin="extracted", kind=l.kind):
                l.status = "skipped"
                counts["skipped"] += 1
                continue
            todo.append(l)
        batches = [todo[i:i + bsize] for i in range(0, len(todo), bsize)]
        total_batches += len(batches)
        plan.append((sid, slines, batches))

    def stopping():
        return stop_flag is not None and stop_flag.is_set()

    def save():
        if project_path:
            project.save(project_path)

    def do_scene(item):
        sid, slines, batches = item
        # Scene-local view: context never crosses a scene or reads another worker's lines.
        local = project_mod.Project(project.root, project.engine, slines)
        for batch in batches:
            if stopping():
                with lock:
                    state["stopped"] = True
                return
            ctx = _ctx_arg(local.context_before(batch[0], context_k)) if context_k > 0 else None
            try:
                results = _translate_batch(cfgs, glossary, mode, batch, ctx, temp, mtpe)
            except Exception as e:  # last-resort guard: a scene must never kill the run
                core.log_failure("<scene %s>" % sid, "<batch error: %r>" % e, "batch_exception")
                results = [("failed", l.source, "batch_error: %r" % (e,)) for l in batch]
            with lock:
                for l, r in zip(batch, results):
                    _apply(l, r)
                    counts[r[0]] += 1
                state["batches_done"] += 1
                if state["batches_done"] % save_every == 0:
                    save()
                if on_progress is not None:
                    on_progress(state["batches_done"], total_batches,
                                dict(counts, scenes_done=state["scenes_done"]))
            if on_line is not None:
                for l in batch:
                    on_line(l)
        with lock:
            state["scenes_done"] += 1

    def guarded(item):
        try:
            do_scene(item)
        except Exception as e:
            core.log_failure("<scene %s>" % item[0], "<crash: %r>" % e, "scene_crash")

    # Names first, single-threaded: one call per distinct name, shared by every scene.
    _ensure_names(project, [l for _, _, bs in plan for b in bs for l in b],
                  cfgs, glossary, mode, temp, stopping)

    if concurrency == 1:
        for item in plan:
            guarded(item)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(guarded, plan))

    save()
    return {
        "ok": counts["ok"], "kept": counts["kept"], "skipped": counts["skipped"],
        "failed": counts["failed"], "protected": counts["protected"],
        "scenes_done": state["scenes_done"], "scenes_total": scenes_total,
        "batches_done": state["batches_done"], "batches_total": total_batches,
        "stopped": state["stopped"], "elapsed": time.time() - t0,
    }


# --------------------------------------------------------------------------- #
# retranslate_lines
# --------------------------------------------------------------------------- #
def retranslate_lines(project, line_ids, cfgs, glossary, *, context_k=None,
                      project_path=None, on_line=None, stop_flag=None):
    """Translate each selected line ALONE, with the lines before it in play order as
    reference. Runs in play order, so a selected line's context already holds the
    fresh translation of a selected neighbour -- but no prompt ever holds two targets."""
    if not cfgs:
        raise ValueError("cfgs must hold at least one cfg")
    cfg0 = cfgs[0]
    mode = core.get_mode(cfg0)
    if context_k is None:
        context_k = int(cfg0.get("context_window", 4))
    if not cfg0.get("context_enabled", True):
        context_k = 0
    temp = float(cfg0.get("temperature", 0.3))
    t0 = time.time()
    counts = {"ok": 0, "kept": 0, "skipped": 0, "failed": 0, "missing": 0}
    stopped = False

    by_id = project.by_id()
    picked = []
    for i in line_ids:
        if i in by_id:
            picked.append(by_id[i])
        else:
            counts["missing"] += 1
    picked.sort(key=lambda l: l.order)

    _ensure_names(project, picked, cfgs, glossary, mode, temp)
    for l in picked:
        if stop_flag is not None and stop_flag.is_set():
            stopped = True
            break
        if not core.should_translate(l.source, mode, origin="extracted", kind=l.kind):
            l.status, l.flagged, l.note = "skipped", False, ""
            counts["skipped"] += 1
        else:
            ctx = _ctx_arg(project.context_before(l, context_k)) if context_k > 0 else None
            res = _solo(cfgs, glossary, mode, l.source, ctx, temp)
            _apply(l, res)
            counts[res[0]] += 1
        if on_line is not None:
            on_line(l)

    if project_path:
        project.save(project_path)
    counts.update(stopped=stopped, elapsed=time.time() - t0)
    return counts
