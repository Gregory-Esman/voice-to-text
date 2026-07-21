"""Unit tests for mac_enroll.py (macOS voice enrollment). Run with the
project venv:
  uv run python tests/test_mac_enroll.py
Headless: no AppKit — a FakeApp stands in for FlowApp.
"""
import os
import sys
import tempfile
import tomllib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mac_enroll  # noqa: E402
import portable  # noqa: E402

SR = portable.vtt_core.SAMPLE_RATE
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


def silence(sec):
    return (np.random.randn(int(sec * SR)) * 0.002).astype("float32")


class FakeRecorder:
    def __init__(self):
        self.taps = []

    def add_tap(self, fn):
        if fn not in self.taps:
            self.taps.append(fn)

    def remove_tap(self, fn):
        if fn in self.taps:
            self.taps.remove(fn)

    def feed(self, chunk, is_recording=False):
        for fn in list(self.taps):
            fn(chunk, is_recording)


class FakeStatus:
    def __init__(self):
        self.title = "Idle"


class FakeApp:
    def __init__(self, state="idle", paused=False):
        self.state = state
        self._paused = paused
        self.recorder = FakeRecorder()
        self.status_item = FakeStatus()
        self.cues = []

    def play_cue(self, kind):
        self.cues.append(kind)


# ── persist_personal ──
_orig_personal = None
if mac_enroll.PERSONAL_CONFIG_PATH.exists():
    _orig_personal = mac_enroll.PERSONAL_CONFIG_PATH.read_text()

try:
    mac_enroll.persist_personal("Greg", 'says "hi"', "555")
    check("persist_personal writes the file", mac_enroll.PERSONAL_CONFIG_PATH.exists())
    with open(mac_enroll.PERSONAL_CONFIG_PATH, "rb") as f:
        parsed = tomllib.load(f)
    check("tomllib parses it", "personal" in parsed)
    check("name round-trips", parsed.get("personal", {}).get("name") == "Greg")
    check("email (with embedded quote) round-trips",
          parsed.get("personal", {}).get("email") == 'says "hi"')
    check("phone round-trips", parsed.get("personal", {}).get("phone") == "555")
finally:
    if _orig_personal is not None:
        mac_enroll.PERSONAL_CONFIG_PATH.write_text(_orig_personal)
    else:
        try:
            mac_enroll.PERSONAL_CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass


# ── EnrollmentFlow: too-short audio ──
app = FakeApp(paused=False)
results = []
flow = mac_enroll.EnrollmentFlow(app, lambda ok, msg: results.append((ok, msg)))
check("start() succeeds when idle", flow.start() is True)
check("start() pauses hotkeys", app._paused is True)
app.recorder.feed(voice(3.0))  # only 3s fed -> well under MIN_ENROLL_SECONDS
flow._finish()
check("too-short audio -> done(False, ...)", results and results[-1][0] is False)
check("_paused restored (was False)", app._paused is False)
check("tap removed after finish", flow._collect not in app.recorder.taps)


# ── EnrollmentFlow: silence (contains_speech gate) ──
app2 = FakeApp(paused=True)
results2 = []
flow2 = mac_enroll.EnrollmentFlow(app2, lambda ok, msg: results2.append((ok, msg)))
check("start() succeeds (prior _paused=True)", flow2.start() is True)
app2.recorder.feed(silence(30.0))
flow2._finish()
check("silence (30s) -> done(False, ...) via contains_speech gate",
      results2 and results2[-1][0] is False)
check("_paused restored (was True)", app2._paused is True)


# ── EnrollmentFlow: good audio, prior _paused False ──
class FakeGate:
    calls = []

    def __init__(self, profile_path, *a, **kw):
        self.profile_path = profile_path

    def enroll(self, audio):
        FakeGate.calls.append(np.array(audio, copy=True))


_real_gate = portable.autodictate.SpeakerGate
portable.autodictate.SpeakerGate = FakeGate
try:
    with tempfile.TemporaryDirectory() as td:
        tmp_profile = os.path.join(td, "sub", "voice_profile.npy")
        _orig_vpp = portable.VOICE_PROFILE_PATH
        from pathlib import Path
        portable.VOICE_PROFILE_PATH = Path(tmp_profile)
        try:
            app3 = FakeApp(paused=False)
            results3 = []
            flow3 = mac_enroll.EnrollmentFlow(app3, lambda ok, msg: results3.append((ok, msg)))
            check("start() succeeds (prior _paused=False)", flow3.start() is True)
            fed = np.concatenate([voice(6.0), voice(6.0)])
            app3.recorder.feed(fed)
            flow3._finish()
            check("good audio -> done(True, ...)", results3 and results3[-1][0] is True)
            check("_paused restored to original (False)", app3._paused is False)
            check("SpeakerGate.enroll called with concatenated audio",
                  FakeGate.calls and FakeGate.calls[-1].size == fed.size)
        finally:
            portable.VOICE_PROFILE_PATH = _orig_vpp
finally:
    portable.autodictate.SpeakerGate = _real_gate


# ── EnrollmentFlow: good audio, prior _paused True (restore honors saved state) ──
portable.autodictate.SpeakerGate = FakeGate
try:
    with tempfile.TemporaryDirectory() as td:
        tmp_profile = os.path.join(td, "voice_profile.npy")
        _orig_vpp = portable.VOICE_PROFILE_PATH
        from pathlib import Path
        portable.VOICE_PROFILE_PATH = Path(tmp_profile)
        try:
            app4 = FakeApp(paused=True)
            results4 = []
            flow4 = mac_enroll.EnrollmentFlow(app4, lambda ok, msg: results4.append((ok, msg)))
            flow4.start()
            app4.recorder.feed(np.concatenate([voice(6.0), voice(6.0)]))
            flow4._finish()
            check("good audio -> done(True, ...) (prior paused=True)",
                  results4 and results4[-1][0] is True)
            check("_paused restored to original (True)", app4._paused is True)
        finally:
            portable.VOICE_PROFILE_PATH = _orig_vpp
finally:
    portable.autodictate.SpeakerGate = _real_gate


# ── start() guards ──
busy_app = FakeApp(state="recording")
flow5 = mac_enroll.EnrollmentFlow(busy_app, lambda ok, msg: None)
check("start() returns False when app not idle", flow5.start() is False)

app6 = FakeApp(state="idle")
flow6a = mac_enroll.EnrollmentFlow(app6, lambda ok, msg: None)
flow6b = mac_enroll.EnrollmentFlow(app6, lambda ok, msg: None)
check("first start() on idle app succeeds", flow6a.start() is True)
check("second start() while one is running returns False", flow6b.start() is False)
flow6a._finish()  # cleanup so its daemon Timer thread has nothing left to do
check("after finish, a new flow can start again", flow6a is not None)


print(("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
