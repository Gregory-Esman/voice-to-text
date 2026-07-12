"""Auto-Dictate building blocks (see AUTO-DICTATE-BRIEF.md).

Three independent pieces, wired together by app.py:

  • Endpointer   — segments the live mic stream into utterances (local VAD):
                   speech begins after ~250 ms of voice, ends after ~900 ms of
                   trailing quiet, 60 s hard cap, with a pre-roll so the first
                   syllable isn't clipped.
  • FocusWatcher — SetWinEventHook(EVENT_OBJECT_FOCUS) + UI Automation: reports
                   whether the focused control is an editable text field.
                   Fails COLD: anything unknown/odd is "not editable".
  • SpeakerGate  — local speaker verification (resemblyzer). Enroll once,
                   then every utterance is embedded ON-DEVICE and compared to
                   the profile before any audio is uploaded.

Plus special_of(): matches the two spoken commands ("scratch that", "send it")
after transcription.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque

import numpy as np

_LOG = logging.getLogger("vtt")

SAMPLE_RATE = 16_000


# ───────────────────────── utterance endpointing ─────────────────────────
class Endpointer:
    """Feed audio blocks from the sounddevice callback; get back a finished
    utterance (float32 mono) when one closes, else None. feed() is cheap —
    one RMS per block — so it's safe on the audio thread. `speaking` is True
    while an utterance is being captured (drives the HUD chip)."""

    def __init__(self, sr: int = SAMPLE_RATE, preroll_ms: int = 400,
                 min_speech_ms: int = 250, silence_ms: int = 900,
                 max_s: float = 60.0, start_rms: float = 0.020,
                 end_rms: float = 0.012) -> None:
        self.sr = sr
        self.preroll_ms = preroll_ms
        self.min_speech_ms = min_speech_ms
        self.silence_ms = silence_ms
        self.max_s = max_s
        self.start_rms = start_rms
        self.end_rms = end_rms
        self.speaking = False
        self._pre: deque = deque()      # (block, dur_ms) pre-roll ring
        self._pre_ms = 0.0
        self._voiced_ms = 0.0           # consecutive-ish voice before start
        self._buf: list = []
        self._buf_s = 0.0
        self._quiet_ms = 0.0

        self.last_span = (0.0, 0.0)     # wall-clock (start, end) of last utterance

    def reset(self) -> None:
        self.speaking = False
        self._pre.clear()
        self._pre_ms = 0.0
        self._voiced_ms = 0.0
        self._buf = []
        self._buf_s = 0.0
        self._quiet_ms = 0.0

    def feed(self, block: np.ndarray):
        dur_ms = len(block) / self.sr * 1000.0
        rms = float(np.sqrt(np.mean(block * block) + 1e-12))
        if not self.speaking:
            self._pre.append((block, dur_ms))
            self._pre_ms += dur_ms
            while self._pre_ms > self.preroll_ms and len(self._pre) > 1:
                _, d = self._pre.popleft()
                self._pre_ms -= d
            if rms >= self.start_rms:
                self._voiced_ms += dur_ms
                if self._voiced_ms >= self.min_speech_ms:
                    self.speaking = True
                    self._buf = [b for b, _ in self._pre]
                    self._buf_s = self._pre_ms / 1000.0
                    self._quiet_ms = 0.0
            else:
                self._voiced_ms = 0.0
            return None
        # in an utterance
        self._buf.append(block)
        self._buf_s += dur_ms / 1000.0
        if rms < self.end_rms:
            self._quiet_ms += dur_ms
        else:
            self._quiet_ms = 0.0
        if self._quiet_ms >= self.silence_ms or self._buf_s >= self.max_s:
            utt = np.concatenate(self._buf).astype("float32")
            # trim the trailing quiet down to ~150 ms so Whisper gets speech
            drop = int(max(0.0, self._quiet_ms - 150.0) / 1000.0 * self.sr)
            if 0 < drop < utt.size:
                utt = utt[:-drop]
            t_end = time.time() - drop / self.sr
            self.last_span = (t_end - len(utt) / self.sr, t_end)
            self.reset()
            return utt
        return None


# ───────────────────────── spoken specials ─────────────────────────
# Include Whisper's common mishears of each phrase (short clips get fuzzy:
# "send it" → "Sender." / "Sent it") so the first try fires.
_SCRATCH = {"scratch that", "scratch it", "scratched that", "strike that"}
_SEND = {"send it", "sent it", "sender", "send"}


def _norm_phrase(text: str) -> str:
    t = re.sub(r"[^a-z ]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def special_of(text: str):
    """'scratch' | 'send' | None. Only an utterance that IS the phrase (nothing
    else) counts — "scratch that idea entirely" is dictation, not a command."""
    n = _norm_phrase(text)
    if n in _SCRATCH:
        return "scratch"
    if n in _SEND:
        return "send"
    return None


# Spoken connectors that precede a command in natural speech ("and also reply
# with...", "okay so write...") — tolerated in front of every command matcher.
_LEADIN = r"(?:(?:and|also|then|now|okay|ok|so|please|hey|um|uh|yeah) )*"

# An utterance that STARTS like an instruction to a ghostwriter routes to the
# writing model instead of being typed verbatim ("write a reply saying no",
# "draft an email to Bob about the invoice").
_COMMAND_RE = re.compile(
    rf"^{_LEADIN}(?:write|draft|compose)\b"
    rf"|^{_LEADIN}(?:reply|respond|answer)\s+(?:saying|to|that|with|and)\b")

# "switch to slack" / "open chrome" as a WHOLE utterance → app action.
_ACTION_RE = re.compile(
    rf"^{_LEADIN}(?:switch to|go to|jump to|open|launch)\s+([a-z0-9 .&+-]{{1,40}})$")


def is_command(text: str) -> bool:
    return bool(_COMMAND_RE.match(_norm_phrase(text)))


# Looser imperative openers ("add a paragraph of well wishes", "make it
# shorter") — MIGHT be a writing command, might be literal dictation. These
# route to the writing model with a DICTATION escape hatch instead of being
# decided by regex.
_MAYBE_COMMAND_RE = re.compile(
    rf"^{_LEADIN}(?:add|append|make|create|give|say|tell|answer|change|turn|"
    r"fix|shorten|expand|extend|reword|redo|translate|summarize|summarise|"
    r"include|attach|throw in|put)\b")


def is_maybe_command(text: str) -> bool:
    return bool(_MAYBE_COMMAND_RE.match(_norm_phrase(text)))


def action_of(text: str):
    """App name to switch to/launch, or None."""
    m = _ACTION_RE.match(_norm_phrase(text))
    return m.group(1).strip() if m else None


# ── voice editing: "remove the last word / three words / sentence" ──
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10,
              "couple": 2, "couple of": 2, "few": 3}
_DELETE_RE = re.compile(
    rf"^{_LEADIN}(?:remove|delete|erase) (?:the )?last"
    r"(?: (\d+|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|couple(?: of)?|few))?"
    r" (words?|sentences?|lines?)$")


def delete_of(text: str):
    """('word'|'sentence', count) for a delete command, else None."""
    # keep digits (unlike _norm_phrase) — "remove last 5 words"
    n = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    m = _DELETE_RE.match(re.sub(r"\s+", " ", n).strip())
    if not m:
        return None
    num, unit = m.group(1), m.group(2)
    count = int(num) if num and num.isdigit() else _NUM_WORDS.get(num or "one", 1)
    count = max(1, min(50, count))
    unit = "word" if unit.startswith("word") else "sentence"
    return (unit, count)


def chars_to_delete(tracked: str, unit: str, count: int) -> int:
    """How many backspaces remove the last `count` words/sentences from the
    text this session typed. 0 if nothing is tracked."""
    t = tracked
    if not t or not t.strip():
        return 0
    if unit == "word":
        m = re.search(r"(?:\s*\S+){1,%d}\s*$" % count, t)
        return len(m.group(0)) if m else len(t)
    # sentences: walk back over `count` terminator groups
    s = t.rstrip()
    i = len(s) - 1
    for _ in range(count):
        while i >= 0 and s[i] in ".!?…":
            i -= 1
        while i >= 0 and s[i] not in ".!?…":
            i -= 1
        if i < 0:
            break
    return len(t) - (i + 1)


# ── noise transcripts: throat-clears/coughs that Whisper renders as words ──
_NOISE_WORDS = {
    "ahem", "hmm", "hm", "mm", "mmm", "mhm", "mm-hmm", "uh", "um", "uh-huh",
    "huh", "ah", "oh", "eh", "er", "erm", "ugh", "oof", "phew", "tsk", "psst",
    "shh", "achoo", "cough", "coughs", "coughing", "sniff", "sniffs",
    "sniffles", "sniffling", "sigh", "sighs", "sighing", "grunt", "grunts",
    "hiccup", "yawn", "yawns", "throat", "clears", "clearing", "wheeze",
    "groan", "groans", "snort", "hmph", "whew",
}


# ── personal details: snippets + spoken-form fixups ──
_SNIPPET_ALIASES = {"email address": "email", "e mail": "email",
                    "e mail address": "email", "phone number": "phone",
                    "full name": "name"}


def snippet_of(text: str, personal: dict):
    """"type my email" / "insert my name" → the configured value, else None."""
    m = re.match(rf"^{_LEADIN}(?:insert|type|enter|paste|put in) my ([a-z ]{{2,30}})$",
                 _norm_phrase(text))
    if not m:
        return None
    key = m.group(1).strip()
    key = _SNIPPET_ALIASES.get(key, key)
    val = str((personal or {}).get(key, "")).strip()
    return val or None


def build_fixers(personal: dict, replacements: dict) -> list:
    """[(compiled regex, replacement)] applied to every transcript. Built-in:
    spoken email forms ("jamie rivera at example dot com") → the real address.
    Plus any literal pairs from the [replacements] config table."""
    fixers = []
    email = str((personal or {}).get("email", "")).strip()
    name = str((personal or {}).get("name", "")).strip()
    if email and "@" in email:
        local, _, domain = email.partition("@")
        dom = re.escape(domain).replace(r"\.", r"\s*(?:\.|dot)\s*")
        alts = [re.escape(local)]
        if name:
            alts.append(r"[\s.\-]*".join(re.escape(p) for p in name.lower().split()))
        pat = rf"\b(?:{'|'.join(alts)})\s*(?:@|\bat\b)\s*{dom}\b"
        try:
            fixers.append((re.compile(pat, re.I), email))
        except Exception:
            pass
    for k, v in (replacements or {}).items():
        try:
            fixers.append((re.compile(re.escape(str(k)), re.I), str(v)))
        except Exception:
            pass
    return fixers


def is_prompt_echo(text: str, personal: dict) -> bool:
    """True if the transcript IS one of the personal glossary values — with a
    long clip that means Whisper echoed the vocabulary prompt instead of
    transcribing (saying just your own name/email takes only a couple seconds)."""
    n = re.sub(r"[^a-z0-9@. ]", " ", (text or "").lower())
    n = re.sub(r"\s+", " ", n).strip().rstrip(".")
    if not n:
        return False
    for v in (personal or {}).values():
        v = str(v).lower().strip()
        if v and (n == v or n.replace(" ", "") == v.replace(" ", "")):
            return True
    return False


def apply_fixers(text: str, fixers: list) -> str:
    for rx, repl in fixers:
        text = rx.sub(repl, text)
    return text


def is_noise(text: str) -> bool:
    """True if the transcript is ONLY vocal noise ("Ahem.", "*coughs*",
    "[clears throat]") — a throat-clear, not dictation. Any real word keeps
    the utterance."""
    t = re.sub(r"[\[\]()*]", " ", (text or "").lower())
    words = re.findall(r"[a-z'-]+", t)
    return bool(words) and all(w.strip("'-") in _NOISE_WORDS for w in words)


# ───────────────────────── focus watching ─────────────────────────
_EVENT_OBJECT_FOCUS = 0x8005
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_UIA_IS_PASSWORD = 30019

# A focused terminal is always typeable but never exposes an Edit control —
# arm by process name instead.
_TERMINAL_EXES = {
    "cmd.exe", "conhost.exe", "openconsole.exe", "windowsterminal.exe",
    "wt.exe", "powershell.exe", "pwsh.exe", "mintty.exe", "alacritty.exe",
    "wezterm-gui.exe", "hyper.exe", "kitty.exe",
}

# Apps that must never auto-arm, by exe basename (config [auto_dictate]
# exclude_apps). Populated by app.py at startup.
EXCLUDED_EXES: set = set()
_EXE_CACHE: dict = {}


def _exe_of(pid: int) -> str:
    """Lowercased exe basename for a pid ('' if unknown)."""
    if pid in _EXE_CACHE:
        return _EXE_CACHE[pid]
    exe = ""
    try:
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32process  # type: ignore
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION
                                 | win32con.PROCESS_VM_READ, False, pid)
        exe = os.path.basename(win32process.GetModuleFileNameEx(h, 0)).lower()
        win32api.CloseHandle(h)
    except Exception:
        pass
    if len(_EXE_CACHE) > 256:
        _EXE_CACHE.clear()
    _EXE_CACHE[pid] = exe
    return exe


class FocusWatcher:
    """Watches keyboard focus system-wide. on_change(editable, ctrl_id, desc)
    fires when focus moves to a different control or the editable state flips.
    ctrl_id is a stable-ish identity for "same box?" checks. Fails cold."""

    def __init__(self, on_change) -> None:
        self._on_change = on_change
        self._evt = threading.Event()
        self._last = None            # (editable, ctrl_id)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._hook_loop, name="vtt-focus-hook",
                         daemon=True).start()
        threading.Thread(target=self._classify_loop, name="vtt-focus-uia",
                         daemon=True).start()
        self._evt.set()              # classify the initial focus at startup

    def poke(self) -> None:
        """Force a re-classification (e.g. after toggling the mode on)."""
        self._evt.set()

    # -- thread A: the WinEvent hook needs its own message pump --
    def _hook_loop(self) -> None:
        try:
            import ctypes
            import ctypes.wintypes as wt
            user32 = ctypes.windll.user32
            proc_t = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND,
                                        ctypes.c_long, ctypes.c_long,
                                        wt.DWORD, wt.DWORD)

            def _cb(hook, event, hwnd, obj_id, child_id, tid, t):
                self._evt.set()

            self._proc = proc_t(_cb)   # keep a ref or it's GC'd under the hook
            hook = user32.SetWinEventHook(
                _EVENT_OBJECT_FOCUS, _EVENT_OBJECT_FOCUS, 0, self._proc, 0, 0,
                _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
            if not hook:
                _LOG.error("focus: SetWinEventHook failed")
                return
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            _LOG.exception("focus: hook thread died")

    # -- thread B: debounce + classify on a UIA-initialized thread --
    def _classify_loop(self) -> None:
        try:
            import uiautomation as auto
            try:
                _init = auto.UIAutomationInitializerInThread()  # noqa: F841
            except Exception:
                try:
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass
        except Exception:
            _LOG.exception("focus: uiautomation unavailable — Auto-Dictate stays cold")
            return
        while True:
            self._evt.wait()
            self._evt.clear()
            time.sleep(0.12)          # let a burst of focus events settle
            if self._evt.is_set():
                continue
            try:
                state = self._classify(auto)
            except Exception:
                state = (False, None, "uia-error")
            if state[:2] != (self._last or (None, None)):
                self._last = state[:2]
                try:
                    self._on_change(*state)
                except Exception:
                    _LOG.exception("focus: on_change failed")

    def _classify(self, auto):
        c = auto.GetFocusedControl()
        if c is None:
            return (False, None, "no-focus")
        try:
            pid = c.ProcessId
        except Exception:
            pid = -1
        try:
            name = (c.Name or "")[:40]
        except Exception:
            name = ""
        try:
            ct = c.ControlTypeName
        except Exception:
            ct = "?"
        desc = f"{ct} '{name}' pid={pid}"
        if pid == os.getpid():                    # never our own windows
            return (False, None, desc)
        try:
            if c.GetPropertyValue(_UIA_IS_PASSWORD):
                return (False, None, desc + " [password]")
        except Exception:
            pass
        exe = _exe_of(pid)
        if exe in EXCLUDED_EXES:
            return (False, None, desc + " [excluded]")
        if exe in _TERMINAL_EXES:
            # terminals are always typeable when focused; use a pid-stable id
            # (console runtime ids churn, which would drop utterances mid-flight)
            return (True, (pid, "terminal"), desc + " [terminal]")
        editable = False
        if ct == "EditControl":
            editable = not self._readonly(c, default=False)
        elif ct in ("DocumentControl", "ComboBoxControl"):
            # a Document/ComboBox is editable only with positive evidence (a
            # writable ValuePattern) — otherwise every web PAGE and dropdown
            # would arm the mic. Web search boxes with suggestions (YouTube,
            # Google) expose as ComboBox.
            editable = self._has_value_pattern(c) and not self._readonly(c, default=True)
        if not editable:
            return (False, None, desc)
        try:
            cid = tuple(c.GetRuntimeId())
        except Exception:
            cid = (pid, ct, name)
        return (True, cid, desc)

    @staticmethod
    def _value_pattern(c):
        for getter in ("GetValuePattern", "GetPattern"):
            try:
                fn = getattr(c, getter)
                vp = fn() if getter == "GetValuePattern" else fn(10002)  # UIA_ValuePatternId
                if vp is not None:
                    return vp
            except Exception:
                continue
        return None

    @classmethod
    def _has_value_pattern(cls, c) -> bool:
        return cls._value_pattern(c) is not None

    @classmethod
    def _readonly(cls, c, default: bool) -> bool:
        vp = cls._value_pattern(c)
        if vp is None:
            return default
        try:
            return bool(vp.IsReadOnly)
        except Exception:
            return default


# ───────────────────────── speaker verification ─────────────────────────
class SpeakerGate:
    """Local your-voice filter. enroll() stores an embedding of the user's
    voice; accept() embeds an utterance ON-DEVICE and cosine-compares. All of
    it runs on CPU via resemblyzer; no audio leaves the machine here."""

    _MIN_S = 1.8            # resemblyzer needs ≥ ~1.6 s; tile short utterances
    _SHORT_S = 1.5          # below this the tiled embedding is unreliable
    _SHORT_MARGIN = 0.12    # …so relax the accept bar for short clips
    ADAPT_MIN = 0.75        # only high-confidence accepts feed the profile
    ADAPT_ALPHA = 0.05      # blend rate per adapted utterance

    def __init__(self, profile_path: str, threshold: float = 0.75,
                 adapt: bool = True) -> None:
        self.profile_path = profile_path
        self.threshold = float(threshold)
        self.adapt_enabled = bool(adapt)
        self._enc = None
        self._profile = None
        self._last_emb = None
        self._adapts = 0
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            import resemblyzer  # noqa: F401
            return True
        except Exception:
            return False

    def enrolled(self) -> bool:
        if self._profile is not None:
            return True
        return os.path.exists(self.profile_path)

    def _encoder(self):
        with self._lock:
            if self._enc is None:
                from resemblyzer import VoiceEncoder
                t0 = time.time()
                self._enc = VoiceEncoder("cpu")
                _LOG.info("speaker: encoder loaded in %.1fs", time.time() - t0)
            return self._enc

    def _load_profile(self):
        if self._profile is None and os.path.exists(self.profile_path):
            self._profile = np.load(self.profile_path)
        return self._profile

    def _embed(self, audio: np.ndarray) -> np.ndarray:
        wav = np.asarray(audio, dtype="float32")
        # identity is established in the first few seconds — embedding a whole
        # long dictation just adds paste lag
        wav = wav[:int(6.0 * SAMPLE_RATE)]
        m = float(np.max(np.abs(wav))) if wav.size else 0.0
        if m > 0:
            wav = wav * (0.9 / m)
        need = int(self._MIN_S * SAMPLE_RATE)
        if wav.size < need:
            reps = int(np.ceil(need / max(1, wav.size)))
            wav = np.tile(wav, reps)[:need]
        return self._encoder().embed_utterance(wav)

    def enroll(self, audio: np.ndarray) -> None:
        emb = self._embed(audio)
        os.makedirs(os.path.dirname(self.profile_path), exist_ok=True)
        np.save(self.profile_path, emb)
        # keep the raw enrollment too, so an adapted profile can be reset
        try:
            np.save(self.profile_path.replace(".npy", "_enrolled.npy"), emb)
        except Exception:
            pass
        self._profile = emb
        _LOG.info("speaker: profile enrolled (%.1fs of audio)",
                  len(audio) / SAMPLE_RATE)

    def maybe_adapt(self, score: float) -> None:
        """Blend the last accepted utterance into the profile when it was a
        high-confidence match, so the profile tracks the user's real mic
        distance/voice over time. The high ADAPT_MIN bar keeps a borderline
        impostor accept (threshold can sit well below it) out of the profile."""
        if (not self.adapt_enabled or self._last_emb is None
                or self._profile is None or score < self.ADAPT_MIN):
            return
        p = self._profile * (1.0 - self.ADAPT_ALPHA) + self._last_emb * self.ADAPT_ALPHA
        n = float(np.linalg.norm(p))
        if n <= 0:
            return
        self._profile = p / n
        self._adapts += 1
        try:
            np.save(self.profile_path, self._profile)
        except Exception:
            pass
        if self._adapts % 25 == 1:
            _LOG.info("speaker: profile adapted (%d utterances blended)", self._adapts)

    def preload(self) -> None:
        """Load the encoder + profile up front (background thread at startup)
        so the first utterance doesn't pay the multi-second model load."""
        try:
            if self.enrolled():
                self._load_profile()
                self._encoder()
                # first real inference pays a ~1.5s torch warm-up — spend it
                # here on silence instead of on the user's first utterance
                self._embed(np.zeros(int(2.0 * SAMPLE_RATE), dtype="float32"))
                _LOG.info("speaker: warmed up")
        except Exception:
            _LOG.exception("speaker: preload failed")

    def accept(self, audio: np.ndarray) -> tuple[bool, float]:
        prof = self._load_profile()
        if prof is None:
            return (False, 0.0)
        emb = self._embed(audio)          # both L2-normalized → dot = cosine
        self._last_emb = emb
        score = float(np.dot(emb, prof))
        # a sub-1.8s clip is tiled up to the encoder's minimum, which
        # systematically depresses the cosine score — a genuine short command
        # ("yes", "send it") lands ~0.10-0.15 under a full-length utterance and
        # was being dropped. Relax the bar for short audio (recall-first).
        bar = self.threshold
        if len(audio) < self._SHORT_S * SAMPLE_RATE:
            bar -= self._SHORT_MARGIN
        return (score >= bar, score)


# ───────────────────────── speaker-echo filter ─────────────────────────
class LoopbackMonitor:
    """Rolling loudness envelope of what THIS machine is playing (WASAPI
    loopback on the default speakers). An utterance whose envelope matches the
    speaker output is the computer hearing itself (a video, music) — drop it,
    whatever the voice filter thought. The user's live voice is never in the
    output stream, so this filter can't eat real dictation. Entirely local.
    Fail-open: if loopback can't start, everything passes as before."""

    HZ = 100                     # envelope resolution (10 ms)
    LAG = 0.35                   # max mic-vs-loopback misalignment searched

    def __init__(self) -> None:
        self._buf: deque = deque(maxlen=self.HZ * 90)   # (t, rms), ~90 s
        self._ok = False
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="vtt-loopback",
                         daemon=True).start()

    def _run(self) -> None:
        logged_fail = False
        while True:
            try:
                import pyaudiowpatch as pa
                p = pa.PyAudio()
                info = p.get_host_api_info_by_type(pa.paWASAPI)
                spk = p.get_device_info_by_index(info["defaultOutputDevice"])
                base_name = spk["name"]        # the actual output device now
                if not spk.get("isLoopbackDevice"):
                    for lb in p.get_loopback_device_info_generator():
                        if spk["name"] in lb["name"]:
                            spk = lb
                            break
                rate = int(spk["defaultSampleRate"])
                frames = max(1, rate // self.HZ)

                def cb(data, n, ti, status):
                    x = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
                    r = float(np.sqrt(np.mean(x * x) + 1e-12))
                    with self._lock:
                        self._buf.append((time.time(), r))
                    return (None, pa.paContinue)

                stream = p.open(format=pa.paInt16,
                                channels=spk["maxInputChannels"], rate=rate,
                                input=True, input_device_index=spk["index"],
                                frames_per_buffer=frames, stream_callback=cb)
                self._ok = True
                logged_fail = False
                _LOG.info("loopback: monitoring '%s'", spk["name"])
                while stream.is_active():
                    time.sleep(10)
                    # follow the default speakers: headphones/Bluetooth swap
                    # means we're guarding a device nothing plays through
                    try:
                        info = p.get_host_api_info_by_type(pa.paWASAPI)
                        cur = p.get_device_info_by_index(
                            info["defaultOutputDevice"])["name"]
                    except Exception:
                        break
                    if cur != base_name:
                        _LOG.info("loopback: default output changed "
                                  "'%s' → '%s' — reopening", base_name, cur)
                        break
                stream.close()
                p.terminate()
                self._ok = False
            except Exception:
                self._ok = False
                if not logged_fail:
                    logged_fail = True
                    _LOG.exception("loopback: unavailable — echo filter off")
                time.sleep(60)
            time.sleep(10)

    def is_self_audio(self, mic_audio: np.ndarray, t0: float, t1: float,
                      threshold: float = 0.55) -> tuple[bool, float]:
        """(drop, correlation). Envelope cross-correlation between the mic
        utterance and the loopback window around it, over ±LAG alignment."""
        if not self._ok:
            return (False, 0.0)
        with self._lock:
            pts = [(t, r) for t, r in self._buf
                   if t0 - self.LAG - 0.5 <= t <= t1 + self.LAG + 0.1]
        if len(pts) < 10:
            return (False, 0.0)          # nothing was playing
        times = np.array([t for t, _ in pts])
        vals = np.array([r for _, r in pts])
        if float(vals.max()) < 3e-4:
            return (False, 0.0)          # effectively silence
        step = 1.0 / self.HZ
        grid = np.arange(t0 - self.LAG, t1 + self.LAG, step)
        out = np.interp(grid, times, vals, left=0.0, right=0.0)
        spb = int(SAMPLE_RATE * step)    # mic samples per envelope bin (10 ms)
        n = len(mic_audio) // spb
        if n < 25:                        # < 0.25 s of envelope — too little
            return (False, 0.0)
        mic = np.sqrt(np.mean(mic_audio[:n * spb].reshape(n, spb) ** 2,
                              axis=1) + 1e-12)

        def z(a):
            s = float(a.std())
            return (a - a.mean()) / s if s > 1e-9 else None

        zm = z(mic)
        if zm is None:
            return (False, 0.0)
        best = 0.0
        max_lag = int(2 * self.LAG / step)
        for lag in range(0, max_lag + 1):
            seg = out[lag: lag + n]
            if len(seg) < n:
                break
            zs = z(seg)
            if zs is None:
                continue
            best = max(best, float(np.dot(zm, zs) / n))
        return (best >= threshold, best)
