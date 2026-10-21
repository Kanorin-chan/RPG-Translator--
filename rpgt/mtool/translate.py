#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart batch translator for eroge (Japanese adult VN) game text.

Reads an MTool "Export Game Text" JSON ({ "日本語キー": "value" }), translates
into the target language with a local OpenAI-compatible LLM, and writes the JSON
back so it can be re-imported into MTool.

Standard library only. All translation logic lives in core.py (shared with the
live XUnity server server.py). This file is the batch CLI PLUS a reusable
run_translation() the control UI (ui.py) drives on a worker thread.

Run with PYTHONUTF8=1 so CJK does not mojibake on Windows:
    PYTHONUTF8=1 python3 translate.py <input.json> [output.json] [--limit N]
                                       [--mode dynamic|strict_ja]

Modes (default comes from config.json "mode", normally "dynamic"):
    dynamic    normalize JA / EN / mixed / already-translated text to target
    strict_ja  original v1 behavior: assume Japanese source, skip non-CJK lines
"""

import argparse
import collections
import concurrent.futures
import os
import sys
import threading
import time

from rpgt import core


# --------------------------------------------------------------------------- #
# planning (shared by the CLI header and run_translation so they never diverge)
# --------------------------------------------------------------------------- #
def plan_translation(in_path, out_path, cfg, mode):
    """Load the source + any resume checkpoint and compute what to do.

    Returns (src, out, todo, total_translatable):
      src   -> the input dict
      out   -> the working output dict (resumed from out_path if it exists)
      todo  -> list of keys still needing translation (respects cfg['limit'])
      total_translatable -> count of translatable keys in the whole file

    An optional cfg['limit'] (int > 0) caps todo to the first N entries; the CLI
    sets it from --limit. Absent/0 = translate everything.
    """
    src = core.load_json(in_path)

    # Resume: start from existing output if present, else a fresh copy of input.
    if os.path.exists(out_path):
        out = core.load_json(out_path)
        for k, v in src.items():
            out.setdefault(k, v)  # schema-drift safety
    else:
        out = dict(src)

    # Offline policy: reject engine data (code/shaders/notetags/asset names).
    # repair_output.py MUST use the identical policy or it would reset entries
    # this path legitimately translated.
    req_cjk = bool(cfg.get("offline_require_cjk", True))

    def _send(k):
        return core.should_translate(k, mode, origin="offline", require_cjk=req_cjk)

    # Which keys need work: translatable AND not yet done (out value still == key).
    todo = [k for k in src if _send(k) and out.get(k, k) == k]
    limit = int(cfg.get("limit", 0) or 0)
    if limit > 0:
        todo = todo[:limit]

    total_translatable = sum(1 for k in src if _send(k))

    # Guard: if require_cjk nuked almost everything, this is probably an
    # English-source export and the operator wants offline_require_cjk=false.
    if req_cjk and src:
        loose = sum(1 for k in src
                    if core.should_translate(k, mode, origin="offline",
                                             require_cjk=False))
        if loose >= 20 and total_translatable < loose * 0.10:
            print("WARNING: offline_require_cjk skipped %d of %d otherwise-"
                  "translatable entries (%.0f%%). If this game's source text is "
                  "English, set \"offline_require_cjk\": false in config.json."
                  % (loose - total_translatable, loose,
                     100.0 * (loose - total_translatable) / loose))

    return src, out, todo, total_translatable


# --------------------------------------------------------------------------- #
# one-batch worker (used ONLY by the parallel path)
# --------------------------------------------------------------------------- #
def _translate_one_batch(cfg, glossary, mode, bi, keys, mtpe):
    """Translate ONE batch end-to-end WITHOUT touching any shared state.

    Cross-batch context is DISABLED here (batch_context=None) because it is racy
    and meaningless when batches overlap; the within-batch ⟦N⟧ segments still give
    the model mutual context (all of a batch's sources go in one prompt, exactly
    as in the sequential path). Mirrors the sequential loop body's per-batch logic:
    call_llm -> parse_markers -> per-entry solo fallback on marker misalignment ->
    validate_or_recover per entry. Returns [(key, fixed, ok), ...]. Never raises
    for a translation problem — the LLM/marker error falls back to solo, a solo
    error keeps the original, and validate_or_recover keeps the original on an
    unrecoverable structure mismatch."""
    sources = keys  # key IS the untranslated source
    max_tokens = mtpe * len(sources)
    batch_context = None  # cross-batch context off under concurrency

    translations = None
    try:
        raw = core.call_llm(
            cfg,
            core.build_batch_user_msg(sources, glossary, mode, batch_context),
            float(cfg.get("temperature", 0.3)),
            max_tokens,
            mode,
        )
        translations = core.parse_markers(raw, len(sources))
    except Exception as e:
        print("  batch %d LLM error (%r) -> solo fallback" % (bi, e))
        translations = None

    if translations is None:
        # Marker misalignment or error: NEVER zip mismatched lists.
        translations = []
        for s in sources:
            try:
                translations.append(
                    core.translate_solo(
                        cfg, glossary, s,
                        float(cfg.get("temperature", 0.3)), mode, batch_context
                    )
                )
            except Exception as e:
                core.log_failure(s, "<solo error: %r>" % e, "solo_exception")
                translations.append(s)  # keep original

    results = []
    for k, tr in zip(keys, translations):
        fixed, ok = core.validate_or_recover(cfg, glossary, k, tr, mode, batch_context)
        results.append((k, fixed, ok))
    return results


# --------------------------------------------------------------------------- #
# parallel batch runner (concurrency > 1)
# --------------------------------------------------------------------------- #
def _run_parallel(cfg, glossary, mode, batches, total_batches, out, out_path,
                  mtpe, save_every, concurrency, on_progress, on_line, stop_flag):
    """Run batches across a ThreadPoolExecutor. Returns (batches_done, failures,
    stopped).

    Each worker translates ONE batch (the slow LLM work) with NO lock held, then
    takes a single lock to apply its results. Under that lock — and only there —
    we mutate the shared `out` dict, bump the `failures` counter, bump the
    completed-batch counter, write the checkpoint (so atomic_write_json can never
    run while `out` is being mutated), and deliver on_progress with a snapshot of
    the completed count so it is MONOTONIC. on_line is fired AFTER the lock, so it
    may arrive out of order across batches (order is not meaningful for the feed).

    stop_flag: when set we stop SUBMITTING new batches but let already in-flight
    ones finish (never tear a batch apart); the caller flushes to disk afterwards.
    `batches_done` is the count of batches that actually completed."""
    lock = threading.Lock()
    completed = 0
    failures = 0
    stopped = False

    def worker(bi, keys):
        nonlocal completed, failures
        try:
            results = _translate_one_batch(cfg, glossary, mode, bi, keys, mtpe)
        except Exception as e:  # last-resort guard: a task must never kill the run
            core.log_failure("<batch %d>" % bi, "<batch error: %r>" % e,
                             "batch_exception")
            results = [(k, k, False) for k in keys]  # keep originals, count as fails
        with lock:
            for (k, fixed, ok) in results:
                out[k] = fixed
                if not ok:
                    failures += 1
            completed += 1
            snap_done = completed
            snap_fail = failures
            # Checkpoint under the lock: never races an out mutation.
            if snap_done % save_every == 0:
                core.atomic_write_json(out_path, out)
            # Deliver under the lock so the completed count is monotonic.
            if on_progress is not None:
                on_progress(snap_done, total_batches, snap_fail)
        # on_line may fire out of order across batches — order is not meaningful.
        if on_line is not None:
            for (k, fixed, ok) in results:
                on_line(k, fixed)

    pending = iter(list(enumerate(batches, 1)))  # (bi, keys), bi is 1-based

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        inflight = set()

        def submit_one():
            for bi, keys in pending:
                inflight.add(ex.submit(worker, bi, keys))
                return True
            return False

        # Prime the pool (skip entirely if stop was requested before we started).
        if stop_flag is not None and stop_flag.is_set():
            stopped = True
        else:
            for _ in range(concurrency):
                if not submit_one():
                    break

        # Collect out of order; refill the window until stop or exhaustion.
        while inflight:
            fut = next(concurrent.futures.as_completed(inflight))
            inflight.discard(fut)
            exc = fut.exception()  # worker() swallows its own errors; belt-and-braces
            if exc is not None:
                core.log_failure("<worker>", "<crash: %r>" % exc, "worker_crash")
            if stop_flag is not None and stop_flag.is_set():
                stopped = True  # stop submitting; let in-flight batches drain
            else:
                submit_one()

    return completed, failures, stopped


# --------------------------------------------------------------------------- #
# reusable batch loop (CLI + UI both call this)
# --------------------------------------------------------------------------- #
def run_translation(in_path, out_path, cfg, glossary, mode,
                    on_progress=None, on_line=None, stop_flag=None):
    """Translate in_path -> out_path with the shared core, batch by batch.

    Callbacks (all optional; UI supplies them, CLI supplies on_progress):
      on_progress(batches_done, total_batches, failures) -- after each batch.
      on_line(source, translation)                       -- per translated entry.
    stop_flag (threading.Event): if set, cancel cleanly BETWEEN batches (the
    already-finished work is flushed to disk first).

    Concurrency (cfg['concurrency'], default 1):
      * 1  -> the SEQUENTIAL path below, unchanged. Cross-batch context (the tail
              of the previous batch fed forward) is preserved exactly, so a
              --no-context run stays byte-identical to the pre-context version.
      * >1 -> the PARALLEL path (_run_parallel): batches run on a thread pool.
              Cross-batch context is AUTO-DISABLED (it is racy/meaningless once
              batches overlap); within-batch ⟦N⟧ mutual context is unchanged. Note
              that under concurrency on_line/on_progress fire FROM WORKER THREADS,
              and on_line may arrive OUT OF ORDER (on_progress' completed count is
              still monotonic). Callbacks must be thread-safe (the UI's only
              enqueue onto a queue.Queue, which is).

    This function writes NO header/summary to stdout (the CLI prints those); it
    only emits, on the rare LLM/marker error, the same "solo fallback" notice the
    original CLI did (plus, under concurrency, a one-time context-disabled notice)
    so batch behavior stays observably identical. Returns a summary dict with the
    counts the CLI's "Done." line needs.
    """
    context_enabled = bool(cfg.get("context_enabled", True))
    context_window = int(cfg.get("context_window", 4))
    ctx = collections.deque(maxlen=context_window)  # rolling (source, result) pairs

    src, out, todo, total_translatable = plan_translation(in_path, out_path, cfg, mode)

    batch_size = int(cfg.get("batch_size", 40))
    mtpe = int(cfg.get("max_tokens_per_entry", 60))
    save_every = int(cfg.get("save_every_batches", 5))
    concurrency = int(cfg.get("concurrency", 1) or 1)
    if concurrency < 1:
        concurrency = 1

    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    total_batches = len(batches)
    batches_done = 0
    failures = 0
    stopped = False
    t0 = time.time()

    if concurrency == 1:
        # ---- SEQUENTIAL PATH — unchanged v1 behavior (cross-batch context on). ----
        for bi, keys in enumerate(batches, 1):
            # Cancellation is honored between batches so we never tear a batch apart.
            if stop_flag is not None and stop_flag.is_set():
                stopped = True
                break

            sources = keys  # key IS the untranslated source
            max_tokens = mtpe * len(sources)

            # Snapshot the tail of the PREVIOUS batches as reference context for this
            # batch (None when disabled -> byte-identical prompt). Within a batch the
            # ⟦N⟧ segments already give the model mutual context.
            batch_context = list(ctx) if context_enabled else None

            translations = None
            try:
                raw = core.call_llm(
                    cfg,
                    core.build_batch_user_msg(sources, glossary, mode, batch_context),
                    float(cfg.get("temperature", 0.3)),
                    max_tokens,
                    mode,
                )
                translations = core.parse_markers(raw, len(sources))
            except Exception as e:
                print("  batch %d LLM error (%r) -> solo fallback" % (bi, e))
                translations = None

            if translations is None:
                # Marker misalignment or error: NEVER zip mismatched lists.
                translations = []
                for s in sources:
                    try:
                        translations.append(
                            core.translate_solo(
                                cfg, glossary, s,
                                float(cfg.get("temperature", 0.3)), mode, batch_context
                            )
                        )
                    except Exception as e:
                        core.log_failure(s, "<solo error: %r>" % e, "solo_exception")
                        translations.append(s)  # keep original

            # Per-entry structure validation (+ recovery).
            for k, tr in zip(keys, translations):
                fixed, ok = core.validate_or_recover(cfg, glossary, k, tr, mode, batch_context)
                out[k] = fixed
                if not ok:
                    failures += 1
                if context_enabled:
                    ctx.append((k, fixed))  # feed forward into the next batch's context
                if on_line is not None:
                    on_line(k, fixed)

            if bi % save_every == 0 or bi == total_batches:
                core.atomic_write_json(out_path, out)

            batches_done = bi

            if on_progress is not None:
                on_progress(bi, total_batches, failures)
    else:
        # ---- PARALLEL PATH — cross-batch context auto-disabled. ----
        print("concurrency=%d → cross-batch context disabled "
              "(within-batch context unchanged)" % concurrency)
        # Give the workers a config with context fully off, so each batch prompt is
        # identical to a --no-context batch (drops the reference-block system clause
        # too, not just the — already absent — cross-batch block).
        pcfg = dict(cfg)
        pcfg["context_enabled"] = False
        batches_done, failures, stopped = _run_parallel(
            pcfg, glossary, mode, batches, total_batches, out, out_path,
            mtpe, save_every, concurrency, on_progress, on_line, stop_flag)

    core.atomic_write_json(out_path, out)
    dt = time.time() - t0
    return {
        "out_path": out_path,
        "mode": mode,
        "src_count": len(src),
        "total_translatable": total_translatable,
        "todo": len(todo),
        "total_batches": total_batches,
        "batches_done": batches_done,
        "failures": failures,
        "elapsed": dt,
        "stopped": stopped,
        "concurrency": concurrency,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Smart batch eroge translator (MTool JSON)."
    )
    ap.add_argument("input", help="MTool-exported ManualTransFile.json")
    ap.add_argument(
        "output",
        nargs="?",
        default=None,
        help="output path (default: <input>.translated.json alongside input)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only process the first N translatable entries (smoke test)",
    )
    ap.add_argument(
        "--mode",
        choices=["dynamic", "strict_ja"],
        default=None,
        help="override config 'mode' (default: whatever config.json says)",
    )
    ap.add_argument(
        "--no-context",
        action="store_true",
        help="disable cross-batch dialogue context "
        "(byte-identical to pre-context behavior)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="parallel batch workers (overrides config 'concurrency'). "
        "1 = sequential with cross-batch context; >1 runs batches in parallel "
        "and auto-disables cross-batch context. Optimal = the LLM server's "
        "parallel slot count (4 on this rig).",
    )
    args = ap.parse_args()

    cfg = core.load_config()
    glossary = core.load_glossary()
    mode = core.get_mode(cfg, args.mode)

    # Context: carry the last few source->result pairs from the previous batch
    # into the next batch's prompt (the flat MTool dict is only best-effort story
    # order, so this is a heuristic — still helps register/pronoun continuity).
    # --no-context forces cfg["context_enabled"]=False so core.system_prompt also
    # drops its context clause, making the whole path byte-identical to v1.
    if args.no_context:
        cfg["context_enabled"] = False
    if args.concurrency is not None:
        cfg["concurrency"] = args.concurrency
    # limit rides in cfg so plan_translation (used for the header AND inside
    # run_translation) applies it identically.
    cfg["limit"] = args.limit

    in_path = args.input
    if args.output:
        out_path = args.output
    else:
        root, ext = os.path.splitext(in_path)
        out_path = root + ".translated" + ext

    if os.path.abspath(in_path) == os.path.abspath(out_path):
        print("ERROR: output must not overwrite input.", file=sys.stderr)
        sys.exit(2)

    # Header (computed with the SAME planner run_translation uses).
    _src, _out, todo, total_translatable = plan_translation(in_path, out_path, cfg, mode)
    print(
        "Mode: %s | Entries: %d total | %d translatable | %d to do this run%s"
        % (
            mode,
            len(_src),
            total_translatable,
            len(todo),
            (" (--limit %d)" % args.limit) if args.limit else "",
        )
    )

    def on_progress(done, total, failures):
        print("[%d/%d] batches, %d failures" % (done, total, failures))

    summary = run_translation(
        in_path, out_path, cfg, glossary, mode, on_progress=on_progress
    )

    dt = summary["elapsed"]
    print(
        "Done. Wrote %s | %d batches | %d failures | %.1fs (%.2fs/entry)"
        % (
            summary["out_path"],
            summary["total_batches"],
            summary["failures"],
            dt,
            dt / max(1, summary["todo"]),
        )
    )


if __name__ == "__main__":
    main()
