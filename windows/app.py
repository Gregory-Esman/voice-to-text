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
    "audio": {"input_device": "default"},
    "sounds": {"enabled": True},
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
        self._gui = None               # set in run(); the desktop window
        # resolve trigger keys → tap-detection + suppression tables
        self._listener = None
        hk = cfg["hotkey"]
        self._bind_triggers(hk.get("dictate_key", "f9"), hk.get("command_key", "f10"))
        self._icon = None

    # ───────────── audio ─────────────
    def _on_audio(self, indata, frames, t, status):  # sounddevice callback
        if not self._capturing:        # stream stays warm; only collect frames
            return                      # while a capture is active (instant start)
        self._frames.append(indata[:, 0].copy())
        lvl = float(np.sqrt(np.mean(indata[:, 0] ** 2)) * 6.0)
        self.hud.set_level(lvl)

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
        except Exception as e:
            self._stream = None
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
            print("[audio] no mic stream")
            return
        # Immediate feedback FIRST — beep + start capturing from the warm mic +
        # show the HUD — before the slower window/selection probing, so the sound
        # lands the instant you tap rather than a beat later.
        self._play("start")
        self._frames = []                  # collect from the warm stream — instant
        self._capturing = True
        self.hud.show()
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
            self._play("error")
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
        if self._paused:                  # hotkeys off until resumed
            return
        if self.state == IDLE:
            self._begin(mode)
        elif self.state == mode:
            self._end()
        # different mode while recording → ignore

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
            mode = DICTATE if tok == self._k_dictate else COMMAND
            # off the hook/callback thread so the keyboard hook stays snappy
            threading.Thread(target=self._toggle, args=(mode,), daemon=True).start()

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

    def _bind_triggers(self, dictate_key: str, command_key: str) -> None:
        """(Re)build the tap-detection + suppression tables from two hotkey names.
        Printable triggers are grouped by VK so one key can hold both a plain and
        a Shift+ variant (dictate=tilde + command=shift+tilde)."""
        self._k_dictate, dvk, dshift = _resolve_trigger(dictate_key, keyboard.Key.f9)
        self._k_command, cvk, cshift = _resolve_trigger(command_key, keyboard.Key.f10)
        self._suppress_vk: dict = {}     # vk -> [(shift_required, token), ...]
        if dvk is not None:
            self._suppress_vk.setdefault(dvk, []).append((dshift, self._k_dictate))
        if cvk is not None:
            self._suppress_vk.setdefault(cvk, []).append((cshift, self._k_command))
        self._vk_press_tok: dict = {}    # vk -> token chosen at key-down
        # tap detection per trigger token: {token: {"down":bool,"t":float,"mod":bool}}
        self._trig = {self._k_dictate: {"down": False, "t": 0.0, "mod": False},
                      self._k_command: {"down": False, "t": 0.0, "mod": False}}

    def apply_hotkeys(self, dictate_key: str, command_key: str) -> None:
        """Rebind the dictate/command hotkeys live — no restart."""
        self.cfg["hotkey"]["dictate_key"] = dictate_key
        self.cfg["hotkey"]["command_key"] = command_key
        self._bind_triggers(dictate_key, command_key)
        self._restart_listener()
        self._sync_ui()

    def apply_input_device(self, spec: str) -> None:
        """Switch the microphone live by reopening the warm stream."""
        self.cfg.setdefault("audio", {})["input_device"] = spec
        self._close_stream()
        self._open_stream()

    def save_config(self) -> None:
        """Persist the editable settings to %APPDATA%\\Voice-To-Text\\config.toml.
        Only the user-facing keys are written; everything else deep-merges from
        DEFAULT_CFG at load, so the file stays small and readable."""
        def q(s):  # TOML basic-string quote
            return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
        c = self.cfg
        hk, au, so = c.get("hotkey", {}), c.get("audio", {}), c.get("sounds", {})
        tr, fo = c.get("transcription", {}), c.get("formatting", {})
        lines = [
            "# Voice-To-Text (Windows) config — written by the Settings window.",
            "# Other keys (API endpoints/keys) fall back to built-in defaults.",
            "",
            "[hotkey]",
            f"dictate_key = {q(hk.get('dictate_key', 'f9'))}",
            f"command_key = {q(hk.get('command_key', 'f10'))}",
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
        self._listener = self._make_listener()
        self._listener.start()
        import pystray
        self._icon = pystray.Icon(
            APP_NAME, self._make_icon_image(), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Open Voice-To-Text", self._tray_open, default=True),
                pystray.MenuItem("Settings…", self._tray_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda i: f"Dictate: {self.cfg['hotkey']['dictate_key']}  ·  "
                              f"Write: {self.cfg['hotkey']['command_key']}",
                    None, enabled=False),
                pystray.Menu.SEPARATOR,
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

    def _toggle_pause(self, icon, item) -> None:
        self.set_paused(not self._paused)

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


def main() -> None:
    if "--selftest" in sys.argv[1:]:
        # Verify the frozen bundle's heavy/native deps import — catches PyInstaller
        # under-collection (e.g. numpy._core._multiarray_umath). Touches no mic,
        # tray, or GUI; prints "selftest ok" and exits 0 so a build can be smoke-
        # tested headlessly.
        import numpy, sounddevice                       # noqa: F401
        from numpy._core import _multiarray_umath       # noqa: F401
        import win32clipboard, uiautomation, PIL.Image  # noqa: F401
        # Windowed exe has no console, so write a marker file instead of printing.
        try:
            marker = os.path.join(os.environ.get("TEMP", "."), "vtt_selftest.txt")
            with open(marker, "w", encoding="utf-8") as f:
                f.write("selftest ok numpy=" + numpy.__version__ + "\n")
        except Exception:
            pass
        return
    # Crash logging: faulthandler catches hard/native crashes (COM, Tcl) and the
    # try/except catches Python exceptions on the main thread — both to a file we
    # can read after the fact.
    try:
        import faulthandler
        faulthandler.enable(open(_crashlog_path(), "a", encoding="utf-8"))
    except Exception:
        pass
    cfg = load_config()
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
