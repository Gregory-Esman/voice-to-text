"""Voice-To-Text — Windows desktop GUI (tkinter).

A clickable window with two tabs:
  • Home     — live status, Pause/Resume, Restart, Quit, Clear thread context,
               Start-at-login, and the current hotkeys at a glance.
  • Settings — microphone picker, Dictate/Command hotkey dropdowns, start/stop
               sound toggle, transcription + writing model fields. Save applies
               live (no restart) and persists to %APPDATA%\\Voice-To-Text\\config.toml.

Mirrors the macOS Settings window (flow.py SettingsController) using the same
underlying config. The window runs on the MAIN thread; the tray icon, keyboard
listener, audio stream and HUD each run on their own threads. All cross-thread
calls into the GUI marshal back onto the Tk thread via root.after().
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

# Amber that matches the app icon / HUD bars.
AMBER = "#f5b15c"
DARK = "#16130d"
BG = "#1f1c17"
FG = "#efe7da"
SUBTLE = "#9b9483"

# Friendly label → config token. Only the keys the agent's _KEYMAP / _PRINTABLE
# actually support are offered, so a chosen hotkey always binds.
HOTKEY_CHOICES = [
    ("F9", "f9"),
    ("F10", "f10"),
    ("Right Alt", "alt_r"),
    ("Left Alt", "alt_l"),
    ("Right Ctrl", "ctrl_r"),
    ("AltGr (Right Alt as AltGr)", "alt_gr"),
    ("Backtick / Tilde  `", "tilde"),
    ("Shift + Tilde  ~", "shift+tilde"),
]
_TOKEN_TO_LABEL = {tok: lbl for lbl, tok in HOTKEY_CHOICES}
_LABEL_TO_TOKEN = {lbl: tok for lbl, tok in HOTKEY_CHOICES}


def _hotkey_label(token: str) -> str:
    return _TOKEN_TO_LABEL.get((token or "").strip().lower(), token or "—")


def list_input_devices() -> list[tuple[str, str]]:
    """[(display label, spec)] for the mic picker. spec is what goes in config:
    'default' or a device-name substring."""
    items = [("System default", "default")]
    try:
        import sounddevice as sd
        seen = set()
        for d in sd.query_devices():
            if d.get("max_input_channels", 0) > 0:
                name = d["name"]
                if name and name not in seen:
                    seen.add(name)
                    items.append((name, name))
    except Exception:
        pass
    return items


class AppWindow:
    """The desktop window. Holds a reference to the running VoiceAgent and drives
    it (pause, rebind hotkeys, switch mic, save config, restart, quit)."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.root: tk.Tk | None = None
        self._status_var = None
        self._pause_btn = None
        self._hk_var = None
        self._autostart_var = None
        # settings widgets / vars
        self._mic_var = None
        self._dict_var = None
        self._cmd_var = None
        self._sounds_var = None
        self._stt_model_var = None
        self._cmd_model_var = None
        self._saved_lbl = None
        self._built = False

    # ───────────────────────── lifecycle ─────────────────────────
    def run(self, start_hidden: bool = False) -> None:
        """Build the window and enter the Tk mainloop (MAIN thread)."""
        self.root = tk.Tk()
        self.root.title("Voice-To-Text")
        self.root.configure(bg=BG)
        self.root.geometry("440x640")
        self.root.minsize(440, 640)
        try:
            self.root.iconbitmap(default=self._ico_path())
        except Exception:
            pass
        self._build()
        self._built = True
        # Closing the window hides it to the tray; it does not quit the app.
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.refresh()
        if start_hidden:
            self.root.withdraw()
        self.root.mainloop()

    @staticmethod
    def _ico_path() -> str:
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "assets", "AppIcon.ico")

    # Thread-safe entry points (callable from the tray thread).
    def show(self) -> None:
        if self.root is not None:
            self.root.after(0, self._show_impl)

    def show_settings(self) -> None:
        if self.root is not None:
            self.root.after(0, self._show_settings_impl)

    def hide(self) -> None:
        if self.root is not None:
            self.root.withdraw()

    def notify_state_changed(self) -> None:
        """Called when pause/autostart change from elsewhere (e.g. the tray)."""
        if self.root is not None:
            self.root.after(0, self.refresh)

    def _show_impl(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.refresh()

    def _show_settings_impl(self) -> None:
        self._show_impl()
        try:
            self._nb.select(self._settings_tab)
        except Exception:
            pass

    # ───────────────────────── UI build ─────────────────────────
    def _style(self) -> None:
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=DARK, foreground=SUBTLE,
                     padding=(16, 8), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", BG)],
               foreground=[("selected", AMBER)])
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Sub.TLabel", background=BG, foreground=SUBTLE)
        st.configure("H.TLabel", background=BG, foreground=AMBER,
                     font=("Segoe UI Semibold", 13))
        st.configure("Status.TLabel", background=BG, font=("Segoe UI Semibold", 15))
        st.configure("TButton", background=DARK, foreground=FG, borderwidth=0,
                     padding=(12, 7), focuscolor=BG)
        st.map("TButton", background=[("active", "#2a2620")])
        st.configure("Accent.TButton", background=AMBER, foreground=DARK,
                     font=("Segoe UI Semibold", 11), padding=(12, 9))
        st.map("Accent.TButton", background=[("active", "#ffc16f")])
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TCombobox", fieldbackground=DARK, background=DARK,
                     foreground=FG, arrowcolor=AMBER, padding=4)
        st.configure("TEntry", fieldbackground=DARK, foreground=FG, padding=4)

    def _build(self) -> None:
        self._style()
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True, padx=12, pady=12)
        home = ttk.Frame(self._nb)
        self._settings_tab = ttk.Frame(self._nb)
        self._nb.add(home, text="  Home  ")
        self._nb.add(self._settings_tab, text="  Settings  ")
        self._build_home(home)
        self._build_settings(self._settings_tab)

    def _build_home(self, f) -> None:
        ttk.Label(f, text="Voice-To-Text", style="H.TLabel").pack(anchor="w", pady=(14, 2), padx=16)
        self._status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self._status_var, style="Status.TLabel").pack(anchor="w", padx=16)

        self._hk_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self._hk_var, style="Sub.TLabel").pack(anchor="w", padx=16, pady=(2, 16))

        self._pause_btn = ttk.Button(f, text="Pause", style="Accent.TButton",
                                     command=self._toggle_pause)
        self._pause_btn.pack(fill="x", padx=16, pady=(0, 6))

        row = ttk.Frame(f)
        row.pack(fill="x", padx=16, pady=(6, 4))
        ttk.Button(row, text="Clear thread context",
                   command=self._clear_context).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row, text="Restart",
                   command=self.agent.restart).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self._autostart_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Start automatically at login (in the tray)",
                        variable=self._autostart_var,
                        command=self._toggle_autostart).pack(anchor="w", padx=16, pady=(12, 4))

        ttk.Label(f, text="Closing this window keeps Voice-To-Text running in the\n"
                          "system tray. Use Quit to exit completely.",
                  style="Sub.TLabel", justify="left").pack(anchor="w", padx=16, pady=(8, 8))

        ttk.Button(f, text="Quit Voice-To-Text",
                   command=self.agent.quit).pack(side="bottom", fill="x", padx=16, pady=16)

    def _build_settings(self, f) -> None:
        pad = {"padx": 16, "pady": (6, 0)}

        ttk.Label(f, text="Microphone", style="Sub.TLabel").pack(anchor="w", **pad)
        self._mic_items = list_input_devices()
        self._mic_var = tk.StringVar()
        mic = ttk.Combobox(f, textvariable=self._mic_var, state="readonly",
                           values=[lbl for lbl, _ in self._mic_items])
        mic.pack(fill="x", padx=16, pady=(2, 8))

        row = ttk.Frame(f)
        row.pack(fill="x", **pad)
        col1 = ttk.Frame(row); col1.pack(side="left", expand=True, fill="x", padx=(0, 6))
        col2 = ttk.Frame(row); col2.pack(side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Label(col1, text="Dictate key", style="Sub.TLabel").pack(anchor="w")
        self._dict_var = tk.StringVar()
        ttk.Combobox(col1, textvariable=self._dict_var, state="readonly",
                     values=[lbl for lbl, _ in HOTKEY_CHOICES]).pack(fill="x", pady=(2, 0))
        ttk.Label(col2, text="Auto-Dictate toggle key", style="Sub.TLabel").pack(anchor="w")
        self._cmd_var = tk.StringVar()
        ttk.Combobox(col2, textvariable=self._cmd_var, state="readonly",
                     values=[lbl for lbl, _ in HOTKEY_CHOICES]).pack(fill="x", pady=(2, 0))

        self._sounds_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Play start / stop sounds",
                        variable=self._sounds_var).pack(anchor="w", padx=16, pady=(14, 4))

        # ── Auto-Dictate ──
        ttk.Label(f, text="Auto-Dictate", style="H.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        self._auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Focused text box = live mic (types what you say)",
                        variable=self._auto_var,
                        command=self._toggle_auto_dictate).pack(anchor="w", padx=16, pady=(4, 2))
        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=16, pady=(2, 4))
        self._enroll_status = ttk.Label(row2, text="", style="Sub.TLabel")
        self._enroll_status.pack(side="left")
        self._enroll_btn = ttk.Button(row2, text="Enroll my voice (30 s)",
                                      command=self._start_enroll)
        self._enroll_btn.pack(side="right")
        self._enroll_active = False

        # ── About you (used to spell your name/email right + "type my email") ──
        row3 = ttk.Frame(f)
        row3.pack(fill="x", **pad)
        col1b = ttk.Frame(row3); col1b.pack(side="left", expand=True, fill="x", padx=(0, 6))
        col2b = ttk.Frame(row3); col2b.pack(side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Label(col1b, text="Your name", style="Sub.TLabel").pack(anchor="w")
        self._name_var = tk.StringVar()
        ttk.Entry(col1b, textvariable=self._name_var).pack(fill="x", pady=(2, 0))
        ttk.Label(col2b, text="Your email", style="Sub.TLabel").pack(anchor="w")
        self._email_var = tk.StringVar()
        ttk.Entry(col2b, textvariable=self._email_var).pack(fill="x", pady=(2, 0))

        ttk.Label(f, text="Transcription model", style="Sub.TLabel").pack(anchor="w", **pad)
        self._stt_model_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._stt_model_var).pack(fill="x", padx=16, pady=(2, 8))

        ttk.Label(f, text="Writing model (spoken write / edit commands)", style="Sub.TLabel").pack(anchor="w", **pad)
        self._cmd_model_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._cmd_model_var).pack(fill="x", padx=16, pady=(2, 8))

        bar = ttk.Frame(f)
        bar.pack(fill="x", side="bottom", padx=16, pady=16)
        self._saved_lbl = ttk.Label(bar, text="", style="Sub.TLabel")
        self._saved_lbl.pack(side="left")
        ttk.Button(bar, text="Save", style="Accent.TButton",
                   command=self._save).pack(side="right")

        ttk.Label(f, text="Tip: tap the toggle key to turn Auto-Dictate on/off (a text box\n"
                          "then = live mic). Tap the dictate key to start manual dictation,\n"
                          "tap again to stop. Right Ctrl / Tilde never disturb app menus.",
                  style="Sub.TLabel", justify="left").pack(anchor="w", side="bottom", padx=16)

    # ───────────────────────── refresh from config ─────────────────────────
    def refresh(self) -> None:
        if not self._built:
            return
        cfg = self.agent.cfg
        hk = cfg.get("hotkey", {})
        paused = self.agent.is_paused()
        self._status_var.set("⏸  Paused" if paused else "●  Active — listening")
        self._hk_var.set(f"Dictate: {_hotkey_label(hk.get('dictate_key', 'ctrl_r'))}     "
                         f"Auto toggle: {_hotkey_label(hk.get('toggle_auto_key', 'tilde'))}")
        self._pause_btn.configure(text="Resume" if paused else "Pause")
        try:
            self._autostart_var.set(self.agent.autostart_enabled())
        except Exception:
            pass
        # settings tab
        self._dict_var.set(_hotkey_label(hk.get("dictate_key", "ctrl_r")))
        self._cmd_var.set(_hotkey_label(hk.get("toggle_auto_key", "tilde")))
        self._sounds_var.set(bool(cfg.get("sounds", {}).get("enabled", True)))
        self._stt_model_var.set(cfg.get("transcription", {}).get("model", ""))
        self._cmd_model_var.set(cfg.get("formatting", {}).get("command_model", ""))
        cur_mic = str(cfg.get("audio", {}).get("input_device", "default"))
        label = next((lbl for lbl, spec in self._mic_items if spec == cur_mic), None)
        self._mic_var.set(label or self._mic_items[0][0])
        # auto-dictate
        try:
            self._auto_var.set(bool(self.agent.auto_dictate_on()))
            if not self._enroll_active:
                self._enroll_status.configure(
                    text="Voice enrolled ✓" if self.agent.speaker_enrolled()
                    else "No voice profile yet")
        except Exception:
            pass
        pe = cfg.get("personal", {}) or {}
        self._name_var.set(str(pe.get("name", "")))
        self._email_var.set(str(pe.get("email", "")))

    # ───────────────────────── Auto-Dictate ─────────────────────────
    def _toggle_auto_dictate(self) -> None:
        want = bool(self._auto_var.get())
        ok = False
        try:
            ok = self.agent.set_auto_dictate(want)
        except Exception:
            pass
        if want and not ok:
            self._auto_var.set(False)
            self._enroll_status.configure(text="Enroll your voice first →")

    def _start_enroll(self) -> None:
        if self._enroll_active:
            return
        if not self.agent.begin_enrollment():
            self._enroll_status.configure(text="Mic is busy — try again")
            return
        self._enroll_active = True
        self._enroll_btn.configure(state="disabled")
        self._enroll_left = int(getattr(self.agent, "ENROLL_SECONDS", 30))
        self._enroll_tick()

    def _enroll_tick(self) -> None:
        if self._enroll_left <= 0:
            self._enroll_status.configure(text="Building your voice profile…")
            self.agent.finish_enrollment(
                lambda ok, msg: self.root.after(0, self._enroll_done, ok, msg))
            return
        self._enroll_status.configure(
            text=f"Recording — talk naturally… {self._enroll_left}s")
        self._enroll_left -= 1
        self.root.after(1000, self._enroll_tick)

    def _enroll_done(self, ok: bool, msg: str) -> None:
        self._enroll_active = False
        self._enroll_btn.configure(state="normal")
        self._enroll_status.configure(text=msg)
        if ok:
            self.root.after(3000, self.refresh)

    # ───────────────────────── actions ─────────────────────────
    def _toggle_pause(self) -> None:
        self.agent.set_paused(not self.agent.is_paused())
        self.refresh()

    def _clear_context(self) -> None:
        try:
            self.agent.clear_context()
        except Exception:
            pass

    def _toggle_autostart(self) -> None:
        try:
            self.agent.set_autostart(self._autostart_var.get())
        except Exception:
            pass

    def _save(self) -> None:
        cfg = self.agent.cfg
        dict_tok = _LABEL_TO_TOKEN.get(self._dict_var.get(), "ctrl_r")
        cmd_tok = _LABEL_TO_TOKEN.get(self._cmd_var.get(), "tilde")
        mic_spec = next((spec for lbl, spec in self._mic_items
                         if lbl == self._mic_var.get()), "default")
        # write into the live config
        cfg.setdefault("hotkey", {})
        cfg.setdefault("audio", {})
        cfg.setdefault("sounds", {})
        cfg.setdefault("transcription", {})
        cfg.setdefault("formatting", {})
        mic_changed = str(cfg["audio"].get("input_device", "default")) != mic_spec
        hk_changed = (cfg["hotkey"].get("dictate_key") != dict_tok
                      or cfg["hotkey"].get("toggle_auto_key") != cmd_tok)
        cfg["audio"]["input_device"] = mic_spec
        cfg["sounds"]["enabled"] = bool(self._sounds_var.get())
        cfg["transcription"]["model"] = self._stt_model_var.get().strip()
        cfg["formatting"]["command_model"] = self._cmd_model_var.get().strip()
        # apply live
        self.agent.set_sounds(cfg["sounds"]["enabled"])
        try:
            self.agent.apply_personal(self._name_var.get(), self._email_var.get())
        except Exception:
            pass
        if hk_changed:
            self.agent.apply_hotkeys(dict_tok, cmd_tok)
        if mic_changed:
            self.agent.apply_input_device(mic_spec)
        try:
            self.agent.save_config()
            self._saved_lbl.configure(text="Saved ✓")
        except Exception as e:
            self._saved_lbl.configure(text=f"Save failed: {e}")
        self.refresh()
        if self._saved_lbl is not None:
            self.root.after(2500, lambda: self._saved_lbl.configure(text=""))
