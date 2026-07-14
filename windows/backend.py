"""Windows OS-integration layer for Voice-To-Text (online / Groq mode).

Mirrors what flow.py does on macOS, using Windows APIs:
  • clipboard get/set + paste (Ctrl+V)        — pywin32 / pyperclip + pynput
  • sounds                                     — winsound
  • autostart at login                         — Startup-folder .cmd
  • recording HUD (frameless, always-on-top)   — tkinter            [Phase 1]
  • screen-context + focused app via UI Automation — uiautomation   [Phase 2]

All Windows-only imports are inside functions so this module imports anywhere
(handy for syntax-checking / testing on non-Windows). Pairs with vtt_core.py
(portable brain) and app.py (agent loop).
"""
from __future__ import annotations

import os
import sys
import logging
import threading
from pathlib import Path

_LOG = logging.getLogger("vtt")


# ───────────────────────── clipboard + paste ──────────────────────────
def clipboard_get() -> str:
    try:
        import win32clipboard  # type: ignore
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
        except Exception:
            return ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        try:
            import pyperclip  # type: ignore
            return pyperclip.paste() or ""
        except Exception:
            return ""


def clipboard_set(text: str) -> None:
    try:
        import win32clipboard  # type: ignore
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text or "", win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
    except Exception:
        try:
            import pyperclip  # type: ignore
            pyperclip.copy(text or "")
        except Exception:
            pass


_kbd = None


def _keyboard():
    global _kbd
    if _kbd is None:
        from pynput.keyboard import Controller
        _kbd = Controller()
    return _kbd


def paste_into_focused_app() -> None:
    """Paste the clipboard into whatever app has focus (Ctrl+V)."""
    import time
    from pynput.keyboard import Key
    kb = _keyboard()
    time.sleep(0.03)
    kb.press(Key.ctrl); kb.press("v"); kb.release("v"); kb.release(Key.ctrl)


def send_backspaces(n: int) -> None:
    """Delete the last n characters in the focused control (Auto-Dictate
    "scratch that"). Backspace works everywhere; Ctrl+Z doesn't."""
    import time
    from pynput.keyboard import Key
    kb = _keyboard()
    time.sleep(0.03)
    for _ in range(max(0, int(n))):
        kb.press(Key.backspace); kb.release(Key.backspace)


def send_ctrl_backspaces(n: int) -> None:
    """Ctrl+Backspace ×n — delete previous word(s) in apps that support it.
    Fallback for voice word-deletion when we didn't type the text ourselves."""
    import time
    from pynput.keyboard import Key
    kb = _keyboard()
    time.sleep(0.03)
    kb.press(Key.ctrl)
    try:
        for _ in range(max(0, int(n))):
            kb.press(Key.backspace); kb.release(Key.backspace)
    finally:
        kb.release(Key.ctrl)


def send_enter() -> None:
    """Press Enter in the focused control (Auto-Dictate "send it")."""
    import time
    from pynput.keyboard import Key
    kb = _keyboard()
    time.sleep(0.03)
    kb.press(Key.enter); kb.release(Key.enter)


def copy_selection() -> tuple[str | None, str | None]:
    """Return (selected_text or None, previous_clipboard). Saves the clipboard,
    sends Ctrl+C, and reads what landed — so command mode can edit a selection
    and restore the user's clipboard afterward. None ⇒ nothing was selected."""
    import time
    from pynput.keyboard import Key
    prev = clipboard_get()
    sentinel = "\x00__vtt_no_sel__\x00"
    clipboard_set(sentinel)
    kb = _keyboard()
    kb.press(Key.ctrl); kb.press("c"); kb.release("c"); kb.release(Key.ctrl)
    time.sleep(0.12)
    got = clipboard_get()
    if got and got != sentinel:
        return got, prev
    clipboard_set(prev)  # nothing selected — restore immediately
    return None, prev


# ───────────────────────────── sounds ─────────────────────────────────
# Soft chimes synthesized in-memory with numpy (no external files) — a gentle
# rising blip on start, a softer falling blip on stop, in the spirit of Wispr
# Flow. Rendered once into WAV bytes so the hot path is just a SND_MEMORY play.
_SOUND_CACHE: dict = {}
_SOUNDS_LOADED = False
_SR = 44100


def _tone(freq, dur, vol=0.26, attack=0.006, decay=9.0, harmonic=0.12):
    """A soft sine note (with a touch of octave for warmth), quick attack + gentle
    exponential decay — returns a float32 waveform in [-1, 1]."""
    import numpy as np
    n = max(1, int(_SR * dur))
    t = np.arange(n) / _SR
    w = np.sin(2 * np.pi * freq * t) + harmonic * np.sin(2 * np.pi * 2 * freq * t)
    env = np.exp(-t * decay)
    a = int(_SR * attack)
    if a > 0:
        env[:a] *= np.linspace(0.0, 1.0, a)
    return w * env * vol


def _wav_bytes(segments):
    import io, wave
    import numpy as np
    sig = np.concatenate(segments) if segments else np.zeros(1, dtype=float)
    tail = min(len(sig), int(_SR * 0.008))   # short fade-out to avoid an end click
    if tail > 0:
        sig[-tail:] *= np.linspace(1.0, 0.0, tail)
    pcm = (np.clip(sig, -1.0, 1.0) * 32767.0).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(_SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synth_cues() -> dict:
    """Two-note chimes: start rises (D5->A5), stop falls (G5->C5), cancel dips,
    error is a low, gentle two-tone — none of them the harsh Windows beep."""
    return {
        "start":  _wav_bytes([_tone(587.33, 0.055), _tone(880.00, 0.11)]),
        "stop":   _wav_bytes([_tone(783.99, 0.055), _tone(523.25, 0.12)]),
        "cancel": _wav_bytes([_tone(523.25, 0.05), _tone(392.00, 0.11)]),
        "error":  _wav_bytes([_tone(392.00, 0.07, vol=0.28), _tone(311.13, 0.15, vol=0.28)]),
        # Auto-Dictate: a barely-there tick when text lands (per-utterance, so it
        # must be far quieter than the start/stop chimes).
        "tick":   _wav_bytes([_tone(1174.66, 0.035, vol=0.10, decay=30.0)]),
    }


def _sound_dir() -> str:
    """Where the bundled cue WAVs live — the PyInstaller extract dir when frozen,
    else the source tree next to this file."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "sounds")


def _load_sounds() -> None:
    """Load the bundled cue WAVs (start/stop/cancel/error) into memory once. Any
    cue whose file is missing is filled in by the numpy synth fallback, so cues
    always exist even if the WAVs didn't ship."""
    global _SOUNDS_LOADED
    _SOUNDS_LOADED = True
    d = _sound_dir()
    for kind, fn in {"start": "start.wav", "stop": "stop.wav",
                     "cancel": "cancel.wav", "error": "error.wav"}.items():
        try:
            with open(os.path.join(d, fn), "rb") as f:
                _SOUND_CACHE[kind] = f.read()
        except Exception:
            pass
    # fill any gaps with synthesized cues (cues with no bundled WAV — e.g.
    # "tick" — always come from the synth)
    try:
        for k, v in _synth_cues().items():
            _SOUND_CACHE.setdefault(k, v)
    except Exception:
        pass


def _play_mem(data: bytes) -> None:
    try:
        import winsound  # type: ignore
        # SND_NODEFAULT: if the buffer can't play, stay silent (never fall back to
        # the Windows default/error sound).
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    except Exception:
        pass


def play(kind: str) -> None:
    """Play a cue (kind in {start, stop, cancel, error}) from a preloaded WAV in
    memory. winsound can't play SND_MEMORY asynchronously, so we play it (sync)
    on a throwaway thread — instant and non-blocking. Falls back to a beep."""
    try:
        import winsound  # type: ignore
        if not _SOUNDS_LOADED:
            _load_sounds()
        data = _SOUND_CACHE.get(kind)
        _LOG.info("cue: %s (%s)", kind, "wav" if data is not None else "beep-fallback")
        if data is not None:
            threading.Thread(target=_play_mem, args=(data,), daemon=True).start()
        else:
            winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass


# ─────────────────────────── autostart ────────────────────────────────
def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"


def set_autostart(enable: bool, python: str, script: str) -> None:
    cmd = _startup_dir() / "VoiceToText.cmd"
    if enable:
        _startup_dir().mkdir(parents=True, exist_ok=True)
        # pythonw.exe so it launches with no console window; --tray starts it
        # minimized to the system tray (no window popping up at every login).
        pyw = python.replace("python.exe", "pythonw.exe")
        cmd.write_text(f'@echo off\r\nstart "" "{pyw}" "{script}" --tray\r\n')
    else:
        try:
            cmd.unlink()
        except FileNotFoundError:
            pass


def autostart_enabled() -> bool:
    return (_startup_dir() / "VoiceToText.cmd").exists()


# ─────────────────────── recording HUD (Phase 1) ──────────────────────
class RecordingHUD:
    """A small always-on-top recording pill with a smooth, audio-reactive bar
    visualizer (Wispr-Flow style): a rounded dark pill (true rounded corners via
    Windows' transparent-color key) with eased amber bars that flow as a
    traveling wave, scale with your voice, and gently 'breathe' in silence.

    Drawn with tkinter on its own thread. show()/hide()/set_level() are called
    from other threads; the tk loop polls the shared state on a timer (tk isn't
    thread-safe, so widgets are only touched on the tk thread)."""

    W, H = 190, 50
    BARS = 22
    KEY = "#ff00ff"            # transparent-color key → real rounded corners
    PILL = "#16130d"           # pill fill (near-black warm)
    BAR = (245, 177, 92)       # amber, matches the app icon

    def __init__(self) -> None:
        self._level = 0.0
        self._slevel = 0.0           # smoothed level
        self._visible = False
        self._alive = False
        self._root = None
        self._canvas = None
        self._thread = None
        self._phase = 0.0
        self._cur = [0.04] * self.BARS   # eased per-bar heights (0..1)
        self._key_ok = True

    def start(self) -> None:
        if self._alive:
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def show(self) -> None:
        self.start()
        self._visible = True

    def hide(self) -> None:
        self._visible = False

    def set_level(self, lvl: float) -> None:
        self._level = max(0.0, min(1.0, float(lvl)))

    @staticmethod
    def _rrect(canvas, x0, y0, x1, y1, r, fill):
        """Filled rounded rectangle from 4 corner ovals + 2 crossing rects."""
        canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
        canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill)
        for cx, cy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
            canvas.create_oval(cx, cy, cx + 2 * r, cy + 2 * r, fill=fill, outline=fill)

    def _run(self) -> None:
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", self.KEY)  # Windows: key → clear
        except Exception:
            self._key_ok = False
        bg = self.KEY if self._key_ok else self.PILL
        sw = root.winfo_screenwidth()
        root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+96")
        root.configure(bg=bg)
        canvas = tk.Canvas(root, width=self.W, height=self.H, bg=bg,
                           highlightthickness=0, bd=0)
        canvas.pack()
        root.withdraw()
        self._root, self._canvas = root, canvas
        self._tick()
        root.mainloop()

    def _tick(self) -> None:
        import math
        root, canvas = self._root, self._canvas
        if root is None:
            return
        try:
            if self._visible:
                root.deiconify()
                root.lift()
                canvas.delete("all")
                self._rrect(canvas, 1, 1, self.W - 1, self.H - 1,
                            self.H // 2 - 1, self.PILL)
                self._slevel += (self._level - self._slevel) * 0.35
                self._phase += 0.22
                bars, bw, gap = self.BARS, 3.0, 5.0
                total = bars * bw + (bars - 1) * gap
                x0 = (self.W - total) / 2.0
                mid = self.H / 2.0
                maxh = self.H * 0.64
                r, g, b = self.BAR
                for i in range(bars):
                    u = i / (bars - 1)
                    env = math.sin(math.pi * u) ** 0.8        # taller in the middle
                    wave = 0.55 + 0.45 * math.sin(self._phase + u * 7.5)
                    breath = 0.06 + 0.05 * math.sin(self._phase * 0.5 + u * 3.0)
                    target = env * (breath + self._slevel * wave * 1.4)
                    target = max(0.04, min(1.0, target))
                    self._cur[i] += (target - self._cur[i]) * 0.30   # easing
                    h = self._cur[i] * maxh
                    x = x0 + i * (bw + gap) + bw / 2.0
                    sh = 0.62 + 0.38 * self._cur[i]                  # brighter when taller
                    col = f"#{int(r * sh):02x}{int(g * sh):02x}{int(b * sh):02x}"
                    canvas.create_line(x, mid - h / 2, x, mid + h / 2,
                                       fill=col, width=bw, capstyle="round")
            else:
                root.withdraw()
        except Exception:
            pass
        root.after(16, self._tick)


def _active_workarea():
    """(left, top, right, bottom) work area (taskbar excluded) of the monitor
    holding the foreground window — so overlays land in the corner of the screen
    the user is actually working on, not always the primary. None if unknown."""
    try:
        import win32gui  # type: ignore
        import win32api  # type: ignore
        hwnd = win32gui.GetForegroundWindow()
        mon = win32api.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
        return win32api.GetMonitorInfo(mon)["Work"]
    except Exception:
        return None


class AutoChip:
    """Auto-Dictate state chip: a small always-on-top pill in the bottom-right
    corner of the monitor holding the focused text box. Gray dot + "Listening" while armed (an editable box is focused),
    amber dot + "Hearing you" while an utterance is being captured, hidden when
    cold. Same threading pattern as RecordingHUD: show()/hide() just set shared
    state; only the tk thread touches widgets."""

    W, H = 152, 30
    KEY = "#ff00ff"
    PILL = "#16130d"
    GRAY = "#9b9483"
    AMBER = "#f5b15c"
    GREEN = "#7fd18a"

    def __init__(self) -> None:
        self._state = None           # None (hidden) | "armed" | "capturing"
        self._toast = None           # (text, color, deadline) | None — transient
        self._alive = False
        self._root = None
        self._canvas = None
        self._key_ok = True

    def show(self, state: str) -> None:
        self._state = state
        if not self._alive:
            self._alive = True
            threading.Thread(target=self._run, daemon=True).start()

    def hide(self) -> None:
        self._state = None

    def toast(self, text: str, color: str = None, secs: float = 1.4) -> None:
        """Flash a transient message pill (e.g. "Auto-Dictate ON") for `secs`,
        overriding the armed/capturing state; afterwards the chip reverts to
        whatever the underlying state is (armed → "Listening", else hidden)."""
        import time
        self._toast = (text, color or self.GRAY, time.time() + secs)
        if not self._alive:
            self._alive = True
            threading.Thread(target=self._run, daemon=True).start()

    def _place(self, root) -> None:
        """Anchor the pill to the bottom-right corner of the monitor holding the
        focused window (falls back to the primary screen). Recomputed each time
        the chip appears, so it follows the screen you're typing on."""
        wa = _active_workarea()
        if wa:
            l, t, r, b = wa
            x, y = r - self.W - 24, b - self.H - 24
        else:
            x = root.winfo_screenwidth() - self.W - 24
            y = root.winfo_screenheight() - self.H - 84
        root.geometry(f"{self.W}x{self.H}+{int(x)}+{int(y)}")

    def _run(self) -> None:
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", self.KEY)
        except Exception:
            self._key_ok = False
        bg = self.KEY if self._key_ok else self.PILL
        self._place(root)
        root.configure(bg=bg)
        canvas = tk.Canvas(root, width=self.W, height=self.H, bg=bg,
                           highlightthickness=0, bd=0)
        canvas.pack()
        root.withdraw()
        self._root, self._canvas = root, canvas
        self._shown = None
        self._tick()
        root.mainloop()

    def _tick(self) -> None:
        root, canvas = self._root, self._canvas
        if root is None:
            return
        try:
            import time
            toast = self._toast
            if toast is not None and time.time() < toast[2]:
                desc = (toast[0], toast[1])          # transient message wins
            else:
                if toast is not None:                # expired → clear + revert
                    self._toast = None
                st = self._state
                if st is None:
                    desc = None
                else:
                    col = self.AMBER if st == "capturing" else self.GRAY
                    txt = "Hearing you" if st == "capturing" else "Listening"
                    desc = (txt, col)
            if desc != self._shown:
                was_hidden = self._shown is None
                self._shown = desc
                if desc is None:
                    root.withdraw()
                else:
                    if was_hidden:
                        self._place(root)   # follow the active monitor on show
                    txt, col = desc
                    canvas.delete("all")
                    RecordingHUD._rrect(canvas, 1, 1, self.W - 1, self.H - 1,
                                        self.H // 2 - 1, self.PILL)
                    canvas.create_oval(12, self.H // 2 - 4, 20, self.H // 2 + 4,
                                       fill=col, outline=col)
                    canvas.create_text(28, self.H // 2, text=txt, anchor="w",
                                       fill=col, font=("Segoe UI", 9))
                    root.deiconify()
                    root.lift()
        except Exception:
            pass
        root.after(80, self._tick)


# ───────────────────── voice app actions (Auto-Dictate) ───────────────
def activate_window(query: str) -> bool:
    """Bring the best-matching top-level window to the foreground ("switch to
    slack"). Matches exe name first, window title second. The Alt tap lifts
    Windows' foreground-change restriction for background processes."""
    import win32gui  # type: ignore
    import win32process  # type: ignore
    q = (query or "").strip().lower()
    if not q:
        return False
    found: list = []          # (score, hwnd)

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        title = win32gui.GetWindowText(h)
        if not title:
            return
        exe = ""
        try:
            import win32api  # type: ignore
            import win32con  # type: ignore
            _, pid = win32process.GetWindowThreadProcessId(h)
            ph = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION
                                      | win32con.PROCESS_VM_READ, False, pid)
            exe = os.path.basename(win32process.GetModuleFileNameEx(ph, 0)).lower()
            win32api.CloseHandle(ph)
        except Exception:
            pass
        stem = exe[:-4] if exe.endswith(".exe") else exe
        if stem == q:
            score = 4
        elif stem.startswith(q):
            score = 3
        elif q in stem:
            score = 2
        elif q in title.lower():
            score = 1
        else:
            return
        found.append((score, h))

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    if not found:
        return False
    found.sort(key=lambda x: -x[0])
    hwnd = found[0][1]
    try:
        from pynput.keyboard import Key
        kb = _keyboard()
        kb.press(Key.alt); kb.release(Key.alt)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)          # SW_RESTORE
        win32gui.SetForegroundWindow(hwnd)
        _LOG.info("action: activated '%s' for '%s'",
                  win32gui.GetWindowText(hwnd)[:40], q)
        return True
    except Exception:
        _LOG.exception("action: activate '%s' failed", q)
        return False


def launch_app(query: str) -> bool:
    """Fallback for "open X" when no window matched: let the shell's `start`
    resolve the name (works for registered apps: chrome, notepad, ...)."""
    q = (query or "").strip()
    if not q:
        return False
    try:
        import subprocess
        subprocess.Popen(["cmd", "/c", "start", "", q],
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        _LOG.info("action: launched '%s'", q)
        return True
    except Exception:
        _LOG.exception("action: launch '%s' failed", q)
        return False


# ─────────────── screen context via UI Automation (Phase 2) ───────────
def frontmost_app() -> tuple[str, str, str]:
    """(name, key, title) of the focused window. `key` (the process exe name) is
    the stable per-app key used for thread-context stitching."""
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        exe = ""
        try:
            import win32api  # type: ignore
            import win32con  # type: ignore
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION
                                     | win32con.PROCESS_VM_READ, False, pid)
            exe = os.path.basename(win32process.GetModuleFileNameEx(h, 0))
        except Exception:
            pass
        return (exe, exe or "?", title)
    except Exception:
        return ("", "?", "")


def read_window_context(limit: int = 12000, max_nodes: int = 4000) -> str:
    """Read visible text from the focused window via UI Automation, so a
    'reply to this' draft / a thread reply has the on-screen conversation.
    Best-effort: returns '' if UIA is unavailable or the app exposes nothing
    (some Electron apps are sparse — that's fine)."""
    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        return ""
    try:
        top = auto.GetForegroundControl()
        if top is None:
            return ""
        texts: list[str] = []
        seen: set[str] = set()
        nodes = [0]

        def walk(node, depth: int = 0) -> None:
            if depth > 40 or nodes[0] > max_nodes:
                return
            if sum(len(t) for t in texts) > limit:
                return
            nodes[0] += 1
            try:
                nm = (node.Name or "").strip()
                if len(nm) >= 2 and nm not in seen:
                    seen.add(nm)
                    texts.append(nm)
            except Exception:
                pass
            try:
                for ch in node.GetChildren():
                    walk(ch, depth + 1)
            except Exception:
                pass

        walk(top)
        return "\n".join(texts)[:limit]
    except Exception:
        return ""
