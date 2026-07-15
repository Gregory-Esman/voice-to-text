"""Voice-To-Text — Windows agent (online / Groq mode).

Phase 0: tray + tap hotkeys + record + Groq transcribe + Ctrl+V paste + Write/Edit.
Phase 1: recording HUD overlay + sounds + autostart.
Phase 2: UI Automation screen-context + thread-context stitching.

Run:  pythonw windows\\app.py     (or: python windows\\app.py for a console)
Needs a Groq key — see windows\\config.example.toml and README.

This reuses vtt_core.py (the portable brain). The macOS app (flow.py) is
unchanged; this is a separate backend that talks to the same logic.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import logging

import numpy as np
import sounddevice as sd
from pynput import keyboard

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vtt_core as core          # noqa: E402
import backend as os_back        # noqa: E402
import autodictate as autod      # noqa: E402
import streaming as stream_mod   # noqa: E402

try:
    import tomllib               # Python 3.11+
except ModuleNotFoundError:      # pragma: no cover
    tomllib = None

SAMPLE_RATE = core.SAMPLE_RATE
APP_NAME = "Voice-To-Text"
_LOG = logging.getLogger("vtt")

DEFAULT_CFG = {
    "transcription": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3",
        "language": "en",
        "vocabulary": "",
        "api_key_env": "GROQ_API_KEY",
        "api_key_file": "groq_key",
    },
    "formatting": {
        "command_base_url": "https://api.groq.com/openai/v1",
        "command_model": "openai/gpt-oss-120b",
        "api_key_env": "GROQ_API_KEY",
        "api_key_file": "groq_key",
    },
    # Dictation cleanup: run the transcript through a fast writing model so pauses
    # and run-ons come out as clean written text. clean=on/off; stream=clean each
    # pause-delimited chunk DURING your pauses (hides the latency) vs one pass at
    # tap-stop. model is a SMALL/fast model (cleanup is easy; speed matters at the
    # tail) — separate from the heavier [formatting] Write model. tone="excited"
    # lets an energetic delivery earn a "!".
    "dictation": {"clean": True, "stream": True,
                  "model": "llama-3.1-8b-instant", "tone": ""},
    # dictate_key  = manual long-form dictation (tap to start, tap to stop)
    # toggle_auto_key = master switch for Auto-Dictate (text box = live mic)
    "hotkey": {"dictate_key": "ctrl_r", "toggle_auto_key": "tilde"},
    "audio": {"input_device": "default"},
    "sounds": {"enabled": True},
    # Auto-Dictate: focused editable text box = live mic (AUTO-DICTATE-BRIEF.md)
    "auto_dictate": {"enabled": False, "similarity": 0.60,
                     "silence_ms": 700, "min_speech_ms": 180,
                     "start_rms": 0.014, "end_rms": 0.008,
                     "echo_corr": 0.55, "adapt": True,
                     "send_in_terminal": False, "exclude_apps": []},
    # who the user is — biases transcription, powers "type my email" snippets,
    # and fixes spoken forms ("... at gmail dot com") into the real address
    "personal": {"name": "", "email": "", "phone": ""},
    "replacements": {},        # literal transcript fixups: "wrong" = "right"
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(base.get(k, {}), v) if isinstance(v, dict) else v
    return out


def load_config() -> dict:
    paths = []
    if os.environ.get("VTT_CONFIG"):
        paths.append(os.environ["VTT_CONFIG"])
    paths.append(os.path.join(os.environ.get("APPDATA", ""), APP_NAME, "config.toml"))
    paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml"))
    for p in paths:
        try:
            if p and tomllib and os.path.exists(p):
                with open(p, "rb") as f:
                    return _deep_merge(DEFAULT_CFG, tomllib.load(f))
        except Exception as e:
            print(f"[config] {p}: {e}")
    return dict(DEFAULT_CFG)


# trigger-key names → the pynput Key we compare against
_KEYMAP = {
    "alt_l": keyboard.Key.alt_l, "alt_r": keyboard.Key.alt_r,
    "alt": keyboard.Key.alt, "alt_gr": keyboard.Key.alt_gr,
    "ctrl_r": keyboard.Key.ctrl_r, "f9": keyboard.Key.f9, "f10": keyboard.Key.f10,
}

# Printable trigger keys → (char to match, Win32 virtual-key to suppress). The
# grave/tilde key is VK_OEM_3 (0xC0) and types "`" unshifted; we suppress it so
# using it as a hotkey doesn't insert a stray backtick.
_PRINTABLE = {
    "tilde": ("`", 0xC0), "grave": ("`", 0xC0),
    "backtick": ("`", 0xC0), "`": ("`", 0xC0),
}


def _resolve_trigger(name, default):
    """(token, suppress_vk, shift) for a config hotkey name. A "shift+" prefix
    means the trigger only fires while Shift is held (printable keys only — the
    tilde key is shared, e.g. dictate="tilde" + command="shift+tilde").
    token: a pynput Key (special keys), a lowercase char (printable, unshifted),
    or ("shift", char) for a shifted printable — kept distinct so the two share
    one VK without colliding. suppress_vk: Win32 VK or None; shift: bool."""
    n = (name or "").strip().lower()
    shift = False
    if n.startswith("shift+"):
        shift, n = True, n[len("shift+"):].strip()
    if not shift and n in _KEYMAP:
        return _KEYMAP[n], None, False
    if n in _PRINTABLE:
        ch, vk = _PRINTABLE[n]
        return (("shift", ch) if shift else ch), vk, shift
    if n in _KEYMAP:                      # shift+<special> unsupported → ignore shift
        return _KEYMAP[n], None, False
    return default, None, False


def _key_token(key):
    """Normalize a pynput key event to a comparable token: the Key for special
    keys, the lowercase char for printable keys (None if neither)."""
    if isinstance(key, keyboard.Key):
        return key
    ch = getattr(key, "char", None)
    return ch.lower() if ch else None


def _shift_is_down() -> bool:
    """True if either Shift key is currently held (Win32 VK_SHIFT, real-time)."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except Exception:
        return False


def _resolve_input_device(spec):
    """Config input_device → a sounddevice device selector. 'default'/'' → None
    (system default); an integer string → that device index; otherwise the first
    input device whose name contains the string (case-insensitive)."""
    s = (str(spec) if spec is not None else "").strip()
    if not s or s.lower() == "default":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and s.lower() in d["name"].lower():
                return i
    except Exception:
        pass
    return None

IDLE, DICTATE, COMMAND = "idle", "dictate", "command"


class VoiceAgent:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.state = IDLE
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._capturing = False        # only collect mic frames between taps
        self._lock = threading.Lock()
        self.hud = os_back.RecordingHUD()
        self.ctx_log = core.ThreadContextLog()
        # command-mode capture
        self._sel = None
        self._prev_clip = None
        self._ctx = ""
        self._app = ("", "?", "")
        self._paused = False           # mic "off hot mode": ignore tap hotkeys
        self._sounds_on = bool(cfg.get("sounds", {}).get("enabled", True))
        # dictation cleanup (clean-during-pauses, the #3 design)
        dc = cfg.get("dictation", {})
        self._clean_on = bool(dc.get("clean", True))
        self._stream_on = bool(dc.get("stream", True)) and self._clean_on
        self._tone = (dc.get("tone") or "").strip() or None
        self._dstream = None           # active streaming pipeline during a dictation
        self._gui = None               # set in run(); the desktop window
        # ── Auto-Dictate (focused text box = live mic) ──
        ad = cfg.get("auto_dictate", {})
        self._auto_on = bool(ad.get("enabled", False))
        self._armed = False            # an editable text box has focus
        self._armed_id = None          # identity of that box
        self._force_arm = False        # onboarding try-it: treat our own box as armed
        self._auto_speaking = False    # endpointer capturing (drives the chip)
        self._auto_ep = autod.Endpointer(
            silence_ms=int(ad.get("silence_ms", 700)),
            min_speech_ms=int(ad.get("min_speech_ms", 180)),
            start_rms=float(ad.get("start_rms", 0.014)),
            end_rms=float(ad.get("end_rms", 0.008)))
        self._auto_q: queue.Queue = queue.Queue()
        self._auto_last: dict = {}     # box id -> (any_emitted, last emitted str)
        self._speaker = autod.SpeakerGate(
            os.path.join(os.environ.get("APPDATA", ""), APP_NAME,
                         "voice_profile.npy"),
            threshold=float(ad.get("similarity", 0.75)),
            adapt=bool(ad.get("adapt", True)))
        self._chip = os_back.AutoChip()
        self._focus = autod.FocusWatcher(self._on_focus_change)
        self._loopback = autod.LoopbackMonitor()
        self._echo_corr = float(ad.get("echo_corr", 0.55))
        self._send_in_terminal = bool(ad.get("send_in_terminal", False))
        excl = set()
        for x in (ad.get("exclude_apps") or []):
            e = str(x).strip().lower()
            if e:
                excl.add(e if e.endswith(".exe") else e + ".exe")
        autod.EXCLUDED_EXES = excl
        # personal details: snippets + transcript fixers + Whisper vocab bias
        self._base_vocab = cfg["transcription"].get("vocabulary", "")
        self._rebuild_personal()
        self._enrolling = False
        self._enroll_frames: list[np.ndarray] = []
        # resolve trigger keys → tap-detection + suppression tables
        self._listener = None
        hk = cfg["hotkey"]
        self._bind_triggers(hk.get("dictate_key", "ctrl_r"),
                            hk.get("toggle_auto_key", "tilde"))
        self._icon = None
        threading.Thread(target=self._auto_loop, name="vtt-auto",
                         daemon=True).start()

    # ───────────── audio ─────────────
    def _on_audio(self, indata, frames, t, status):  # sounddevice callback
        if self._capturing:            # manual (hotkey) capture in progress
            self._frames.append(indata[:, 0].copy())
            lvl = float(np.sqrt(np.mean(indata[:, 0] ** 2)) * 6.0)
            self.hud.set_level(lvl)
            return
        if self._enrolling:            # voice-profile recording (Settings)
            self._enroll_frames.append(indata[:, 0].copy())
            return
        # Auto-Dictate: armed box focused → segment utterances locally.
        # Everything else falls through and the frame is DISCARDED on arrival.
        if (self._auto_on and self._armed and not self._paused
                and self.state == IDLE):
            utt = self._auto_ep.feed(indata[:, 0].copy())
            if self._auto_ep.speaking != self._auto_speaking:
                self._auto_speaking = self._auto_ep.speaking
                self._chip.show("capturing" if self._auto_speaking else "armed")
            if utt is not None:
                self._auto_q.put((utt, self._armed_id, self._auto_ep.last_span))

    def _open_stream(self) -> None:
        """Open the mic ONCE at startup and keep it warm. Opening a PortAudio
        stream per-tap cost 130–840 ms here (a laggy, clipped mic-on); a warm
        stream makes starting a capture instant."""
        try:
            device = _resolve_input_device(
                self.cfg.get("audio", {}).get("input_device", "default"))
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                          dtype="float32", callback=self._on_audio,
                                          device=device)
            self._stream.start()
            _LOG.info("mic stream opened (device=%s, rate=%d)", device, SAMPLE_RATE)
        except Exception as e:
            self._stream = None
            _LOG.exception("could not open mic")
            print(f"[audio] could not open mic: {e}")

    def _close_stream(self) -> None:
        try:
            if self._stream:
                self._stream.stop(); self._stream.close()
        finally:
            self._stream = None

    def _play(self, kind: str) -> None:
        """Play a cue unless sounds are disabled in settings."""
        if self._sounds_on:
            os_back.play(kind)

    # ───────────── recording lifecycle ─────────────
    def _begin(self, mode: str) -> None:
        with self._lock:
            if self.state != IDLE:
                return
            self.state = mode
        if self._stream is None:           # mic never opened at startup
            self.state = IDLE
            self._play("error")
            _LOG.warning("begin(%s): no mic stream", mode)
            print("[audio] no mic stream")
            return
        # A manual capture always preempts Auto-Dictate: drop any half-heard
        # auto utterance so it can't fire mid-manual-recording.
        self._auto_ep.reset()
        self._auto_speaking = False
        # Immediate feedback FIRST — beep + start capturing from the warm mic +
        # show the HUD — before the slower window/selection probing, so the sound
        # lands the instant you tap rather than a beat later.
        self._play("start")
        self._frames = []                  # collect from the warm stream — instant
        self._capturing = True
        self.hud.show()
        self._app = os_back.frontmost_app()
        self._dstream = None
        if mode == DICTATE and self._stream_on:
            # clean pause-delimited chunks in the background as you speak
            self._dstream = stream_mod.DictationStream(
                self._frames_snapshot, self._stream_transcribe,
                self._stream_clean, log=_LOG.info)
            self._dstream.start()
        if mode == COMMAND:
            # selection → edit it; capture screen context (Phase 2) off-thread
            self._sel, self._prev_clip = os_back.copy_selection()
            self._ctx = ""
            if self._sel is None:
                def grab():
                    raw = os_back.read_window_context()
                    self._ctx = self.ctx_log.stitch(raw, self._app[1], time.time())
                threading.Thread(target=grab, daemon=True).start()

    def _end(self) -> None:
        with self._lock:
            mode = self.state
            if mode == IDLE:
                return
            self.state = IDLE
        self._capturing = False
        audio = (np.concatenate(self._frames) if self._frames
                 else np.zeros(0, dtype="float32"))
        self._frames = []
        self._play("stop")
        self.hud.hide()
        dstream, self._dstream = self._dstream, None
        if mode == DICTATE and dstream is not None:
            threading.Thread(target=self._process_stream, args=(dstream, audio),
                             daemon=True).start()
        else:
            threading.Thread(target=self._process, args=(mode, audio), daemon=True).start()

    # ───────────── transcribe → write/paste ─────────────
    def _stt_key(self) -> str:
        t = self.cfg["transcription"]
        return core._resolve_api_key(t["api_key_env"], t["api_key_file"])

    def _process(self, mode: str, audio: np.ndarray) -> None:
        try:
            if not core.contains_speech(audio):
                _LOG.info("process(%s): no speech (%.1fs)", mode, len(audio) / SAMPLE_RATE)
                return
            t = self.cfg["transcription"]
            text = (core.transcribe_remote(audio, t["base_url"], t["model"],
                                           self._stt_key(), t.get("language", ""),
                                           t.get("vocabulary", "")).get("text") or "").strip()
            text = core.collapse_repeats(text)
            text = autod.apply_fixers(text, self._fixers)
            _LOG.info("process(%s): transcribed %d chars", mode, len(text))
            if mode == DICTATE:
                if not core.has_lexical_content(text) or core.is_hallucination(text):
                    return
                if self._clean_on:             # inline cleanup (stream=off): one pass at stop
                    cleaned = self._stream_clean(text, "")
                    if cleaned:
                        text = cleaned
                text = core.start_case(text)   # fresh paste → capital first letter, no lead space
                self._emit(text)
                return
            # command / write mode
            if (not core.has_lexical_content(text)
                    or core.is_hallucination(text, strict=True)):
                return
            f = self.cfg["formatting"]
            url = f["command_base_url"]            # cloud path (base_url set)
            model = f["command_model"]
            kenv, kfile = f["api_key_env"], f["api_key_file"]
            if self._sel is not None:
                result = core.apply_command(text, self._sel, url, model,
                                            url, kenv, kfile)
            else:
                ctx = self._ctx[:12000] if core.wants_context(text) else ""
                email = core.is_email_context(*self._app)
                result = core.generate_text(text, url, model, "", email=email,
                                            base_url=url, api_key_env=kenv,
                                            api_key_file=kfile, context=ctx)
            if result:
                self._emit(result, restore=self._prev_clip)
        except Exception as e:
            self._play("error")
            _LOG.exception("process(%s) error", mode)
            print(f"[process] error: {e}")
        finally:
            self._sel = None

    def _emit(self, text: str, restore: str | None = None) -> None:
        os_back.clipboard_set(text)
        os_back.paste_into_focused_app()
        if restore is not None:
            def _restore():
                time.sleep(0.6); os_back.clipboard_set(restore)
            threading.Thread(target=_restore, daemon=True).start()

    # ───────────── streaming dictation (clean-during-pauses) ─────────────
    def _frames_snapshot(self) -> np.ndarray:
        """A copy of all mic frames captured so far this dictation (for the
        stream poller to scan for pauses). Snapshots the list defensively — the
        audio callback appends concurrently."""
        fr = self._frames
        n = len(fr)
        if not n:
            return np.zeros(0, dtype="float32")
        return np.concatenate(fr[:n])

    def _stream_transcribe(self, audio: np.ndarray) -> str:
        """Transcribe one chunk to raw text (fixers applied), '' if empty/noise."""
        if not core.contains_speech(audio):
            return ""
        t = self.cfg["transcription"]
        text = (core.transcribe_remote(audio, t["base_url"], t["model"],
                                       self._stt_key(), t.get("language", ""),
                                       t.get("vocabulary", "")).get("text") or "").strip()
        text = core.collapse_repeats(text)
        text = autod.apply_fixers(text, self._fixers)
        if not core.has_lexical_content(text) or core.is_hallucination(text):
            return ""
        return text

    def _stream_clean(self, raw: str, prev: str) -> str:
        """Clean one chunk into written text, continuing from `prev`. Uses the
        fast [dictation] model (cleanup is easy; the tail's latency is felt),
        falling back to the heavier [formatting] Write model / endpoint."""
        f = self.cfg["formatting"]
        model = (self.cfg.get("dictation", {}).get("model") or "").strip() \
            or f["command_model"]
        return core.clean_dictation(
            raw, f["command_base_url"], model, prev=prev,
            tone=self._tone, base_url=f["command_base_url"],
            api_key_env=f["api_key_env"], api_key_file=f["api_key_file"])

    def _process_stream(self, dstream, audio: np.ndarray) -> None:
        """Finish a streamed dictation: drain the already-cleaned chunks, process
        the final tail, then paste the assembled text once."""
        try:
            t0 = time.time()
            text = dstream.finish(audio)
            if not text or not core.has_lexical_content(text) or core.is_hallucination(text):
                _LOG.info("stream: nothing to emit (%.1fs audio)", len(audio) / SAMPLE_RATE)
                return
            text = core.start_case(text)
            _LOG.info("stream: emit %d chars (finish %dms)",
                      len(text), int((time.time() - t0) * 1000))
            self._emit(text)
        except Exception as e:
            self._play("error")
            _LOG.exception("stream: process error")
            print(f"[stream] error: {e}")

    # ───────────── Auto-Dictate (focused text box = live mic) ─────────────
    def _on_focus_change(self, editable: bool, cid, desc: str) -> None:
        """FocusWatcher callback: keyboard focus moved. Arm on an editable box,
        disarm (and drop any half-heard utterance) the instant it leaves."""
        if self._force_arm:            # onboarding try-it box isn't UIA-editable
            editable, cid, desc = True, ("__onboarding__",), "onboarding practice"
        self._armed = bool(editable)
        self._armed_id = cid
        self._auto_ep.reset()
        self._auto_speaking = False
        _LOG.info("auto: %s — %s", "ARMED" if editable else "disarmed", desc)
        if self._auto_on and not self._paused:
            self._chip.show("armed") if editable else self._chip.hide()

    def _auto_loop(self) -> None:
        """Worker: one finished utterance at a time, in spoken order."""
        try:
            import comtypes  # UIA (screen context for write commands) needs COM
            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            utt, cid, span = self._auto_q.get()
            try:
                self._auto_process(utt, cid, span)
            except Exception:
                self._play("error")
                _LOG.exception("auto: process error")

    def _auto_process(self, audio: np.ndarray, cid, span) -> None:
        if not self._auto_on or self._paused:
            return
        t0 = time.time()
        # speech check on head + middle slices (not the full clip — that scan
        # is O(duration)). Head alone missed speech when background noise armed
        # the endpointer before the user started talking (25s clip dropped).
        win = int(3.0 * SAMPLE_RATE)
        mid = max(0, audio.size // 2 - win // 2)
        if not (core.contains_speech(audio[:win])
                or (audio.size > win
                    and core.contains_speech(audio[mid:mid + win]))):
            _LOG.info("auto: no speech (%.1fs)", len(audio) / SAMPLE_RATE)
            return
        # the machine hearing itself? (video/music through the speakers)
        echo, corr = self._loopback.is_self_audio(audio, span[0], span[1],
                                                  self._echo_corr)
        if echo:
            _LOG.info("auto: DROPPED as speaker echo (corr %.2f, %.1fs)",
                      corr, len(audio) / SAMPLE_RATE)
            return
        ok, score = self._speaker.accept(audio)      # LOCAL — nothing uploaded
        t1 = time.time()
        if not ok:
            _LOG.info("auto: DROPPED by voice filter (score %.3f, %.1fs)",
                      score, len(audio) / SAMPLE_RATE)
            return
        self._speaker.maybe_adapt(score)             # profile tracks the user
        t = self.cfg["transcription"]
        text = (core.transcribe_remote(audio, t["base_url"], t["model"],
                                       self._stt_key(), t.get("language", ""),
                                       t.get("vocabulary", "")).get("text") or "").strip()
        # glossary-echo guard: a long clip whose whole "transcript" is one of
        # the personal values = Whisper parroting the vocabulary prompt.
        # Retranscribe with no bias to get the real words.
        if (len(audio) / SAMPLE_RATE > 4.0
                and autod.is_prompt_echo(text, self._personal)):
            _LOG.info("auto: glossary echo suspected (%r) — retrying unbiased",
                      text[:40])
            text = (core.transcribe_remote(audio, t["base_url"], t["model"],
                                           self._stt_key(), t.get("language", ""),
                                           "").get("text") or "").strip()
        t2 = time.time()
        _LOG.info("auto: voice ok (%.3f, echo %.2f) %.1fs clip — gate %dms, stt %dms",
                  score, corr, len(audio) / SAMPLE_RATE,
                  int((t1 - t0) * 1000), int((t2 - t1) * 1000))
        text = core.collapse_repeats(text)
        if not core.has_lexical_content(text) or core.is_hallucination(text):
            return
        if autod.is_noise(text):                     # throat-clear → "Ahem."
            _LOG.info("auto: noise dropped (%r)", text[:30])
            return
        text = autod.apply_fixers(text, self._fixers)
        # the box may have changed while we transcribed — never type into the
        # wrong place, and never fight a manual capture
        if not self._armed or cid != self._armed_id or self.state != IDLE:
            _LOG.info("auto: focus moved — dropping %r", text[:60])
            return
        if len(self._auto_last) > 64:
            self._auto_last.clear()
        # per-box session record: everything we typed (for precise voice
        # edits) + the most recent utterance (for "scratch that")
        rec = self._auto_last.setdefault(cid, {"text": "", "last": ""})
        sp = autod.special_of(text)
        if sp == "scratch":
            if rec["last"]:
                os_back.send_backspaces(len(rec["last"]))
                rec["text"] = rec["text"][:-len(rec["last"])]
                _LOG.info("auto: scratch that (%d chars)", len(rec["last"]))
                rec["last"] = ""
            self._play("tick")
            return
        if sp == "send":
            in_terminal = (isinstance(cid, tuple) and len(cid) == 2
                           and cid[1] == "terminal")
            if in_terminal and not self._send_in_terminal:
                # Enter at a shell prompt EXECUTES the line — never by voice
                self._play("cancel")
                _LOG.info("auto: 'send it' blocked in terminal")
                return
            os_back.send_enter()
            rec["text"], rec["last"] = "", ""        # box is empty again
            self._play("tick")
            _LOG.info("auto: send it")
            return
        if autod.is_clear_all(text):                 # "delete everything" / "start over"
            n = len(rec["text"])
            if n > 0:
                os_back.send_backspaces(n)
                rec["text"], rec["last"] = "", ""
                self._play("tick")
                _LOG.info("auto: cleared all (%d chars)", n)
            else:
                self._play("cancel")                 # nothing we typed → don't guess
                _LOG.info("auto: clear-all but nothing tracked")
            return
        deletion = autod.delete_of(text)             # "remove the last 3 words"
        if deletion is not None:
            unit, count = deletion
            n = autod.chars_to_delete(rec["text"], unit, count)
            if n > 0:
                os_back.send_backspaces(n)
                rec["text"] = rec["text"][:-n]
                rec["last"] = ""
                self._play("tick")
                _LOG.info("auto: removed last %d %s(s) — %d chars", count, unit, n)
            elif unit == "word":
                # we didn't type this text; Ctrl+Backspace works in most apps
                os_back.send_ctrl_backspaces(count)
                self._play("tick")
                _LOG.info("auto: ctrl-backspace ×%d (untracked text)", count)
            else:
                self._play("cancel")
                _LOG.info("auto: no tracked text to remove a %s from", unit)
            return
        snip = autod.snippet_of(text, self._personal)
        if snip is not None:                          # "type my email"
            _LOG.info("auto: snippet → %d chars", len(snip))
            text = snip
        else:
            target = autod.action_of(text)
            if target is not None:                    # "switch to slack" / "open chrome"
                done = os_back.activate_window(target) or os_back.launch_app(target)
                self._play("tick" if done else "error")
                _LOG.info("auto: action '%s' → %s", target, "ok" if done else "FAILED")
                return
            if autod.is_command(text):                # "write a reply saying ..."
                text = self._auto_command(text)
                if not text:
                    self._play("error")
                    return
            elif autod.is_maybe_command(text):        # "add a paragraph of ..."
                drafted = self._auto_command(text, maybe=True)
                if drafted == "":
                    self._play("error")
                    return
                if drafted is not None:               # None = model said DICTATION
                    text = drafted
        if snip is None:                              # capitalize dictation/drafts, not an email snippet
            text = core.start_case(text, rec["text"])  # new sentence → capital; mid-sentence stays lowercase
        out = text
        if rec["text"] and not rec["text"][-1].isspace():
            out = " " + text                          # utterances flow as prose
        self._emit(out)
        rec["text"] = (rec["text"] + out)[-20000:]    # cap a marathon session
        rec["last"] = out
        self._play("tick")
        _LOG.info("auto: typed %d chars", len(out))

    def _auto_command(self, instruction: str, maybe: bool = False):
        """Hands-free write command: draft with the writing model, using screen
        context when the instruction implies it ("reply saying...").
        maybe=True: the utterance only LOOKS command-ish — the model may answer
        DICTATION, in which case we return None (caller types it verbatim)."""
        f = self.cfg["formatting"]
        url, model = f["command_base_url"], f["command_model"]
        app = os_back.frontmost_app()
        ctx = ""
        if maybe or core.wants_context(instruction):
            try:
                raw = os_back.read_window_context()
                ctx = self.ctx_log.stitch(raw, app[1], time.time())[:12000]
            except Exception:
                _LOG.exception("auto: context grab failed")
        try:
            out = core.generate_text(instruction, url, model, "",
                                     email=core.is_email_context(*app),
                                     base_url=url,
                                     api_key_env=f["api_key_env"],
                                     api_key_file=f["api_key_file"],
                                     context=ctx, maybe_dictation=maybe)
            if maybe and (out or "").strip().strip('."\'').upper() == "DICTATION":
                _LOG.info("auto: model ruled dictation — typing verbatim")
                return None
            _LOG.info("auto: write command%s → %d chars (ctx %d)",
                      " (maybe)" if maybe else "", len(out or ""), len(ctx))
            return out or ""
        except Exception:
            _LOG.exception("auto: write command failed")
            return ""

    def _rebuild_personal(self) -> None:
        """Recompute snippets/fixers/vocabulary from cfg['personal']."""
        cfg = self.cfg
        self._personal = {str(k).lower(): str(v)
                          for k, v in (cfg.get("personal") or {}).items()
                          if isinstance(v, (str, int)) and str(v).strip()}
        self._fixers = autod.build_fixers(self._personal,
                                          cfg.get("replacements") or {})
        # NOTHING from [personal] is fed to Whisper as a vocabulary prompt.
        # Prompt biasing makes Whisper ECHO a glossary item as the "transcript"
        # of unrelated speech: first seen with the email (8s of speech → just
        # the address), then with the NAME — parroted whole, and worse, spliced
        # into the MIDDLE of a real sentence (which the whole-transcript echo
        # guard can't catch). Spoken-form fixers handle these instead. Only an
        # explicit [transcription] vocabulary the user set is passed through.
        cfg["transcription"]["vocabulary"] = self._base_vocab

    def apply_personal(self, name: str, email: str) -> None:
        """Update the user's details live (Settings) and persist them."""
        pe = self.cfg.setdefault("personal", {})
        pe["name"], pe["email"] = (name or "").strip(), (email or "").strip()
        self._rebuild_personal()
        try:
            self.save_config()
        except Exception:
            _LOG.exception("personal: save failed")

    def set_groq_key(self, key: str) -> bool:
        """Persist the Groq API key to Windows Credential Manager (the resolver's
        first lookup) and make it live this session. The key is NEVER logged."""
        key = (key or "").strip()
        if not key:
            return False
        os.environ["GROQ_API_KEY"] = key
        try:
            import keyring
            keyring.set_password("voice-to-text", "groq_key", key)
        except Exception:
            _LOG.exception("groq key: credential-store save failed")
        _LOG.info("groq key: saved (%d chars)", len(key))   # length only, never the key
        return True

    def has_groq_key(self) -> bool:
        t = self.cfg["transcription"]
        return bool(core._resolve_api_key(t["api_key_env"], t["api_key_file"]))

    def set_force_arm(self, on: bool) -> None:
        """Onboarding try-it: force our own (UIA-invisible) practice box to count
        as an armed editable box, so Auto-Dictate can type into it too."""
        self._force_arm = bool(on)
        if on:
            self._on_focus_change(True, ("__onboarding__",), "onboarding practice")
        else:
            self._auto_ep.reset()
            self._auto_speaking = False

    def auto_dictate_on(self) -> bool:
        return self._auto_on

    def speaker_enrolled(self) -> bool:
        return self._speaker.enrolled()

    def set_auto_dictate(self, on: bool) -> bool:
        """Enable/disable Auto-Dictate. Enabling requires a voice profile."""
        on = bool(on)
        if on and not self._speaker.enrolled():
            _LOG.info("auto: enable refused — no voice profile yet")
            self._sync_ui()
            return False
        self._auto_on = on
        self.cfg.setdefault("auto_dictate", {})["enabled"] = on
        if on:
            self._focus.start()
            self._focus.poke()
            self._loopback.start()
            threading.Thread(target=self._speaker.preload, daemon=True).start()
        else:
            self._chip.hide()
            self._auto_ep.reset()
            self._auto_speaking = False
        try:
            self.save_config()
        except Exception:
            _LOG.exception("auto: save_config failed")
        _LOG.info("auto: %s", "ENABLED" if on else "disabled")
        self._sync_ui()
        return True

    # ── voice enrollment (driven by the Settings window) ──
    ENROLL_SECONDS = 30

    def begin_enrollment(self) -> bool:
        if self._stream is None or self._enrolling or self.state != IDLE:
            return False
        self._enroll_frames = []
        self._enrolling = True
        _LOG.info("enroll: recording started")
        return True

    def cancel_enrollment(self) -> None:
        self._enrolling = False
        self._enroll_frames = []

    def finish_enrollment(self, done) -> None:
        """Stop recording and build the voice profile off-thread.
        done(ok: bool, message: str) is called from that worker thread."""
        self._enrolling = False
        frames, self._enroll_frames = self._enroll_frames, []

        def work():
            try:
                audio = (np.concatenate(frames) if frames
                         else np.zeros(0, dtype="float32"))
                if audio.size < 10 * SAMPLE_RATE:
                    done(False, "Recording was too short — try again.")
                    return
                if not core.contains_speech(audio):
                    done(False, "Didn't hear speech — try again, closer to the mic.")
                    return
                self._speaker.enroll(audio)          # first call loads the model
                done(True, "Voice enrolled ✓")
            except Exception as e:
                _LOG.exception("enroll: failed")
                done(False, f"Enrollment failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    # ───────────── hotkeys (tap detection) ─────────────
    def _toggle(self, mode: str) -> None:
        if self._paused:                  # hotkeys off until resumed
            return
        if self.state == IDLE:
            self._begin(mode)
        elif self.state == mode:
            self._end()
        # different mode while recording → ignore

    def _hotkey_toggle_auto(self) -> None:
        """Master switch (the toggle hotkey, default tilde): flip Auto-Dictate
        on/off with an audible cue + a chip toast so the new state is obvious.
        Enabling needs a voice profile — if there's none, play the error cue and
        say so rather than silently doing nothing."""
        target = not self._auto_on
        if target and not self._speaker.enrolled():
            self._play("error")
            self._chip.toast("Enroll your voice first", self._chip.GRAY)
            return
        self.set_auto_dictate(target)
        self._play("start" if self._auto_on else "stop")
        self._chip.toast(
            "Auto-Dictate ON" if self._auto_on else "Auto-Dictate OFF",
            self._chip.GREEN if self._auto_on else self._chip.GRAY)

    def _press_trigger(self, tok) -> None:
        st = self._trig.get(tok)
        if st is not None and not st["down"]:
            st.update(down=True, t=time.time(), mod=False)

    def _release_trigger(self, tok) -> None:
        st = self._trig.get(tok)
        if not st or not st["down"]:
            return
        held = time.time() - st["t"]
        st["down"] = False
        if not st["mod"] and held < 0.6:          # a genuine tap
            action = self._trig_action.get(tok)
            # off the hook/callback thread so the keyboard hook stays snappy
            if action == "toggle_auto":
                threading.Thread(target=self._hotkey_toggle_auto, daemon=True).start()
            elif action == DICTATE:
                threading.Thread(target=self._toggle, args=(DICTATE,), daemon=True).start()

    def _on_press(self, key) -> None:
        tok = _key_token(key)
        if tok in self._trig:
            self._press_trigger(tok)
        else:
            for st in self._trig.values():       # a real key during a hold = modifier use
                if st["down"]:
                    st["mod"] = True

    def _on_release(self, key) -> None:
        self._release_trigger(_key_token(key))

    def _win32_filter(self, msg, data) -> None:
        """Win32 low-level keyboard filter. For SUPPRESSED printable triggers
        (e.g. the tilde), suppress_event() also stops the key reaching
        on_press/on_release — so we run tap-detection HERE and then suppress, so
        the key never types its character. A single VK can carry two bindings
        that differ by Shift (dictate=tilde, command=shift+tilde); we pick the
        one whose Shift requirement matches the live Shift state, recording the
        choice on key-down so the matching key-up releases the same trigger even
        if Shift was let go mid-tap. Non-suppressed keys pass straight through to
        on_press/on_release as usual."""
        if self._listener is None:
            return
        vk = data.vkCode
        bindings = self._suppress_vk.get(vk)
        if not bindings:
            return                                # not suppressed — normal path
        if msg in (0x0100, 0x0104):               # WM_KEYDOWN / WM_SYSKEYDOWN
            shift = _shift_is_down()
            tok = next((t for sr, t in bindings if sr == shift), None)
            if tok is None:                       # no exact match → unshifted fallback
                tok = next((t for sr, t in bindings if not sr), None)
            if tok is None:
                return                            # nothing bound for this state → let it type
            self._vk_press_tok[vk] = tok
            self._press_trigger(tok)
            self._listener.suppress_event()
        elif msg in (0x0101, 0x0105):             # WM_KEYUP / WM_SYSKEYUP
            tok = self._vk_press_tok.pop(vk, None)
            if tok is None:
                return                            # we didn't handle the down → let it pass
            self._release_trigger(tok)
            self._listener.suppress_event()

    # ───────────── control API (used by the GUI + tray) ─────────────
    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if self._paused:                # pause silences Auto-Dictate too
            self._chip.hide()
            self._auto_ep.reset()
            self._auto_speaking = False
        elif self._auto_on and self._armed:
            self._chip.show("armed")
        self._sync_ui()

    def set_sounds(self, on: bool) -> None:
        self._sounds_on = bool(on)

    def clear_context(self) -> None:
        self.ctx_log.clear()

    def autostart_enabled(self) -> bool:
        return os_back.autostart_enabled()

    def set_autostart(self, enable: bool) -> None:
        os_back.set_autostart(bool(enable), sys.executable,
                              os.path.abspath(__file__))
        self._sync_ui()

    def _make_listener(self):
        return keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release,
            win32_event_filter=self._win32_filter)

    def _restart_listener(self) -> None:
        try:
            if self._listener:
                self._listener.stop()
        except Exception:
            pass
        self._listener = self._make_listener()
        self._listener.start()

    def _bind_triggers(self, dictate_key: str, toggle_auto_key: str) -> None:
        """(Re)build the tap-detection + suppression tables from the two hotkey
        names: dictate (manual long-form dictation) and toggle_auto (the master
        switch for Auto-Dictate). A printable trigger (the tilde) is suppressed so
        it never types its character; a lone modifier/function key (Right Ctrl,
        F9) passes through the normal press/release path and stays usable as a
        real modifier when combined with another key."""
        self._k_dictate, dvk, dshift = _resolve_trigger(dictate_key, keyboard.Key.ctrl_r)
        self._k_toggle,  tvk, tshift = _resolve_trigger(toggle_auto_key, keyboard.Key.f9)
        # token -> action, consulted on a genuine tap in _release_trigger
        self._trig_action = {self._k_dictate: DICTATE,
                             self._k_toggle: "toggle_auto"}
        self._suppress_vk: dict = {}     # vk -> [(shift_required, token), ...]
        if dvk is not None:
            self._suppress_vk.setdefault(dvk, []).append((dshift, self._k_dictate))
        if tvk is not None:
            self._suppress_vk.setdefault(tvk, []).append((tshift, self._k_toggle))
        self._vk_press_tok: dict = {}    # vk -> token chosen at key-down
        # tap detection per trigger token: {token: {"down":bool,"t":float,"mod":bool}}
        self._trig = {self._k_dictate: {"down": False, "t": 0.0, "mod": False},
                      self._k_toggle:  {"down": False, "t": 0.0, "mod": False}}

    def apply_hotkeys(self, dictate_key: str, toggle_auto_key: str) -> None:
        """Rebind the dictate/toggle-auto hotkeys live — no restart."""
        self.cfg["hotkey"]["dictate_key"] = dictate_key
        self.cfg["hotkey"]["toggle_auto_key"] = toggle_auto_key
        self._bind_triggers(dictate_key, toggle_auto_key)
        self._restart_listener()
        self._sync_ui()

    def apply_input_device(self, spec: str) -> None:
        """Switch the microphone live by reopening the warm stream."""
        self.cfg.setdefault("audio", {})["input_device"] = spec
        self._close_stream()
        self._open_stream()

    def refresh_devices(self) -> bool:
        """Re-scan audio devices. PortAudio caches its device list when the app
        starts, so a mic connected LATER (e.g. a Bluetooth headset) is invisible
        until the engine is re-initialized. Closes the warm mic, re-inits, and
        reopens on the current device (resolved by name, so a changed index is
        fine). Returns False if busy (a capture in progress) — nothing touched."""
        if self._capturing or self.state != IDLE or self._enrolling:
            _LOG.info("audio: refresh skipped (busy)")
            return False
        self._close_stream()
        try:
            sd._terminate(); sd._initialize()
            _LOG.info("audio: device list refreshed")
        except Exception:
            _LOG.exception("audio: reinit failed")
        self._open_stream()
        return True

    def save_config(self) -> None:
        """Persist the editable settings to %APPDATA%\\Voice-To-Text\\config.toml.
        Only the user-facing keys are written; everything else deep-merges from
        DEFAULT_CFG at load, so the file stays small and readable."""
        def q(s):  # TOML basic-string quote
            return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
        c = self.cfg
        hk, au, so = c.get("hotkey", {}), c.get("audio", {}), c.get("sounds", {})
        tr, fo = c.get("transcription", {}), c.get("formatting", {})
        ad = c.get("auto_dictate", {})
        di = c.get("dictation", {})
        lines = [
            "# Voice-To-Text (Windows) config — written by the Settings window.",
            "# Other keys (API endpoints/keys) fall back to built-in defaults.",
            "",
            "[hotkey]",
            f"dictate_key = {q(hk.get('dictate_key', 'ctrl_r'))}",
            f"toggle_auto_key = {q(hk.get('toggle_auto_key', 'tilde'))}",
            "",
            "[audio]",
            f"input_device = {q(au.get('input_device', 'default'))}",
            "",
            "[sounds]",
            f"enabled = {'true' if so.get('enabled', True) else 'false'}",
            "",
            "[transcription]",
            f"model = {q(tr.get('model', 'whisper-large-v3'))}",
            "",
            "[formatting]",
            f"command_model = {q(fo.get('command_model', 'openai/gpt-oss-120b'))}",
            "",
            "[dictation]",
            f"clean = {'true' if di.get('clean', True) else 'false'}",
            f"stream = {'true' if di.get('stream', True) else 'false'}",
            f"model = {q(di.get('model', 'llama-3.1-8b-instant'))}",
            f"tone = {q(di.get('tone', ''))}",
            "",
            "[auto_dictate]",
            f"enabled = {'true' if ad.get('enabled', False) else 'false'}",
            f"similarity = {float(ad.get('similarity', 0.60))}",
            f"silence_ms = {int(ad.get('silence_ms', 700))}",
            f"min_speech_ms = {int(ad.get('min_speech_ms', 180))}",
            f"start_rms = {float(ad.get('start_rms', 0.014))}",
            f"end_rms = {float(ad.get('end_rms', 0.008))}",
            f"echo_corr = {float(ad.get('echo_corr', 0.55))}",
            f"adapt = {'true' if ad.get('adapt', True) else 'false'}",
            f"send_in_terminal = {'true' if ad.get('send_in_terminal', False) else 'false'}",
            "exclude_apps = [" + ", ".join(q(x) for x in (ad.get('exclude_apps') or [])) + "]",
            "",
            "[personal]",
            *[f"{k} = {q(v)}" for k, v in (c.get('personal') or {}).items()
              if str(v).strip()],
            "",
            "[replacements]",
            *[f"{q(k)} = {q(v)}" for k, v in (c.get('replacements') or {}).items()],
            "",
        ]
        path = os.path.join(os.environ.get("APPDATA", ""), APP_NAME, "config.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _sync_ui(self) -> None:
        """Push state changes to the tray menu + the GUI window (thread-safe)."""
        try:
            if self._icon:
                self._icon.update_menu()
        except Exception:
            pass
        try:
            if self._gui:
                self._gui.notify_state_changed()
        except Exception:
            pass

    def quit(self) -> None:
        self._quit(self._icon, None)

    # ───────────── tray ─────────────
    def _make_icon_image(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(245, 177, 92, 255))
        d.rounded_rectangle((27, 18, 37, 40), radius=5, fill=(26, 20, 10, 255))
        d.rectangle((31, 40, 33, 48), fill=(26, 20, 10, 255))
        return img

    def run(self, start_hidden: bool = False) -> None:
        self._open_stream()            # warm the mic up front (instant captures)
        os_back._load_sounds()         # preload cues so the first start-beep isn't
                                       # lazy-loaded on the hot path (occasional miss)
        if self._auto_on:
            if self._speaker.enrolled():
                self._focus.start()
                self._loopback.start()
                # load the voice model now, not on the first utterance (that
                # lazy load showed up as ~3.7 s of paste lag)
                threading.Thread(target=self._speaker.preload,
                                 daemon=True).start()
            else:                      # profile file gone — never arm blind
                self._auto_on = False
                _LOG.warning("auto: enabled in config but no voice profile — off")
        self._listener = self._make_listener()
        self._listener.start()
        import pystray
        self._icon = pystray.Icon(
            APP_NAME, self._make_icon_image(), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Open Voice-To-Text", self._tray_open, default=True),
                pystray.MenuItem("Settings…", self._tray_settings),
                pystray.MenuItem("Welcome guide…", self._tray_guide),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda i: f"Dictate: {self.cfg['hotkey']['dictate_key']}  ·  "
                              f"Auto toggle: {self.cfg['hotkey']['toggle_auto_key']}",
                    None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Auto-Dictate (text box = live mic)",
                                 self._toggle_auto,
                                 checked=lambda i: self._auto_on),
                pystray.MenuItem("Pause hotkeys", self._toggle_pause,
                                 checked=lambda i: self._paused),
                pystray.MenuItem("Clear thread context",
                                 lambda i, it: self.ctx_log.clear()),
                pystray.MenuItem("Start at login",
                                 self._toggle_autostart,
                                 checked=lambda i: os_back.autostart_enabled()),
                pystray.MenuItem("Restart", lambda i, it: self.restart()),
                pystray.MenuItem("Quit", self._quit),
            ),
        )
        # Tray runs on a background thread; the desktop window owns the main thread
        # (tkinter must be on the main thread). If the GUI can't start, fall back
        # to tray-only and keep the process alive.
        threading.Thread(target=self._icon.run, daemon=True).start()
        try:
            from gui import AppWindow
            self._gui = AppWindow(self)
            self._gui.run(start_hidden=start_hidden)
        except Exception as e:
            print(f"[gui] tray-only mode: {e}")
            self._gui = None
            threading.Event().wait()

    def _tray_open(self, icon, item) -> None:
        if self._gui:
            self._gui.show()

    def _tray_settings(self, icon, item) -> None:
        if self._gui:
            self._gui.show_settings()

    def _tray_guide(self, icon, item) -> None:
        if self._gui:
            self._gui.show_onboarding()

    def _toggle_pause(self, icon, item) -> None:
        self.set_paused(not self._paused)

    def _toggle_auto(self, icon, item) -> None:
        if not self._auto_on and not self._speaker.enrolled():
            self._play("error")        # needs enrollment first — open Settings
            if self._gui:
                self._gui.show_settings()
            return
        self.set_auto_dictate(not self._auto_on)

    def _toggle_autostart(self, icon, item) -> None:
        os_back.set_autostart(not os_back.autostart_enabled(),
                              sys.executable, os.path.abspath(__file__))
        self._sync_ui()

    def restart(self) -> None:
        import subprocess
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        script = os.path.abspath(__file__)
        try:
            DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            subprocess.Popen([pyw, script], creationflags=DETACHED, close_fds=True)
        except Exception as e:
            print(f"[restart] {e}")
            return
        self._quit(self._icon, None)

    def _quit(self, icon, item) -> None:
        try:
            self._close_stream()
            if self._icon:
                self._icon.stop()
        finally:
            os._exit(0)


def _crashlog_path() -> str:
    return os.path.join(os.environ.get("TEMP", "."), "vtt_crash.log")


def _log_path() -> str:
    base = os.path.join(os.environ.get("APPDATA") or os.environ.get("TEMP", "."), APP_NAME)
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.environ.get("TEMP", ".")
    return os.path.join(base, "voice-to-text.log")


class _StreamToLog:
    """File-like that mirrors writes into the log (captures stray print())."""

    def __init__(self, level: int, orig) -> None:
        self._level, self._orig, self._buf = level, orig, ""

    def write(self, s: str) -> None:
        try:
            if self._orig:
                self._orig.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                _LOG.log(self._level, line.rstrip())

    def flush(self) -> None:
        try:
            if self._orig:
                self._orig.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False


def _setup_logging() -> None:
    """Log everything to a rotating file so a windowed (no-console) build can be
    diagnosed after the fact: all stdout/stderr (incl. stray print()), plus
    uncaught exceptions on the main thread AND on worker threads."""
    if getattr(_setup_logging, "_done", False):
        return
    _setup_logging._done = True
    _LOG.setLevel(logging.INFO)
    try:
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(_log_path(), maxBytes=1_000_000, backupCount=3,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        _LOG.addHandler(h)
    except Exception:
        pass
    sys.stdout = _StreamToLog(logging.INFO, sys.__stdout__)
    sys.stderr = _StreamToLog(logging.ERROR, sys.__stderr__)

    def _hook(exc_type, exc, tb) -> None:
        _LOG.error("uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
    try:
        def _thook(args) -> None:
            _LOG.error("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                       exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = _thook
    except Exception:
        pass
    _LOG.info("=== Voice-To-Text starting (frozen=%s) ===", getattr(sys, "frozen", False))
    _LOG.info("log file: %s", _log_path())


def main() -> None:
    if "--selftest" in sys.argv[1:]:
        # Verify the frozen bundle's heavy/native deps import — catches PyInstaller
        # under-collection (e.g. numpy._core._multiarray_umath). Touches no mic,
        # tray, or GUI; prints "selftest ok" and exits 0 so a build can be smoke-
        # tested headlessly.
        import numpy, sounddevice                       # noqa: F401
        from numpy._core import _multiarray_umath       # noqa: F401
        import win32clipboard, uiautomation, PIL.Image  # noqa: F401
        # Auto-Dictate deps: heavy natives + the resemblyzer model weights —
        # instantiating VoiceEncoder proves pretrained.pt shipped in the bundle
        import torch, librosa, webrtcvad, pyaudiowpatch  # noqa: F401
        from resemblyzer import VoiceEncoder
        VoiceEncoder("cpu")
        os_back._load_sounds()                          # verify bundled cue WAVs
        cues = sorted(os_back._SOUND_CACHE.keys())
        start_wav = os_back._SOUND_CACHE.get("start", b"")[:4] == b"RIFF"
        # Windowed exe has no console, so write a marker file instead of printing.
        try:
            marker = os.path.join(os.environ.get("TEMP", "."), "vtt_selftest.txt")
            with open(marker, "w", encoding="utf-8") as f:
                f.write(f"selftest ok numpy={numpy.__version__} "
                        f"cues={cues} start_wav={start_wav}\n")
        except Exception:
            pass
        return
    _setup_logging()
    # Crash logging: faulthandler catches hard/native crashes (COM, Tcl) and the
    # try/except catches Python exceptions on the main thread — both to a file we
    # can read after the fact.
    try:
        import faulthandler
        faulthandler.enable(open(_crashlog_path(), "a", encoding="utf-8"))
    except Exception:
        pass
    cfg = load_config()
    _LOG.info("config loaded (dictate=%s, toggle_auto=%s)",
              cfg["hotkey"].get("dictate_key"), cfg["hotkey"].get("toggle_auto_key"))
    if not core._resolve_api_key(cfg["transcription"]["api_key_env"],
                                 cfg["transcription"]["api_key_file"]):
        print("No Groq API key found. Set GROQ_API_KEY, or store it in Windows "
              "Credential Manager under service 'voice-to-text', account 'groq_key'. "
              "See windows\\config.example.toml.")
    start_hidden = any(a in ("--tray", "--minimized", "--hidden") for a in sys.argv[1:])
    try:
        VoiceAgent(cfg).run(start_hidden=start_hidden)
    except BaseException:
        import traceback
        import datetime
        try:
            with open(_crashlog_path(), "a", encoding="utf-8") as f:
                f.write(f"\n=== python exception {datetime.datetime.now()} ===\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
