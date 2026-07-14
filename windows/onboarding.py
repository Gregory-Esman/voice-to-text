"""First-run onboarding — a guided welcome flow shown the first time the app
launches after install. Explains what the app does, sets up the user's voice
(which doubles as a mic check), teaches the two keys, lets them try it once, and
leaves them with a cheat-sheet. Runs entirely on the Tk main thread; enrollment
work is marshalled back with root.after(). A marker file records completion so
it only ever appears once (re-openable from Settings)."""

import os
import tkinter as tk
from tkinter import ttk

# Match the main window's palette (kept local to avoid import coupling).
AMBER = "#f5b15c"
DARK = "#16130d"
BG = "#1f1c17"
FG = "#efe7da"
SUBTLE = "#9b9483"
GREEN = "#7fd18a"
CARD = "#26221b"

APP_NAME = "Voice-To-Text"
N_STEPS = 6


def _marker_path() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), APP_NAME, "onboarded")


def is_onboarded() -> bool:
    try:
        return os.path.exists(_marker_path())
    except Exception:
        return False


def mark_onboarded() -> None:
    try:
        p = _marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("done\n")
    except Exception:
        pass


class Onboarding:
    """A modal-ish Toplevel wizard driven by the AppWindow. `on_done(started)` is
    called when it closes (started=True if the app was launched to the tray, so
    the caller can hide the main window afterward)."""

    def __init__(self, app_window, start_hidden: bool, on_done=None) -> None:
        self.app = app_window
        self.agent = app_window.agent
        self.root = app_window.root
        self._start_hidden = start_hidden
        self._on_done = on_done
        self.top = None
        self.step = 0
        self._enroll_active = False
        self._enroll_left = 0
        self._tryit_text = None
        self._tryit_seen = False

    # ── lifecycle ──
    def start(self) -> None:
        self.top = tk.Toplevel(self.root)
        self.top.title("Welcome to Voice-To-Text")
        self.top.configure(bg=BG)
        w, h = 560, 680
        self.top.resizable(False, False)
        try:
            self.top.iconbitmap(default=self.app._ico_path())
        except Exception:
            pass
        # center on the primary screen, keeping it fully on-screen
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        self._w = w
        self._x = max(0, (sw - w) // 2)
        self._y = max(0, min((sh - h) // 2 - 20, sh - h - 40))
        self.top.geometry(f"{w}x{h}+{self._x}+{self._y}")
        self.top.protocol("WM_DELETE_WINDOW", self._finish)
        self.top.attributes("-topmost", True)
        self.top.after(400, lambda: self._safe(self.top.attributes, "-topmost", False))

        # Pack order matters: header pinned top, footer pinned BOTTOM (so the
        # Next button is never clipped), body fills the middle.
        self._header = tk.Frame(self.top, bg=BG)
        self._header.pack(side="top", fill="x", padx=28, pady=(22, 0))
        self._dots = tk.Label(self._header, text="", bg=BG, fg=SUBTLE,
                              font=("Segoe UI", 9))
        self._dots.pack(anchor="e")

        self._footer = tk.Frame(self.top, bg=BG)
        self._footer.pack(side="bottom", fill="x", padx=28, pady=(10, 20))
        tk.Frame(self.top, bg=CARD, height=1).pack(side="bottom", fill="x", padx=28)

        self._body = tk.Frame(self.top, bg=BG)
        self._body.pack(side="top", fill="both", expand=True, padx=28, pady=(4, 0))

        self.top.lift()
        self.top.focus_force()
        self._render()

    def _safe(self, fn, *a):
        try:
            fn(*a)
        except Exception:
            pass

    # ── helpers ──
    def _clear(self, frame) -> None:
        for c in frame.winfo_children():
            c.destroy()

    def _h1(self, parent, text) -> None:
        tk.Label(parent, text=text, bg=BG, fg=AMBER,
                 font=("Segoe UI Semibold", 20), justify="left",
                 wraplength=480).pack(anchor="w", pady=(6, 2))

    def _sub(self, parent, text) -> None:
        tk.Label(parent, text=text, bg=BG, fg=FG, font=("Segoe UI", 12),
                 justify="left", wraplength=480).pack(anchor="w", pady=(0, 10))

    def _bullet(self, parent, icon, title, desc) -> None:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=5, ipady=8, ipadx=4)
        tk.Label(row, text=icon, bg=CARD, fg=AMBER,
                 font=("Segoe UI", 16), width=3).pack(side="left", padx=(8, 4))
        col = tk.Frame(row, bg=CARD)
        col.pack(side="left", fill="x", expand=True, padx=(2, 10))
        tk.Label(col, text=title, bg=CARD, fg=FG,
                 font=("Segoe UI Semibold", 11), justify="left",
                 wraplength=395, anchor="w").pack(anchor="w")
        if desc:
            tk.Label(col, text=desc, bg=CARD, fg=SUBTLE, font=("Segoe UI", 10),
                     justify="left", wraplength=395, anchor="w").pack(anchor="w")

    def _key_cap(self, parent, cap, desc) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=6)
        # fixed-width chip so both keys leave the SAME room for the text (a wide
        # chip like "R-Ctrl" must not squeeze the label into a clipping wrap)
        chip = tk.Label(row, text=cap, bg=DARK, fg=AMBER,
                        font=("Consolas", 12, "bold"), width=7, pady=4)
        chip.pack(side="left", padx=(0, 12))
        tk.Label(row, text=desc, bg=BG, fg=FG, font=("Segoe UI", 11),
                 justify="left", wraplength=360, anchor="w").pack(
                     side="left", fill="x", expand=True)

    def _btn(self, parent, text, cmd, accent=True, side="right"):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=(AMBER if accent else DARK),
                      fg=(DARK if accent else FG),
                      activebackground=("#ffc16f" if accent else "#2a2620"),
                      activeforeground=(DARK if accent else FG),
                      font=("Segoe UI Semibold", 11), bd=0, relief="flat",
                      padx=18, pady=9, cursor="hand2")
        b.pack(side=side, padx=4)
        return b

    def _nav(self, next_text="Next  ›", next_cmd=None, back=True, skip=None):
        self._clear(self._footer)
        if next_cmd is None:
            next_cmd = self._next
        self._btn(self._footer, next_text, next_cmd, accent=True, side="right")
        if back and self.step > 0:
            self._btn(self._footer, "‹  Back", self._back, accent=False, side="left")
        if skip is not None:
            self._btn(self._footer, skip[0], skip[1], accent=False, side="right")

    # ── flow ──
    def _next(self) -> None:
        if self.step < N_STEPS - 1:
            self.step += 1
            self._render()
        else:
            self._finish()

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._render()

    def _render(self) -> None:
        self._dots.config(text=f"Step {self.step + 1} of {N_STEPS}")
        self._safe(self.agent.set_force_arm, False)   # only the try-it step arms our box
        self._clear(self._body)
        (self._step_welcome, self._step_details, self._step_voice,
         self._step_keys, self._step_tryit, self._step_done)[self.step]()
        self.top.after_idle(self._fit_height)

    def _fit_height(self) -> None:
        """Size the window to exactly fit the current step's content (clamped to
        the screen), so nothing is ever clipped whatever the display scaling is."""
        try:
            self.top.update_idletasks()
            need = (self._header.winfo_reqheight() + self._body.winfo_reqheight()
                    + self._footer.winfo_reqheight() + 78)   # paddings + separator + chrome
            sh = self.top.winfo_screenheight()
            h = max(420, min(need, sh - 80))
            y = max(0, min(self._y, sh - h - 48))
            self.top.geometry(f"{self._w}x{h}+{self._x}+{y}")
        except Exception:
            pass

    # ── step 1: what it does ──
    def _step_welcome(self) -> None:
        self._h1(self._body, "\U0001F3A4  Talk, and it types.")
        self._sub(self._body, "Voice-To-Text turns your speech into typed text in "
                              "any app — email, chat, docs, the browser, anywhere.")
        self._bullet(self._body, "⌨", "Dictate into any text box",
                     "Put your cursor in a box, talk, and your words appear.")
        self._bullet(self._body, "⚡", "Hands-free Auto-Dictate",
                     "Turn it on and every text box becomes a live mic — no keys.")
        self._bullet(self._body, "\U0001F4AC", "Speak commands, not just words",
                     "“write a reply saying…”, “send it”, "
                     "“delete the last sentence”.")
        if not self._has_key():
            tk.Label(self._body,
                     text="⚠  No Groq API key found yet — you'll need a free "
                          "one from console.groq.com/keys for transcription.",
                     bg=BG, fg=AMBER, font=("Segoe UI", 9), justify="left",
                     wraplength=500).pack(anchor="w", pady=(12, 0))
        self._nav(next_text="Get started  ›", back=False)

    # ── step 2: your details (name / email / Groq key) ──
    def _field(self, label, var, show=None, hint=None):
        tk.Label(self._body, text=label, bg=BG, fg=SUBTLE,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(10, 2))
        e = tk.Entry(self._body, textvariable=var, bg=DARK, fg=FG,
                     insertbackground=AMBER, font=("Segoe UI", 11), bd=0,
                     relief="flat", show=(show or ""))
        e.pack(fill="x", ipady=6)
        if hint:
            tk.Label(self._body, text=hint, bg=BG, fg=SUBTLE, font=("Segoe UI", 9),
                     justify="left", wraplength=475).pack(anchor="w", pady=(2, 0))
        return e

    def _link(self, parent, text, url):
        lbl = tk.Label(parent, text=text, bg=BG, fg=AMBER,
                       font=("Segoe UI", 10, "underline"), cursor="hand2")
        lbl.pack(anchor="w", pady=(6, 0))

        def _open(_e):
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        lbl.bind("<Button-1>", _open)

    def _step_details(self) -> None:
        self._h1(self._body, "Your details")
        self._sub(self._body, "All optional — nothing here is required. Your name "
                              "and email just power a couple of conveniences; set "
                              "what you like, or skip and add them later in Settings.")
        pe = self.agent.cfg.get("personal", {}) or {}
        self._name_var = tk.StringVar(value=str(pe.get("name", "")))
        self._email_var = tk.StringVar(value=str(pe.get("email", "")))
        self._key_var = tk.StringVar()
        self._field("Your name  (optional)", self._name_var,
                    hint="Why: so the app spells your name correctly in what you "
                         "dictate, and so “type my name” works.")
        self._field("Your email  (optional)", self._email_var,
                    hint="Why: so “type my email” inserts it, and speaking your email "
                         "aloud (“jamie at gmail dot com”) becomes your real address.")
        self._field("Groq API key", self._key_var, show="•",
                    hint="Needed to transcribe your speech. Free, starts with “gsk_”.")
        self._link(self._body, "Get a free Groq key  →", "https://console.groq.com/keys")
        if self._has_key():
            tk.Label(self._body,
                     text="✓  A key is already saved — leave blank to keep it.",
                     bg=BG, fg=GREEN, font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))
        self._nav(next_text="Next  ›", next_cmd=self._save_details,
                  skip=("Skip", self._next))

    def _save_details(self) -> None:
        try:
            self.agent.apply_personal(self._name_var.get(), self._email_var.get())
        except Exception:
            pass
        key = self._key_var.get().strip()
        if key:
            try:
                self.agent.set_groq_key(key)     # → Credential Manager; never logged
            except Exception:
                pass
        self._next()

    # ── step 3: enroll voice (also the mic check) ──
    def _step_voice(self) -> None:
        self._h1(self._body, "Set up your voice")
        self._sub(self._body, "Auto-Dictate only types when it hears YOU — not a "
                              "video, the TV, or someone else on a call. Record about "
                              "30 seconds so it learns your voice. Just talk naturally "
                              "(read anything out loud).")
        enrolled = self._enrolled()
        self._enroll_status = tk.Label(
            self._body,
            text=("Voice already enrolled ✓" if enrolled
                  else "Tap Record and talk until it finishes."),
            bg=BG, fg=(GREEN if enrolled else SUBTLE), font=("Segoe UI", 11))
        self._enroll_status.pack(anchor="w", pady=(8, 10))
        self._enroll_btn = self._btn_inline(
            self._body, ("Re-record" if enrolled else "●  Record 30s"),
            self._start_enroll)
        skip = None if enrolled else ("Skip for now", self._skip_voice)
        self._nav(next_cmd=self._next if (enrolled or self._enrolled()) else self._need_voice,
                  skip=skip)

    def _btn_inline(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, bg=AMBER, fg=DARK,
                      activebackground="#ffc16f", activeforeground=DARK,
                      font=("Segoe UI Semibold", 11), bd=0, relief="flat",
                      padx=18, pady=9, cursor="hand2")
        b.pack(anchor="w")
        return b

    def _need_voice(self) -> None:
        if self._enrolled():
            self._next()
        else:
            self._enroll_status.config(
                text="Record your voice first, or Skip for now.", fg=AMBER)

    def _skip_voice(self) -> None:
        # leave Auto-Dictate off; they can enroll later from Settings
        self._next()

    def _start_enroll(self) -> None:
        if self._enroll_active:
            return
        if not self.agent.begin_enrollment():
            self._enroll_status.config(text="Mic is busy — try again in a moment.",
                                       fg=AMBER)
            return
        self._enroll_active = True
        self._enroll_btn.config(state="disabled")
        self._enroll_left = int(getattr(self.agent, "ENROLL_SECONDS", 30))
        self._enroll_tick()

    def _enroll_tick(self) -> None:
        if not self._enroll_active:
            return
        if self._enroll_left <= 0:
            self._enroll_status.config(text="Building your voice profile…",
                                       fg=SUBTLE)
            self.agent.finish_enrollment(
                lambda ok, msg: self.root.after(0, self._enroll_done, ok, msg))
            return
        self._enroll_status.config(
            text=f"Listening — keep talking…  {self._enroll_left}s", fg=AMBER)
        self._enroll_left -= 1
        self.top.after(1000, self._enroll_tick)

    def _enroll_done(self, ok: bool, msg: str) -> None:
        self._enroll_active = False
        try:
            self._enroll_btn.config(state="normal", text="Re-record")
        except Exception:
            pass
        if ok:
            self._enroll_status.config(text="Voice enrolled ✓", fg=GREEN)
            try:
                self.agent.set_auto_dictate(True)   # turn Auto-Dictate on for them
            except Exception:
                pass
            self._nav(next_cmd=self._next,
                      skip=None)                     # Next now just advances
        else:
            self._enroll_status.config(
                text=(msg or "That didn't work — try again.") +
                     "\n(If it never hears you, check Windows mic access.)",
                fg=AMBER)

    # ── step 3: the two keys ──
    def _step_keys(self) -> None:
        self._h1(self._body, "Your two keys")
        self._sub(self._body, "Everything runs off two keys plus the tray icon.")
        self._key_cap(self._body, "`",
                      "Tap the tilde key (top-left, above Tab) to turn "
                      "Auto-Dictate ON or off. When it's on, click any text box "
                      "and just talk — a chip shows “Listening”.")
        self._key_cap(self._body, "R-Ctrl",
                      "Tap Right Ctrl to start manual dictation, tap again to "
                      "stop and paste. Best when you have a lot to say.")
        self._bullet(self._body, "\U0001F514", "Tray icon (bottom-right, by the clock)",
                     "Right-click it for Settings, Pause, and Quit.")
        self._nav()

    # ── step 4: try it ──
    def _step_tryit(self) -> None:
        self._h1(self._body, "Try it now")
        if self._enrolled():
            # turn Auto-Dictate on and force-arm this box (it isn't UIA-editable),
            # so the headline feature works right here in the practice box
            self._safe(self.agent.set_auto_dictate, True)
            self._safe(self.agent.set_force_arm, True)
            self._sub(self._body, "Auto-Dictate is ON. Click the box and just start "
                                  "talking — no keys. (Or tap Right Ctrl to dictate "
                                  "manually instead.)")
        else:
            self._sub(self._body, "Click the box, tap Right Ctrl, say a few words, "
                                  "then tap Right Ctrl again to see them appear.")
        self._tryit_text = tk.Text(self._body, height=4, bg=DARK, fg=FG,
                                   insertbackground=AMBER, font=("Segoe UI", 12),
                                   bd=0, relief="flat", wrap="word", padx=10, pady=8)
        self._tryit_text.pack(fill="x", pady=(2, 6))
        self._tryit_hint = tk.Label(self._body,
                                    text="Waiting for your voice…", bg=BG,
                                    fg=SUBTLE, font=("Segoe UI", 10))
        self._tryit_hint.pack(anchor="w")
        tk.Label(self._body,
                 text="This is exactly how it works everywhere — focus any text box "
                      "and talk.",
                 bg=BG, fg=SUBTLE, font=("Segoe UI", 9), justify="left",
                 wraplength=480).pack(anchor="w", pady=(8, 0))
        self.top.after(300, lambda: self._safe(self._tryit_text.focus_force))
        self._tryit_seen = False
        self._watch_tryit()
        self._nav(skip=("Skip", self._next))

    def _watch_tryit(self) -> None:
        if self._tryit_text is None or not self._tryit_text.winfo_exists():
            return
        try:
            if self._tryit_text.get("1.0", "end").strip():
                if not self._tryit_seen:
                    self._tryit_seen = True
                    self._tryit_hint.config(text="Nice — it works! ✓", fg=GREEN)
                    self._play("tick")
        except Exception:
            pass
        self.top.after(400, self._watch_tryit)

    # ── step 5: done + cheat-sheet ──
    def _mode_block(self, color, title, desc, items) -> None:
        wrap = tk.Frame(self._body, bg=BG)
        wrap.pack(fill="x", pady=(10, 0))
        tk.Frame(wrap, bg=color, width=4).pack(side="left", fill="y")
        col = tk.Frame(wrap, bg=BG)
        col.pack(side="left", fill="x", expand=True, padx=(12, 0))
        tk.Label(col, text=title, bg=BG, fg=color,
                 font=("Segoe UI Semibold", 13), anchor="w", justify="left",
                 wraplength=460).pack(anchor="w")
        tk.Label(col, text=desc, bg=BG, fg=FG, font=("Segoe UI", 10), anchor="w",
                 justify="left", wraplength=460).pack(anchor="w", pady=(1, 0))
        for it in items:
            tk.Label(col, text="•  " + it, bg=BG, fg=SUBTLE, font=("Segoe UI", 10),
                     anchor="w", justify="left", wraplength=445).pack(
                         anchor="w", pady=(3, 0))

    def _step_done(self) -> None:
        self._h1(self._body, "\U0001F389  You're all set")
        self._sub(self._body, "Two ways to use your voice — knowing the difference "
                              "is the whole trick:")
        # MODE 1 — plain dictation (green)
        self._mode_block(
            GREEN, "\U0001F5E3  Just talk  →  it gets typed",
            "Say anything and it's typed out word for word. This is the default — "
            "most of the time you just talk.", [])
        # MODE 2 — commands (amber) — visibly distinct
        self._mode_block(
            AMBER, "⚡  Say a command  →  it acts instead",
            "A few phrases are recognized as commands and DONE for you, not typed:",
            ["“write a reply saying…” / “draft an email about…”   → writes it for you",
             "“delete the last sentence” · “scratch that” · “delete everything”   → edits",
             "“send it”  → presses Enter        “type my email”  → your address"])
        tk.Label(self._body,
                 text="Keys:   `  toggle Auto-Dictate      R-Ctrl  manual dictation",
                 bg=BG, fg=SUBTLE, font=("Segoe UI", 9)).pack(anchor="w", pady=(14, 0))
        tk.Label(self._body,
                 text="Reopen this guide, or change keys / mic / your voice, from the "
                      "tray icon.",
                 bg=BG, fg=SUBTLE, font=("Segoe UI", 9), justify="left",
                 wraplength=480).pack(anchor="w", pady=(3, 0))
        self._nav(next_text="Finish", next_cmd=self._finish)

    # ── finish ──
    def _finish(self) -> None:
        mark_onboarded()
        self._safe(self.agent.set_force_arm, False)   # stop force-arming our box
        try:
            if self._enroll_active:
                self.agent.cancel_enrollment()
        except Exception:
            pass
        self._safe(self.top.grab_release)
        self._safe(self.top.destroy)
        self.top = None
        if self._on_done:
            self._safe(self._on_done, self._start_hidden)

    # ── small agent shims ──
    def _has_key(self) -> bool:
        try:
            import vtt_core as core
            t = self.agent.cfg["transcription"]
            return bool(core._resolve_api_key(t["api_key_env"], t["api_key_file"]))
        except Exception:
            return True   # don't nag if we can't tell

    def _enrolled(self) -> bool:
        try:
            return bool(self.agent.speaker_enrolled())
        except Exception:
            return False

    def _play(self, kind: str) -> None:
        try:
            self.agent._play(kind)
        except Exception:
            pass
