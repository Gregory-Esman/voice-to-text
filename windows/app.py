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
import sys
import threading
import time

import numpy as np
import sounddevice as sd
from pynput import keyboard

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vtt_core as core          # noqa: E402
import backend as os_back        # noqa: E402

try:
    import tomllib               # Python 3.11+
except ModuleNotFoundError:      # pragma: no cover
    tomllib = None

SAMPLE_RATE = core.SAMPLE_RATE
APP_NAME = "Voice-To-Text"

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
    "hotkey": {"dictate_key": "alt_r", "command_key": "alt_l"},
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

IDLE, DICTATE, COMMAND = "idle", "dictate", "command"


class VoiceAgent:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.state = IDLE
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self.hud = os_back.RecordingHUD()
        self.ctx_log = core.ThreadContextLog()
        # command-mode capture
        self._sel = None
        self._prev_clip = None
        self._ctx = ""
        self._app = ("", "?", "")
        # resolve trigger keys
        hk = cfg["hotkey"]
        self._k_dictate = _KEYMAP.get(hk.get("dictate_key", "alt_r"), keyboard.Key.alt_r)
        self._k_command = _KEYMAP.get(hk.get("command_key", "alt_l"), keyboard.Key.alt_l)
        # tap detection per trigger: {key: {"down":bool,"t":float,"mod":bool}}
        self._trig = {self._k_dictate: {"down": False, "t": 0.0, "mod": False},
                      self._k_command: {"down": False, "t": 0.0, "mod": False}}
        self._icon = None

    # ───────────── audio ─────────────
    def _on_audio(self, indata, frames, t, status):  # sounddevice callback
        self._frames.append(indata[:, 0].copy())
        lvl = float(np.sqrt(np.mean(indata[:, 0] ** 2)) * 6.0)
        self.hud.set_level(lvl)

    def _start_stream(self) -> None:
        self._frames = []
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                      dtype="float32", callback=self._on_audio)
        self._stream.start()

    def _stop_stream(self) -> np.ndarray:
        try:
            if self._stream:
                self._stream.stop(); self._stream.close()
        finally:
            self._stream = None
        return (np.concatenate(self._frames) if self._frames
                else np.zeros(0, dtype="float32"))

    # ───────────── recording lifecycle ─────────────
    def _begin(self, mode: str) -> None:
        with self._lock:
            if self.state != IDLE:
                return
            self.state = mode
        self._app = os_back.frontmost_app()
        if mode == COMMAND:
            # selection → edit it; capture screen context (Phase 2) off-thread
            self._sel, self._prev_clip = os_back.copy_selection()
            self._ctx = ""
            if self._sel is None:
                def grab():
                    raw = os_back.read_window_context()
                    self._ctx = self.ctx_log.stitch(raw, self._app[1], time.time())
                threading.Thread(target=grab, daemon=True).start()
        try:
            self._start_stream()
        except Exception as e:
            self.state = IDLE
            os_back.play("error")
            print(f"[audio] could not start: {e}")
            return
        os_back.play("start")
        self.hud.show()

    def _end(self) -> None:
        with self._lock:
            mode = self.state
            if mode == IDLE:
                return
            self.state = IDLE
        audio = self._stop_stream()
        os_back.play("stop")
        self.hud.hide()
        threading.Thread(target=self._process, args=(mode, audio), daemon=True).start()

    # ───────────── transcribe → write/paste ─────────────
    def _stt_key(self) -> str:
        t = self.cfg["transcription"]
        return core._resolve_api_key(t["api_key_env"], t["api_key_file"])

    def _process(self, mode: str, audio: np.ndarray) -> None:
        try:
            if not core.contains_speech(audio):
                return
            t = self.cfg["transcription"]
            text = (core.transcribe_remote(audio, t["base_url"], t["model"],
                                           self._stt_key(), t.get("language", ""),
                                           t.get("vocabulary", "")).get("text") or "").strip()
            text = core.collapse_repeats(text)
            if mode == DICTATE:
                if not core.has_lexical_content(text) or core.is_hallucination(text):
                    return
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
            os_back.play("error")
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

    # ───────────── hotkeys (tap detection) ─────────────
    def _toggle(self, mode: str) -> None:
        if self.state == IDLE:
            self._begin(mode)
        elif self.state == mode:
            self._end()
        # different mode while recording → ignore

    def _on_press(self, key) -> None:
        if key in self._trig:
            st = self._trig[key]
            if not st["down"]:
                st.update(down=True, t=time.time(), mod=False)
        else:
            for st in self._trig.values():       # a real key during a hold = modifier use
                if st["down"]:
                    st["mod"] = True

    def _on_release(self, key) -> None:
        st = self._trig.get(key)
        if not st or not st["down"]:
            return
        held = time.time() - st["t"]
        st["down"] = False
        if not st["mod"] and held < 0.6:          # a genuine tap
            self._toggle(DICTATE if key is self._k_dictate else COMMAND)

    # ───────────── tray ─────────────
    def _make_icon_image(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(245, 177, 92, 255))
        d.rounded_rectangle((27, 18, 37, 40), radius=5, fill=(26, 20, 10, 255))
        d.rectangle((31, 40, 33, 48), fill=(26, 20, 10, 255))
        return img

    def run(self) -> None:
        listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        listener.start()
        import pystray
        self._icon = pystray.Icon(
            APP_NAME, self._make_icon_image(), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda i: f"Dictate: {self.cfg['hotkey']['dictate_key']}  ·  "
                              f"Write: {self.cfg['hotkey']['command_key']}",
                    None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Clear thread context",
                                 lambda i, it: self.ctx_log.clear()),
                pystray.MenuItem("Start at login",
                                 self._toggle_autostart,
                                 checked=lambda i: os_back.autostart_enabled()),
                pystray.MenuItem("Restart", lambda i, it: self.restart()),
                pystray.MenuItem("Quit", self._quit),
            ),
        )
        self._icon.run()

    def _toggle_autostart(self, icon, item) -> None:
        os_back.set_autostart(not os_back.autostart_enabled(),
                              sys.executable, os.path.abspath(__file__))

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
            if self._icon:
                self._icon.stop()
        finally:
            os._exit(0)


def main() -> None:
    cfg = load_config()
    if not core._resolve_api_key(cfg["transcription"]["api_key_env"],
                                 cfg["transcription"]["api_key_file"]):
        print("No Groq API key found. Set GROQ_API_KEY, or store it in Windows "
              "Credential Manager under service 'voice-to-text', account 'groq_key'. "
              "See windows\\config.example.toml.")
    VoiceAgent(cfg).run()


if __name__ == "__main__":
    main()
