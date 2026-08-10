"""Unit tests for mac_dictation.py — run with the project venv:
  uv run python tests/test_mac_dictation.py
Uses fakes for the FlowApp surface + monkeypatches vtt_core.chat_complete /
contains_speech, so it hits no network and is deterministic. Covers:
_clean_backend's online/offline routing, clean_chunk's backend passthrough and
fail-open behavior, finalize's deterministic vs. LLM-cleaned paths, personal/
replacement fixers, maybe_start_stream's gating, and an end-to-end
DictationStream run (ordered assembly + boundary capitalization).
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import portable                                    # noqa: E402
from portable import vtt_core, autodictate, streaming  # noqa: E402
import mac_dictation                                # noqa: E402

SR = vtt_core.SAMPLE_RATE
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ── fakes ──
def base_cfg(clean=True, stream=True, command_base_url="", backend="local",
            personal=None, replacements=None):
    return {
        "dictation": {"clean": clean, "stream": stream,
                      "model": "llama-3.1-8b-instant",
                      "model_local": "llama3.1:8b", "tone": ""},
        "formatting": {"ollama_url": "http://localhost:11434",
                      "model": "gpt-oss:20b", "command_model": "",
                      "command_base_url": command_base_url,
                      "command_api_key_env": "OPENAI_API_KEY",
                      "command_api_key_file": ""},
        "transcription": {"backend": backend, "language": "en",
                          "model": "base", "vocabulary": ""},
        "personal": personal if personal is not None else
                    {"name": "Jamie Rivera", "email": "jamie@example.com"},
        "replacements": replacements if replacements is not None else
                       {"Claude Coe": "Claude Code"},
    }


class FakeRecorder:
    def __init__(self):
        self.audio = np.zeros(0, dtype="float32")

    def snapshot(self):
        return self.audio


class FakeApp:
    """cfg + recorder.snapshot + a canned/sequenced transcribe_for_auto."""

    def __init__(self, cfg, transcript="", sequence=None):
        self.cfg = cfg
        self.recorder = FakeRecorder()
        self._transcript = transcript
        self._sequence = sequence
        self._i = 0

    def transcribe_for_auto(self, audio, vocabulary=None):
        if self._sequence is not None:
            i = self._i
            self._i += 1
            return self._sequence[i] if i < len(self._sequence) else ""
        return self._transcript


# ── 1. _clean_backend ──
def test_clean_backend():
    ollama_url, model, base_url, key_env, key_file = mac_dictation._clean_backend(
        base_cfg(command_base_url="https://api.groq.com/openai/v1"))
    check("online: base_url passthrough", base_url == "https://api.groq.com/openai/v1")
    check("online: uses dictation.model", model == "llama-3.1-8b-instant")

    ollama_url, model, base_url, key_env, key_file = mac_dictation._clean_backend(
        base_cfg(command_base_url=""))
    check("offline: base_url == ''", base_url == "")
    check("offline: uses dictation.model_local", model == "llama3.1:8b")
    check("offline: ollama_url from [formatting]", ollama_url == "http://localhost:11434")


# ── 2. clean_chunk ──
def test_clean_chunk():
    captured = {}

    def fake_chat_complete(messages, url, model, temperature, base_url="",
                           api_key_env="OPENAI_API_KEY", api_key_file=""):
        captured["base_url"] = base_url
        captured["model"] = model
        return "Cleaned text."

    orig = vtt_core.chat_complete
    vtt_core.chat_complete = fake_chat_complete
    try:
        mac_dictation.clean_chunk(base_cfg(command_base_url=""), "hello there", "")
        check("offline clean_chunk calls chat_complete with base_url=''",
              captured["base_url"] == "")

        mac_dictation.clean_chunk(
            base_cfg(command_base_url="https://api.groq.com/openai/v1"),
            "hello there", "")
        check("online clean_chunk passes base_url through",
              captured["base_url"] == "https://api.groq.com/openai/v1")
    finally:
        vtt_core.chat_complete = orig

    def raising_chat_complete(*a, **k):
        raise RuntimeError("boom")

    vtt_core.chat_complete = raising_chat_complete
    try:
        out = mac_dictation.clean_chunk(base_cfg(), "hello there", "")
        check("cleaner raising -> clean_chunk returns ''", out == "")
    finally:
        vtt_core.chat_complete = orig


# ── 3. finalize ──
def test_finalize():
    app = FakeApp(base_cfg(clean=False))
    out = mac_dictation.finalize(app, "go to google dot com.")
    check("finalize clean=false: deterministic casing + URL fix",
          out == "Go to google.com")

    orig_clean_chunk = mac_dictation.clean_chunk
    mac_dictation.clean_chunk = lambda cfg, raw, prev, whole=False: "cleaned version wins"
    try:
        app = FakeApp(base_cfg(clean=True))
        out = mac_dictation.finalize(app, "raw text here")
        check("finalize clean=true: cleaned text wins (sentence-cased)",
              out == "Cleaned version wins")
    finally:
        mac_dictation.clean_chunk = orig_clean_chunk

    mac_dictation.clean_chunk = lambda cfg, raw, prev, whole=False: ""
    try:
        app = FakeApp(base_cfg(clean=True))
        out = mac_dictation.finalize(app, "raw text here")
        check("finalize: cleaner failing -> raw text survives (sentence-cased)",
              out == "Raw text here")
    finally:
        mac_dictation.clean_chunk = orig_clean_chunk


# ── 4. fixers ──
def test_fixers():
    cfg = base_cfg(personal={"name": "Jamie Rivera", "email": "jamie@example.com"},
                   replacements={"Claude Coe": "Claude Code"})
    fixers = mac_dictation.build_fixers_from_cfg(cfg)
    out = autodictate.apply_fixers("my email is jamie at example dot com", fixers)
    check("spoken email form -> real address", "jamie@example.com" in out)

    out2 = autodictate.apply_fixers("I used Claude Coe today", fixers)
    check("[replacements] pair applied", out2 == "I used Claude Code today")

    # [personal] must never leak into STT vocabulary bias.
    empty_fixers_cfg = base_cfg(personal={}, replacements={})
    check("no personal/replacements -> no fixers built",
          mac_dictation.build_fixers_from_cfg(empty_fixers_cfg) == [])


# ── 5. maybe_start_stream ──
def test_maybe_start_stream():
    check("clean=false -> None",
          mac_dictation.maybe_start_stream(
              FakeApp(base_cfg(clean=False, stream=True))) is None)
    check("stream=false -> None",
          mac_dictation.maybe_start_stream(
              FakeApp(base_cfg(clean=True, stream=False))) is None)
    check("backend=assemblyai -> None",
          mac_dictation.maybe_start_stream(
              FakeApp(base_cfg(clean=True, stream=True, backend="assemblyai"))) is None)

    ds = mac_dictation.maybe_start_stream(
        FakeApp(base_cfg(clean=True, stream=True, backend="local")))
    check("otherwise -> a live DictationStream", isinstance(ds, streaming.DictationStream))
    if ds is not None:
        ds.cancel()


# ── 6. end-to-end DictationStream run (fake transcribe/clean) ──
def _speech(dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = 0.3 * np.sin(2 * np.pi * 160 * t) + 0.15 * np.sin(2 * np.pi * 320 * t)
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)
    return (sig * env + 0.02 * np.random.randn(n)).astype("float32")


def _sil(dur):
    return (0.0008 * np.random.randn(int(dur * SR))).astype("float32")


def test_dictation_stream_end_to_end():
    cfg = base_cfg(clean=True, stream=True)
    raws = ["first part here", "um second part", "third and final part"]
    app = FakeApp(cfg, sequence=raws)
    fixers = mac_dictation.build_fixers_from_cfg(cfg)

    def fake_clean(cfg_, raw, prev):
        words = [w for w in raw.split() if w.lower() not in ("um", "uh")]
        out = " ".join(words).strip()
        return (out + ".") if out else ""

    orig_contains_speech = vtt_core.contains_speech
    orig_clean_chunk = mac_dictation.clean_chunk
    vtt_core.contains_speech = lambda audio, sr=SR: True  # bypass VAD on synthetic audio
    mac_dictation.clean_chunk = fake_clean
    try:
        buf = {"a": np.zeros(0, "float32")}
        ds = streaming.DictationStream(
            snapshot=lambda: buf["a"],
            transcribe=lambda a: mac_dictation.transcribe_chunk(app, a, fixers),
            clean=lambda raw, prev: mac_dictation.clean_chunk(app.cfg, raw, prev),
            log=lambda *a, **k: None, min_chunk=1.0, poll_ms=50)
        ds.start()
        full = np.concatenate([_speech(1.6), _sil(0.8), _speech(1.6),
                               _sil(0.8), _speech(1.4)])
        step = int(0.1 * SR)
        for i in range(0, full.size, step):
            buf["a"] = full[:i + step].copy()
            time.sleep(0.015)
        time.sleep(0.12)
        res = ds.finish(full)
        res = vtt_core.start_case(res)
        check("end-to-end ordered assembly across chunks",
              res == "First part here. Second part. Third and final part.")
    finally:
        vtt_core.contains_speech = orig_contains_speech
        mac_dictation.clean_chunk = orig_clean_chunk


# ── 7. clean_scope: WHERE the cleanup LLM runs ──
def test_clean_scope():
    check("clean_scope defaults to 'final'",
          mac_dictation.clean_scope(base_cfg()) == "final")
    cfg = base_cfg()
    cfg["dictation"]["clean_scope"] = "chunk"
    check("clean_scope 'chunk' honored", mac_dictation.clean_scope(cfg) == "chunk")
    cfg["dictation"]["clean_scope"] = "nonsense"
    check("clean_scope falls back to 'final' on a bad value",
          mac_dictation.clean_scope(cfg) == "final")


# ── 8. final-scope: cleanup runs ONCE, over the whole transcript ──
def test_final_scope_defers_cleanup():
    """The regression that caused the fragmented output: in "final" scope the
    per-chunk path must NOT call the cleanup model (a lone fragment can't be
    punctuated correctly), and finish_stream must call it exactly once with the
    whole assembled transcript and whole=True."""
    calls = []

    def fake_clean_chunk(cfg, raw, prev, whole=False):
        calls.append({"raw": raw, "prev": prev, "whole": whole})
        return "One clean sentence that spans the pause."

    orig_clean_chunk = mac_dictation.clean_chunk
    orig_contains_speech = vtt_core.contains_speech
    mac_dictation.clean_chunk = fake_clean_chunk
    vtt_core.contains_speech = lambda a, sr=SR: a.size > 0
    try:
        cfg = base_cfg()
        cfg["dictation"]["clean_scope"] = "final"
        app = FakeApp(cfg, sequence=["First part here.", "second part.", "third part."])
        buf = {"a": np.zeros(0, dtype="float32")}
        app.recorder.snapshot = lambda: buf["a"]
        ds = mac_dictation.maybe_start_stream(app)

        full = np.concatenate([_speech(1.6), _sil(1.0), _speech(1.6),
                               _sil(1.0), _speech(1.4)])
        step = int(0.1 * SR)
        for i in range(0, full.size, step):
            buf["a"] = full[:i + step].copy()
            time.sleep(0.015)
        time.sleep(0.12)
        out = mac_dictation.finish_stream(app, ds, full)

        per_chunk = [c for c in calls if not c["whole"]]
        check("final scope: cleanup model never runs per chunk", per_chunk == [])
        whole_calls = [c for c in calls if c["whole"]]
        check("final scope: exactly one whole-transcript cleanup call",
              len(whole_calls) == 1)
        # Case-insensitive: the per-chunk start_case() still runs, so a chunk
        # following a period arrives capitalized ("third part." -> "Third part.").
        raw_seen = whole_calls[0]["raw"].lower() if whole_calls else ""
        check("final scope: that call sees ALL the chunks, not a fragment",
              len(whole_calls) == 1 and "first part here." in raw_seen
              and "second part." in raw_seen and "third part." in raw_seen)
        check("final scope: cleaned text is what gets returned",
              out == "One clean sentence that spans the pause.")
    finally:
        mac_dictation.clean_chunk = orig_clean_chunk
        vtt_core.contains_speech = orig_contains_speech

    # A cleanup failure must fall back to the raw transcript, never lose words.
    mac_dictation.clean_chunk = lambda cfg, raw, prev, whole=False: ""
    vtt_core.contains_speech = lambda a, sr=SR: a.size > 0
    try:
        cfg = base_cfg()
        cfg["dictation"]["clean_scope"] = "final"
        app = FakeApp(cfg, sequence=["words that must survive."])
        buf = {"a": np.zeros(0, dtype="float32")}
        app.recorder.snapshot = lambda: buf["a"]
        ds = mac_dictation.maybe_start_stream(app)
        full = _speech(1.6)
        out = mac_dictation.finish_stream(app, ds, full)
        check("final scope: cleanup returning '' falls back to raw words",
              "words that must survive" in out.lower())
    finally:
        mac_dictation.clean_chunk = orig_clean_chunk
        vtt_core.contains_speech = orig_contains_speech


# ── 9. stitch / strict_lists reach the prompt builder ──
def test_stitch_and_strict_lists_flags():
    seen = {}

    def fake_chat_complete(messages, url, model, temperature, base_url="",
                           api_key_env="OPENAI_API_KEY", api_key_file=""):
        seen["system"] = messages[0]["content"]
        return "ok."

    orig = vtt_core.chat_complete
    vtt_core.chat_complete = fake_chat_complete
    try:
        cfg = base_cfg()
        mac_dictation.clean_chunk(cfg, "hello there", "", whole=True)
        check("whole=True adds the sentence-boundary repair rules",
              "SENTENCE-BOUNDARY REPAIR" in seen["system"])
        check("strict list rule on by default",
              "LIST FORMATTING IS STRICT" in seen["system"])

        mac_dictation.clean_chunk(cfg, "hello there", "", whole=False)
        check("a lone chunk never gets boundary-repair rules",
              "SENTENCE-BOUNDARY REPAIR" not in seen["system"])

        cfg["dictation"]["stitch_fragments"] = False
        mac_dictation.clean_chunk(cfg, "hello there", "", whole=True)
        check("stitch_fragments=false disables the repair rules",
              "SENTENCE-BOUNDARY REPAIR" not in seen["system"])

        cfg["dictation"]["strict_lists"] = False
        mac_dictation.clean_chunk(cfg, "hello there", "", whole=True)
        check("strict_lists=false disables the strict list rule",
              "LIST FORMATTING IS STRICT" not in seen["system"])
    finally:
        vtt_core.chat_complete = orig


if __name__ == "__main__":
    test_clean_backend()
    test_clean_chunk()
    test_finalize()
    test_fixers()
    test_maybe_start_stream()
    test_dictation_stream_end_to_end()
    test_clean_scope()
    test_final_scope_defers_cleanup()
    test_stitch_and_strict_lists_flags()
    print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)
