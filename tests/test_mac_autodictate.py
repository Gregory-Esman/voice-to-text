"""Unit tests for mac_autodictate.py — run with the project venv:
  uv run python tests/test_mac_autodictate.py
Headless: no AppKit/AX import happens here (mac_autodictate keeps those lazy
inside FocusWatcherMac/AutoChipMac methods, none of which this file calls).
Covers: classify_focus matrix, AutoDictateController dispatch (with a FakeApp/
FakeGate/FakeWatcher/FakeChip), and set_enabled's enrollment gate.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mac_autodictate as mad  # noqa: E402

SR = mad.SAMPLE_RATE
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def voice(sec, f0=150.0, amp=0.3):
    t = np.arange(int(sec * SR)) / SR
    carrier = np.sin(2 * np.pi * f0 * t) + 0.4 * np.sin(2 * np.pi * 2 * f0 * t)
    syll = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t)
    return (carrier * syll * amp).astype("float32")


# ═══════════════════════ classify_focus matrix ═══════════════════════
OWN_PID = 111
OTHER_PID = 222

check("AXTextField -> armed",
      mad.classify_focus("AXTextField", "", False, OTHER_PID, OWN_PID,
                         "com.example.app", [])[0] is True)
check("AXSecureTextField as role -> cold",
      mad.classify_focus("AXSecureTextField", "", True, OTHER_PID, OWN_PID,
                         "com.example.app", [])[0] is False)
check("AXSecureTextField as subrole -> cold",
      mad.classify_focus("AXTextField", "AXSecureTextField", True, OTHER_PID,
                         OWN_PID, "com.example.app", [])[0] is False)
check("own pid -> cold",
      mad.classify_focus("AXTextField", "", True, OWN_PID, OWN_PID,
                         "com.example.app", [])[0] is False)
check("com.apple.terminal -> armed with (pid, 'terminal')",
      mad.classify_focus("AXUnknown", "", False, OTHER_PID, OWN_PID,
                         "com.apple.terminal", [])
      == (True, (OTHER_PID, "terminal"), "terminal:com.apple.terminal"))
check("unknown role + settable -> armed",
      mad.classify_focus("AXGroup", "", True, OTHER_PID, OWN_PID,
                         "com.other.app", [])[0] is True)
check("unknown role + not settable -> cold",
      mad.classify_focus("AXGroup", "", False, OTHER_PID, OWN_PID,
                         "com.other.app", [])[0] is False)
check("excluded substring -> cold",
      mad.classify_focus("AXTextField", "", True, OTHER_PID, OWN_PID,
                         "com.slack.desktop", ["slack"])[0] is False)
check("excluded list without a match -> unaffected",
      mad.classify_focus("AXTextField", "", True, OTHER_PID, OWN_PID,
                         "com.example.app", ["slack"])[0] is True)


# ═══════════════════════ controller dispatch ═══════════════════════
class FakeRecorder:
    def add_tap(self, fn):
        pass

    def remove_tap(self, fn):
        pass


class FakeApp:
    def __init__(self, personal=None):
        self.cfg = {
            "auto_dictate": {"enabled": False, "similarity": 0.60,
                             "silence_ms": 700, "min_speech_ms": 180,
                             "start_rms": 0.014, "end_rms": 0.008,
                             "adapt": True, "send_in_terminal": False,
                             "exclude_apps": []},
            "personal": personal or {"name": "Jamie Rivera",
                                     "email": "jamierivera@example.com",
                                     "phone": ""},
            "replacements": {},
        }
        self.state = "idle"
        self._paused = False
        self.recorder = FakeRecorder()
        self.emitted = []
        self.cues = []
        self.persisted = []
        self._transcripts = []       # queued canned transcripts
        self.transcribe_vocab_calls = []   # vocabulary arg on each call
        self.write_calls = []        # (instruction, maybe)
        self.write_result = ""

    def play_cue(self, kind):
        self.cues.append(kind)

    def emit_text(self, text):
        self.emitted.append(text)

    def transcribe_for_auto(self, audio, vocabulary=None):
        self.transcribe_vocab_calls.append(vocabulary)
        return self._transcripts.pop(0) if self._transcripts else ""

    def auto_write(self, instruction, maybe=False):
        self.write_calls.append((instruction, maybe))
        return self.write_result

    def _persist(self, key, value, section=None):
        self.persisted.append((section, key, value))


class FakeGate:
    def __init__(self, enrolled=True, accept=(True, 0.9)):
        self._enrolled = enrolled
        self._accept = accept
        self.adapted = []

    def enrolled(self):
        return self._enrolled

    def accept(self, audio):
        return self._accept

    def maybe_adapt(self, score):
        self.adapted.append(score)

    def preload(self):
        pass


class FakeWatcher:
    def __init__(self, on_change):
        self.started = False
        self.stopped = False
        self.poked = False
        self.excluded_apps = set()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def poke(self):
        self.poked = True


class FakeChip:
    def __init__(self):
        self.shown = []
        self.hidden = 0
        self.toasts = []

    def show(self, state):
        self.shown.append(state)

    def hide(self):
        self.hidden += 1

    def toast(self, text, color=None, secs=1.4):
        self.toasts.append(text)


def make_controller(app=None, gate_kwargs=None):
    app = app or FakeApp()
    gate = FakeGate(**(gate_kwargs or {}))
    ctrl = mad.AutoDictateController(app, watcher=FakeWatcher(lambda *a: None),
                                     chip=FakeChip(), gate=gate)
    ctrl._enabled = True
    return ctrl


CID = (4242, "AXTextArea", None)

# monkeypatch the keystroke/app-action senders so tests never touch real
# input devices or launch real apps
_send_backspaces_calls = []
_send_word_backspaces_calls = []
_send_enter_calls = []
_activate_calls = []


def _fake_send_backspaces(n):
    _send_backspaces_calls.append(n)


def _fake_send_word_backspaces(n):
    _send_word_backspaces_calls.append(n)


def _fake_send_enter():
    _send_enter_calls.append(True)


def _fake_activate_app(q):
    _activate_calls.append(q)
    return True


def _fake_launch_app(q):
    return False


mad.send_backspaces = _fake_send_backspaces
mad.send_word_backspaces = _fake_send_word_backspaces
mad.send_enter = _fake_send_enter
mad.activate_app = _fake_activate_app
mad.launch_app = _fake_launch_app


def reset_sends():
    _send_backspaces_calls.clear()
    _send_word_backspaces_calls.clear()
    _send_enter_calls.clear()
    _activate_calls.clear()


# ── "Scratch that." -> backspaces == len(last emit) ──
reset_sends()
c = make_controller()
c._armed, c._armed_id = True, CID
c.app._transcripts = ["Hello there."]
c._process_utterance(voice(1.5), CID, (0.0, 1.5))
check("dictation typed before scratch", c.app.emitted == ["Hello there."])
last_len = len(c.app.emitted[-1])
c.app._transcripts = ["Scratch that."]
c._process_utterance(voice(1.2), CID, (2.0, 3.2))
check("'Scratch that.' -> backspaces == len(last emit)",
      _send_backspaces_calls and _send_backspaces_calls[-1] == last_len)
check("'Scratch that.' plays tick", c.app.cues[-1] == "tick")

# ── "send it" in a terminal box, send_in_terminal=false -> blocked + cancel ──
reset_sends()
c = make_controller()
c._armed, c._armed_id = True, (555, "terminal")
c.app._transcripts = ["send it"]
c._process_utterance(voice(1.0), (555, "terminal"), (0.0, 1.0))
check("'send it' in terminal blocked (no Enter sent)", not _send_enter_calls)
check("'send it' in terminal plays cancel", c.app.cues[-1] == "cancel")

# ── "delete the last three words" -> backspace count matches chars_to_delete ──
reset_sends()
c = make_controller()
c._armed, c._armed_id = True, CID
c._auto_last[CID] = {"text": "Hello there. How are you today?", "last": ""}
c.app._transcripts = ["delete the last three words"]
c._process_utterance(voice(1.4), CID, (0.0, 1.4))
from portable import autodictate as _ad  # noqa: E402
expect_n = _ad.chars_to_delete("Hello there. How are you today?", "word", 3)
check("'delete the last three words' -> correct backspace count",
      _send_backspaces_calls == [expect_n])

# ── "type my email" -> personal value emitted verbatim (no start_case mangling) ──
c = make_controller()
c._armed, c._armed_id = True, CID
c.app._transcripts = ["type my email"]
c._process_utterance(voice(1.0), CID, (0.0, 1.0))
check("'type my email' emits the raw address verbatim",
      c.app.emitted[-1] == "jamierivera@example.com")

# ── "switch to slack" -> activate called, nothing typed ──
reset_sends()
c = make_controller()
c._armed, c._armed_id = True, CID
before = list(c.app.emitted)
c.app._transcripts = ["switch to slack"]
c._process_utterance(voice(1.0), CID, (0.0, 1.0))
check("'switch to slack' calls activate_app('slack')", _activate_calls == ["slack"])
check("'switch to slack' types nothing", c.app.emitted == before)

# ── prompt-echo: >4s clip, transcript == personal email -> unbiased retry ──
c = make_controller()
c._armed, c._armed_id = True, CID
c.app._transcripts = ["jamierivera@example.com", "hey there how's it going"]
c._process_utterance(voice(4.5), CID, (0.0, 4.5))
check("prompt echo triggers a second unbiased transcribe call",
      c.app.transcribe_vocab_calls == [None, ""])
check("prompt echo: second transcript wins",
      c.app.emitted[-1].lower().startswith("hey there"))

# ── focus moved (cid mismatch at emit time) -> dropped ──
c = make_controller()
c._armed, c._armed_id = True, ("some", "other", "box")
before = list(c.app.emitted)
c.app._transcripts = ["hello there friend"]
c._process_utterance(voice(1.0), CID, (0.0, 1.0))
check("focus moved -> utterance dropped", c.app.emitted == before)

# ── gate reject -> dropped ──
c = make_controller(gate_kwargs={"accept": (False, 0.1)})
c._armed, c._armed_id = True, CID
before = list(c.app.emitted)
c.app._transcripts = ["hello there friend"]
c._process_utterance(voice(1.0), CID, (0.0, 1.0))
check("gate reject -> utterance dropped", c.app.emitted == before)
check("gate reject -> never even transcribed", c.app.transcribe_vocab_calls == [])

# ── noise "Ahem." -> dropped ──
c = make_controller()
c._armed, c._armed_id = True, CID
before = list(c.app.emitted)
c.app._transcripts = ["Ahem."]
c._process_utterance(voice(1.0), CID, (0.0, 1.0))
check("noise 'Ahem.' -> dropped", c.app.emitted == before)

# ── two utterances -> prose joins with space + boundary casing via start_case ──
c = make_controller()
c._armed, c._armed_id = True, CID
c.app._transcripts = ["hello there"]
c._process_utterance(voice(1.0), CID, (0.0, 1.0))
c.app._transcripts = ["how are you"]
c._process_utterance(voice(1.0), CID, (2.0, 3.0))
check("two utterances join with a space",
      c.app.emitted == ["Hello there", " how are you"])

# ═══════════════════════ set_enabled enrollment gate ═══════════════════════
c = make_controller(gate_kwargs={"enrolled": False})
c._enabled = False
ok = c.set_enabled(True)
check("set_enabled(True) refused without enrollment -> returns False", ok is False)
check("set_enabled(True) refused -> stays disabled", c.enabled() is False)

print(("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
