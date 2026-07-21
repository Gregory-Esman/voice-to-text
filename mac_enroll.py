"""macOS voice enrollment for the Auto-Dictate speaker gate.

Records ENROLL_SECONDS of natural speech off the recorder's always-on mic tap,
gates it for length + actual speech, then builds a resemblyzer voice-print via
portable.autodictate.SpeakerGate and saves it to portable.VOICE_PROFILE_PATH.

FlowApp.open_enroll() lazy-imports run_enrollment() so the app runs fine
before this module exists (or on a Lane 0-only checkout). This module in turn
never imports flow.py/AppKit at module scope, so it (and its tests) stay
importable headlessly.
"""
import threading
from pathlib import Path

import numpy as np

import portable

# Mirrors flow.IDLE's value. Kept local (rather than importing flow) so this
# module — and its tests — never need AppKit/rumps to be importable.
IDLE = "idle"

ENROLL_SECONDS = 30
MIN_ENROLL_SECONDS = 10

# Same directory as flow.py's own config.toml — gitignored, deep-merged over
# it at load time (see flow.load_config / PERSONAL_CONFIG_PATH).
PERSONAL_CONFIG_PATH = Path(__file__).resolve().parent / "config.personal.toml"


def log(msg: str) -> None:
    print(f"[mac_enroll] {msg}", flush=True)


def _toml_escape(s: str) -> str:
    """Escape a value for a TOML basic string (quotes/backslashes/newlines),
    so persist_personal's output always round-trips through tomllib."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def persist_personal(name: str, email: str, phone: str) -> None:
    """Write config.personal.toml wholesale — a [personal] section only, so
    Settings' Name/Email fields save without ever touching the committed
    config.toml. NEVER passed to Whisper as vocabulary bias (enforced by the
    transcription path, not here)."""
    lines = [
        "# Personal details — gitignored, deep-merged over config.toml.",
        "# Written by mac_enroll.persist_personal (Settings > Name/Email).",
        "[personal]",
        f'name = "{_toml_escape(name)}"',
        f'email = "{_toml_escape(email)}"',
        f'phone = "{_toml_escape(phone)}"',
        "",
    ]
    PERSONAL_CONFIG_PATH.write_text("\n".join(lines))


class EnrollmentFlow:
    """One enrollment attempt: ENROLL_SECONDS of raw mic audio -> a voice
    profile, collected via the app's always-on recorder tap. Hotkeys are
    paused for the duration (a stray hotkey mid-enrollment would otherwise
    fight the tap / start a real dictation)."""

    def __init__(self, app, done):  # noqa: ANN001
        self._app = app
        self._done = done
        self._chunks: list = []
        self._timer = None
        self._prev_paused = None
        self._finished = False

    def start(self) -> bool:
        app = self._app
        if getattr(app, "state", IDLE) != IDLE:
            return False
        if getattr(app, "_enroll_flow", None) is not None:
            return False
        app._enroll_flow = self
        self._prev_paused = getattr(app, "_paused", False)
        app._paused = True
        app.recorder.add_tap(self._collect)
        app.play_cue("start")
        try:
            app.status_item.title = "Enrolling voice… speak naturally (30s)"
        except Exception as e:
            log(f"  status update failed: {e!r}")
        self._timer = threading.Timer(ENROLL_SECONDS, self._finish)
        self._timer.daemon = True
        self._timer.start()
        return True

    def cancel(self) -> None:
        """User-initiated abort (e.g. closing the app mid-enrollment)."""
        if self._finished:
            return
        self._finished = True
        self._stop_collecting()
        self._restore(False)
        self._app.play_cue("cancel")
        self._done(False, "Enrollment canceled")

    def _collect(self, chunk, is_recording) -> None:  # noqa: ANN001
        self._chunks.append(np.asarray(chunk, dtype="float32"))

    def _stop_collecting(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
        try:
            self._app.recorder.remove_tap(self._collect)
        except Exception as e:
            log(f"  remove_tap failed: {e!r}")
        if getattr(self._app, "_enroll_flow", None) is self:
            self._app._enroll_flow = None

    def _restore(self, ok: bool) -> None:
        self._app._paused = self._prev_paused if self._prev_paused is not None else False

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_collecting()
        app = self._app
        audio = (np.concatenate(self._chunks) if self._chunks
                 else np.zeros(0, dtype="float32"))

        def finish(ok: bool, message: str) -> None:
            self._restore(ok)
            app.play_cue("stop" if ok else "error")
            self._done(ok, message)

        sr = portable.vtt_core.SAMPLE_RATE
        if audio.size < MIN_ENROLL_SECONDS * sr or not portable.vtt_core.contains_speech(audio):
            finish(False, "Didn't hear enough speech — try again (30s of natural talking)")
            return
        try:
            profile_path = portable.VOICE_PROFILE_PATH
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            gate = portable.autodictate.SpeakerGate(str(profile_path))
            gate.enroll(audio)
        except Exception as e:
            log(f"  enroll failed: {e!r}")
            finish(False, f"Enrollment failed: {e}")
            return
        finish(True, "Voice enrolled ✓")


def run_enrollment(app) -> bool:
    """Entry point called by FlowApp.open_enroll (menu item / Settings'
    "Enroll voice…" button)."""

    def done(ok: bool, message: str) -> None:
        try:
            from flow import notify  # flow is already fully loaded by the time
            notify("Voice-To-Text", "Enrollment", message)  # this callback fires
        except Exception as e:
            log(f"  notify failed: {e!r}")
        try:
            if getattr(app, "autodictate", None) is not None:
                app.auto_item.state = 1 if app.autodictate.enabled() else 0
        except Exception as e:
            log(f"  menu refresh failed: {e!r}")
        try:
            refresh = getattr(getattr(app, "settings", None), "_refresh_autodictate", None)
            if callable(refresh):
                refresh()
        except Exception as e:
            log(f"  settings refresh failed: {e!r}")

    flow = EnrollmentFlow(app, done)
    return flow.start()
