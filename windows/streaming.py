"""Clean-during-pauses streaming for manual dictation (the #3 design).

While you hold a dictation open and pause to think, the audio you already spoke
is cut at the silence, transcribed, and cleaned IN THE BACKGROUND — so that work
happens in the dead time between sentences. At tap-stop only the final short
tail is left to process, then the already-cleaned pieces are assembled and pasted
once (no on-screen flash, no incremental typing).

The engine is deliberately decoupled from the app: it's driven by three
callables so it can be unit-tested with fakes —
  snapshot()          -> np.ndarray of ALL audio captured so far (float32, 16k)
  transcribe(audio)   -> str   raw transcript for one chunk ("" to drop it)
  clean(raw, prev)    -> str   cleaned continuation of `raw` given prior text
"""
from __future__ import annotations

import queue
import threading
import time

import numpy as np

import vtt_core as core

SAMPLE_RATE = core.SAMPLE_RATE
_SENTINEL = object()


class DictationStream:
    """Chunk-during-pauses transcribe+clean pipeline for one dictation."""

    def __init__(self, snapshot, transcribe, clean, sr: int = SAMPLE_RATE,
                 log=None, min_chunk: float = 1.2, poll_ms: int = 250) -> None:
        self._snapshot = snapshot
        self._transcribe = transcribe
        self._clean = clean
        self._sr = sr
        self._log = log or (lambda *a, **k: None)
        self._min_chunk = int(min_chunk * sr)
        self._poll = poll_ms / 1000.0
        self._processed = 0            # samples already cut into chunks
        self._prefix = ""              # running cleaned text (also continuation ctx)
        self._plock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._poller = None
        self._worker = None
        self._n = 0                    # chunk counter (for logs)

    # ── lifecycle ──
    def start(self) -> None:
        self._worker = threading.Thread(target=self._work, name="vtt-stream-worker",
                                        daemon=True)
        self._worker.start()
        self._poller = threading.Thread(target=self._poll_loop, name="vtt-stream-poll",
                                       daemon=True)
        self._poller.start()

    def finish(self, audio: np.ndarray | None = None) -> str:
        """Stop cutting, process the final tail, drain, return assembled text.

        `audio` is the authoritative full recording (the app clears its live
        frame buffer at tap-stop, so the tail can't be read from snapshot() any
        more). Falls back to snapshot() when not given."""
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=self._poll * 4)
        if audio is None:
            audio = self._audio()
        tail = audio[self._processed:] if audio.size > self._processed else np.zeros(0, "float32")
        if tail.size:
            self._q.put((self._next_idx(), tail))
        self._q.put(_SENTINEL)
        if self._worker:
            self._worker.join(timeout=90)
        with self._plock:
            return self._prefix.strip()

    def cancel(self) -> None:
        self._stop.set()
        self._q.put(_SENTINEL)

    # ── internals ──
    def _next_idx(self) -> int:
        self._n += 1
        return self._n

    def _audio(self) -> np.ndarray:
        try:
            a = self._snapshot()
        except Exception:
            return np.zeros(0, "float32")
        return a if a is not None and a.size else np.zeros(0, "float32")

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self._poll)
            audio = self._audio()
            if audio.size - self._processed < self._min_chunk:
                continue
            cut = core.find_pause(audio, self._processed, self._sr)
            if cut and cut - self._processed >= self._min_chunk:
                chunk = audio[self._processed:cut].copy()
                self._processed = cut
                self._q.put((self._next_idx(), chunk))
                self._log("stream: cut chunk at %.1fs (%.1fs of audio)",
                          cut / self._sr, audio.size / self._sr)

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is _SENTINEL:
                return
            idx, chunk = item
            try:
                raw = (self._transcribe(chunk) or "").strip()
                if not raw:
                    continue
                with self._plock:
                    prev = self._prefix
                cleaned = (self._clean(raw, prev) or "").strip()
                if not cleaned:
                    continue
                # Enforce the boundary case deterministically (a fast model is
                # inconsistent about capitalizing after a prior sentence).
                cleaned = core.start_case(cleaned, prev)
                with self._plock:
                    self._prefix = self._join(self._prefix, cleaned)
                self._log("stream: chunk %d -> %d raw / %d clean chars",
                          idx, len(raw), len(cleaned))
            except Exception:
                self._log("stream: chunk %d failed", idx)

    @staticmethod
    def _join(prefix: str, piece: str) -> str:
        if not prefix:
            return piece
        sep = "" if prefix[-1] in " \n\t" else " "
        return prefix + sep + piece
