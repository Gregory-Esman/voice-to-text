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
def play(kind: str) -> None:
    """kind in {start, stop, cancel, error}."""
    try:
        import winsound  # type: ignore
        m = {
            "start": winsound.MB_OK,
            "stop": winsound.MB_OK,
            "cancel": winsound.MB_ICONASTERISK,
            "error": winsound.MB_ICONHAND,
        }
        winsound.MessageBeep(m.get(kind, winsound.MB_OK))
    except Exception:
        pass


# ─────────────────────────── autostart ────────────────────────────────
def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"


def set_autostart(enable: bool, python: str, script: str) -> None:
    cmd = _startup_dir() / "VoiceToText.cmd"
    if enable:
        _startup_dir().mkdir(parents=True, exist_ok=True)
        # pythonw.exe so it launches with no console window
        pyw = python.replace("python.exe", "pythonw.exe")
        cmd.write_text(f'@echo off\r\nstart "" "{pyw}" "{script}"\r\n')
    else:
        try:
            cmd.unlink()
        except FileNotFoundError:
            pass


def autostart_enabled() -> bool:
    return (_startup_dir() / "VoiceToText.cmd").exists()


# ─────────────────────── recording HUD (Phase 1) ──────────────────────
class RecordingHUD:
    """A small always-on-top 'recording' pill with a live level bar, drawn with
    tkinter on its own thread. show()/hide()/set_level() are called from other
    threads; the tk loop polls the shared flags on a timer (tk isn't
    thread-safe, so we never touch widgets off-thread)."""

    W, H = 260, 56

    def __init__(self) -> None:
        self._level = 0.0
        self._visible = False
        self._alive = False
        self._root = None
        self._canvas = None
        self._thread = None

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

    def _run(self) -> None:
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.94)
        except Exception:
            pass
        sw = root.winfo_screenwidth()
        root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+120")
        root.configure(bg="#14110c")
        canvas = tk.Canvas(root, width=self.W, height=self.H, bg="#14110c",
                           highlightthickness=0)
        canvas.pack()
        root.withdraw()
        self._root, self._canvas = root, canvas
        self._tick()
        root.mainloop()

    def _tick(self) -> None:
        import random
        root, canvas = self._root, self._canvas
        if root is None:
            return
        try:
            if self._visible:
                root.deiconify()
                root.lift()
                canvas.delete("all")
                bars = 13
                gap = 6
                bw = 4
                total = bars * bw + (bars - 1) * gap
                x0 = (self.W - total) / 2
                mid = self.H / 2
                lvl = self._level
                for i in range(bars):
                    jitter = 0.35 + 0.65 * abs(random.random())
                    h = max(3, (6 + lvl * 38) * jitter)
                    x = x0 + i * (bw + gap)
                    canvas.create_rectangle(
                        x, mid - h / 2, x + bw, mid + h / 2,
                        fill="#f5b15c", outline="")
            else:
                root.withdraw()
        except Exception:
            pass
        root.after(33, self._tick)


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
