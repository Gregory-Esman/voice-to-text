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
import threading
from pathlib import Path


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
_SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
_SOUND_FILES = {"start": "start.wav", "stop": "stop.wav",
                "cancel": "cancel.wav", "error": "error.wav"}
_SOUND_CACHE: dict = {}
_SOUNDS_LOADED = False


def _load_sounds() -> None:
    """Preload the cue WAVs into memory so the first cue is instant (no file I/O
    on the hot path) — mirrors the macOS app's NSSound preload. The sounds are
    synthesized equivalents of the mac cues: a bright 'tink' start, a soft 'pop'
    stop, a hollow 'bottle' cancel, a low 'basso' error."""
    global _SOUNDS_LOADED
    _SOUNDS_LOADED = True
    for kind, fn in _SOUND_FILES.items():
        try:
            with open(os.path.join(_SOUND_DIR, fn), "rb") as f:
                _SOUND_CACHE[kind] = f.read()
        except Exception:
            pass


def _play_mem(data: bytes) -> None:
    try:
        import winsound  # type: ignore
        winsound.PlaySound(data, winsound.SND_MEMORY)   # sync — runs on its own thread
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
