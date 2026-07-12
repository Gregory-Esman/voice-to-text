"""Unit tests for windows/streaming.py + vtt_core.start_case — run with the venv:
  .venv\\Scripts\\python.exe tests\\test_streaming.py
Uses fakes for transcribe/clean, so it hits no network and is deterministic.
Covers: sentence-case helper, pause chunking, in-order assembly, deterministic
boundary capitalization, and the no-pause (single tail) path.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "windows"))
import vtt_core as core       # noqa: E402
import streaming as S         # noqa: E402

SR = core.SAMPLE_RATE
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ── start_case ──
def test_start_case():
    check("fresh lowercase -> capital", core.start_case("yeah okay") == "Yeah okay")
    check("strips leading space on fresh", core.start_case("  hello") == "Hello")
    check("leaves number-leading alone", core.start_case("3 apples") == "3 apples")
    check("already capital untouched", core.start_case("Hello") == "Hello")
    check("continuation after period -> capital",
          core.start_case("and then i left", "I went.") == "And then i left")
    check("mid-sentence continuation stays lowercase",
          core.start_case("and then i left", "I went") == "and then i left")
    check("period behind closing quote still ends sentence",
          core.start_case("sure", 'He said "ok."') == "Sure")


# ── audio helpers ──
def _speech(dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = 0.3 * np.sin(2 * np.pi * 160 * t) + 0.15 * np.sin(2 * np.pi * 320 * t)
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)
    return (sig * env + 0.02 * np.random.randn(n)).astype("float32")


def _sil(dur):
    return (0.0008 * np.random.randn(int(dur * SR))).astype("float32")


def _run(full, raws, clean_fn, min_chunk=1.0):
    """Drive a DictationStream by feeding `full` in 100 ms steps (mimics the mic
    callback), transcribe returns preset `raws` per chunk in order."""
    seq = {"i": 0}

    def ftrans(a):
        i = seq["i"]
        seq["i"] += 1
        return raws[i] if i < len(raws) else ""

    buf = {"a": np.zeros(0, "float32")}
    ds = S.DictationStream(lambda: buf["a"], ftrans, clean_fn,
                           log=lambda *a, **k: None, min_chunk=min_chunk, poll_ms=50)
    ds.start()
    step = int(0.1 * SR)
    for i in range(0, full.size, step):
        buf["a"] = full[:i + step].copy()
        time.sleep(0.015)
    time.sleep(0.12)
    return ds.finish(full), seq["i"]


# A fake cleaner: trim, drop "um/uh", add a period, capitalize per prev (as the
# real deterministic boundary fix in the worker will also enforce).
def _fake_clean(raw, prev):
    words = [w for w in raw.split() if w.lower() not in ("um", "uh")]
    out = " ".join(words).strip()
    return (out + ".") if out else ""


def test_pause_chunking_and_order():
    full = np.concatenate([_speech(1.6), _sil(0.8), _speech(1.6),
                           _sil(0.8), _speech(1.4)])
    raws = ["first part here", "um second part", "third and final part"]
    res, n = _run(full, raws, _fake_clean)
    res = core.start_case(res)
    check("three segments -> 3 transcribe calls", n == 3)
    check("assembled in spoken order",
          res == "First part here. Second part. Third and final part.")
    check("filler dropped by cleaner", "um" not in res.lower().split())
    check("no double spaces", "  " not in res)
    check("boundary capitalized after period", ". Second" in res and ". Third" in res)


def test_no_pause_single_tail():
    # One breath, no gaps: nothing is cut mid-way; the whole clip is the tail.
    full = _speech(2.2)
    res, n = _run(full, ["just one continuous thought"], _fake_clean)
    res = core.start_case(res)
    check("no-pause path still produces text", res == "Just one continuous thought.")
    check("no-pause path = single transcribe call", n == 1)


def test_empty_recording():
    full = _sil(1.5)
    res, n = _run(full, [""], _fake_clean)
    check("silence -> empty result", res == "")


if __name__ == "__main__":
    test_start_case()
    test_pause_chunking_and_order()
    test_no_pause_single_tail()
    test_empty_recording()
    print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)
