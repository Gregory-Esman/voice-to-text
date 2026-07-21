"""Auto-Dictate engine for macOS — a focused editable text box becomes a live
mic (see windows/AUTO-DICTATE-BRIEF.md for the original design). This is a
faithful Mac port of windows/app.py's `_auto_process`/`_auto_command`/
`_on_focus_change`/`_on_audio` gating onto flow.py's own plumbing:
  • Endpointing, speaker verification, and all spoken-command matching are
    reused VERBATIM from portable.autodictate (never copied) — Endpointer,
    SpeakerGate, special_of, is_command, is_maybe_command, action_of,
    delete_of, chars_to_delete, is_clear_all, snippet_of, build_fixers,
    apply_fixers, is_prompt_echo, is_noise.
  • Only genuinely Mac-specific plumbing is new here: focus-watching via the
    Accessibility API (FocusWatcherMac), the floating status chip
    (AutoChipMac), and pynput-based keystroke senders.

Keep AppKit/ApplicationServices OUT of module scope — everything that touches
them is imported lazily inside the method that needs it, so this module (and
its tests) can be imported headless.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

import numpy as np
from pynput.keyboard import Controller as _KeyController
from pynput.keyboard import Key as _Key

import portable
from portable import autodictate as ad
from portable import vtt_core as core

SAMPLE_RATE = 16_000

EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
TERMINAL_BUNDLES = {"com.apple.terminal", "com.googlecode.iterm2",
                    "dev.warp.warp-stable", "com.github.wez.wezterm",
                    "net.kovidgoyal.kitty", "org.alacritty", "co.zeit.hyper"}

# A completed utterance queued for the worker thread, used to unblock
# shutdown() cleanly (queue.Queue has no native "stop" signal).
_SHUTDOWN = object()

_kb = _KeyController()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ───────────────────────── focus classification (pure) ─────────────────────────
def classify_focus(role, subrole, settable_value, focused_pid, own_pid,
                   bundle_id, excluded, elem_hash=None):
    """Mac analog of windows autodictate.FocusWatcher._classify. PURE — no AX
    calls, so it's fully unit-testable. Returns (editable: bool,
    box_id: tuple|None, desc: str). Fails COLD on anything unrecognized.

    Order (mirrors windows _classify):
      own pid → cold (never arm our own menu-bar/settings windows)
      AXSecureTextField as role OR subrole → cold ALWAYS (passwords)
      bundle id substring-matches an excluded entry → cold
      bundle id is a known terminal → armed, id=(pid, "terminal")
      role is one of EDITABLE_ROLES → armed
      else a writable AXValue (settable_value) → armed (rich web composers /
        contenteditables — the Mac analog of Windows' writable ValuePattern)
      else → cold
    """
    role = role or ""
    subrole = subrole or ""
    bid = (bundle_id or "").lower()
    if focused_pid == own_pid:
        return (False, None, "own-app")
    if "AXSecureTextField" in (role, subrole):
        return (False, None, "secure-field")
    for ex in (excluded or []):
        ex = str(ex).strip().lower()
        if ex and ex in bid:
            return (False, None, f"excluded:{bundle_id}")
    if bid in TERMINAL_BUNDLES:
        return (True, (focused_pid, "terminal"), f"terminal:{bundle_id}")
    if role in EDITABLE_ROLES:
        return (True, (focused_pid, role, elem_hash), f"{role}")
    if settable_value:
        return (True, (focused_pid, "value", role, subrole),
                f"settable:{role}/{subrole}")
    return (False, None, f"cold:{role}/{subrole}")


# ───────────────────────── focus watching ─────────────────────────
class FocusWatcherMac:
    """Watches macOS keyboard focus system-wide. on_change(editable, box_id,
    desc) fires when focus moves to a different control or the editable state
    flips. Dedicated daemon thread with its own CFRunLoop: an AXObserver on
    the frontmost app's pid watches kAXFocusedUIElementChangedNotification,
    re-attached whenever the frontmost app changes (NSWorkspace's
    DidActivateApplication notification). A 0.5s poll of the systemwide
    AXFocusedUIElement is layered on top as a fallback, since Chrome/Electron
    notifications are flaky. 120ms debounce after any wake so a burst of
    events settles before classifying. ANY AX error is treated as cold —
    this thread must never crash the app."""

    POLL_INTERVAL = 0.5
    DEBOUNCE = 0.12

    def __init__(self, on_change) -> None:
        self._on_change = on_change
        self._last = None            # (editable, box_id)
        self._started = False
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()
        self._own_pid = os.getpid()
        self.excluded_apps: set = set()   # lowercased bundle-id substrings

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_evt.clear()
        threading.Thread(target=self._run, name="vtt-auto-focus-mac",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake_evt.set()
        self._started = False

    def poke(self) -> None:
        """Force a re-classification now (e.g. right after toggling on)."""
        self._wake_evt.set()

    # -- background thread --
    def _run(self) -> None:
        have_ax = True
        try:
            from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification
            from ApplicationServices import (
                AXObserverCreate, AXObserverAddNotification,
                AXObserverGetRunLoopSource, AXUIElementCreateApplication,
                kAXFocusedUIElementChangedNotification,
            )
            from CoreFoundation import (CFRunLoopAddSource, CFRunLoopRemoveSource,
                                        CFRunLoopGetCurrent, kCFRunLoopDefaultMode)
        except Exception as e:
            have_ax = False
            log(f"auto: AX observer unavailable ({e}) — 0.5s poll only")

        if have_ax:
            attached = {"observer": None, "source": None}

            def _ax_cb(observer, element, notification, refcon):  # noqa: ANN001
                self._wake_evt.set()

            def _detach() -> None:
                if attached["observer"] is not None and attached["source"] is not None:
                    try:
                        CFRunLoopRemoveSource(CFRunLoopGetCurrent(), attached["source"],
                                              kCFRunLoopDefaultMode)
                    except Exception:
                        pass
                attached["observer"] = attached["source"] = None

            def _attach() -> None:
                _detach()
                try:
                    app = NSWorkspace.sharedWorkspace().frontmostApplication()
                    if app is None:
                        return
                    pid = app.processIdentifier()
                    err, observer = AXObserverCreate(pid, _ax_cb, None)
                    if err != 0 or observer is None:
                        return
                    ax_app = AXUIElementCreateApplication(pid)
                    AXObserverAddNotification(observer, ax_app,
                                              kAXFocusedUIElementChangedNotification, None)
                    src = AXObserverGetRunLoopSource(observer)
                    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopDefaultMode)
                    attached["observer"], attached["source"] = observer, src
                except Exception:
                    log("auto: AX observer attach failed for this app — poll fallback only")

            def _on_activate(note) -> None:  # noqa: ANN001
                self._wake_evt.set()
                _attach()

            try:
                NSWorkspace.sharedWorkspace().notificationCenter() \
                    .addObserverForName_object_queue_usingBlock_(
                        NSWorkspaceDidActivateApplicationNotification, None, None,
                        _on_activate)
            except Exception:
                log("auto: activation-notification hook failed — reattach via polling only")
            _attach()

        while not self._stop_evt.is_set():
            self._poll_tick()

    def _poll_tick(self) -> None:
        """One 0.5s poll + 120ms debounce cycle. Runs the CFRunLoop briefly
        (so AX observer callbacks/notifications get delivered) when AppKit is
        available, else a plain sleep."""
        try:
            from Foundation import NSDate, NSRunLoop
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(self.POLL_INTERVAL))
        except Exception:
            self._wake_evt.wait(self.POLL_INTERVAL)
        if self._stop_evt.is_set():
            return
        self._wake_evt.clear()
        time.sleep(self.DEBOUNCE)
        if self._wake_evt.is_set():
            return              # a newer event arrived — let it win
        try:
            self._classify_and_report()
        except Exception:
            log("auto: focus classify failed — treating as cold")
            self._report(False, None, "classify-error")

    def _classify_and_report(self) -> None:
        from AppKit import NSWorkspace
        from ApplicationServices import (
            AXUIElementCopyAttributeValue, AXUIElementCreateSystemWide,
            AXUIElementGetPid, AXUIElementIsAttributeSettable,
        )
        system = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyAttributeValue(system, "AXFocusedUIElement", None)
        if err != 0 or el is None:
            self._report(False, None, "no-focus")
            return

        def _attr(name, default=""):
            try:
                e2, v = AXUIElementCopyAttributeValue(el, name, None)
                return v if e2 == 0 and v is not None else default
            except Exception:
                return default

        role = str(_attr("AXRole", ""))
        subrole = str(_attr("AXSubrole", ""))
        try:
            e3, settable = AXUIElementIsAttributeSettable(el, "AXValue", None)
            settable_value = bool(settable) if e3 == 0 else False
        except Exception:
            settable_value = False
        try:
            e4, pid = AXUIElementGetPid(el)
            pid = int(pid) if e4 == 0 else -1
        except Exception:
            pid = -1
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        bundle_id = (app.bundleIdentifier() or "") if app is not None else ""
        try:
            elem_hash = hash(el)
        except Exception:
            elem_hash = None
        editable, box_id, desc = classify_focus(
            role, subrole, settable_value, pid, self._own_pid, bundle_id,
            self.excluded_apps, elem_hash)
        self._report(editable, box_id, f"{desc} pid={pid} bundle={bundle_id}")

    def _report(self, editable: bool, box_id, desc: str) -> None:
        state = (editable, box_id)
        if state != self._last:
            self._last = state
            try:
                self._on_change(editable, box_id, desc)
            except Exception:
                log("auto: on_change callback failed")


# ───────────────────────── status chip (HUD) ─────────────────────────
class AutoChipMac:
    """Small floating status pill — the Mac analog of windows/backend.py's
    AutoChip. Copies flow.py's PillPanel pattern (nonactivating NSPanel at
    NSStatusWindowLevel, canJoinAllSpaces|stationary|fullScreenAuxiliary),
    positioned bottom-right of the active screen. Every AppKit call is routed
    through AppHelper.callAfter so this is safe to drive from any thread."""

    GRAY = "#9b9483"
    AMBER = "#f5b15c"
    GREEN = "#7fd18a"

    def __init__(self) -> None:
        self._panel = None
        self._label = None
        self._toast_timer = None

    def show(self, state: str) -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._show_main, state)

    def hide(self) -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._hide_main)

    def toast(self, text: str, color: str | None = None, secs: float = 1.4) -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._toast_main, text, color, secs)

    # -- main thread only below --
    def _colors(self):
        from AppKit import NSColor

        def _hex(h):
            h = h.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)
        return _hex(self.GRAY), _hex(self.AMBER), _hex(self.GREEN)

    def _build(self) -> None:
        from AppKit import (
            NSBackingStoreBuffered, NSColor, NSFont, NSMakeRect, NSPanel,
            NSScreen, NSStatusWindowLevel, NSTextField,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
            NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
        )
        w, h = 180, 34
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.85))
        panel.setHasShadow_(True)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 8, w - 20, h - 16))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setTextColor_(NSColor.whiteColor())
        label.setFont_(NSFont.systemFontOfSize_(12))
        panel.setContentView_(label)
        self._panel, self._label = panel, label

    def _active_screen(self):
        from AppKit import NSEvent, NSScreen
        try:
            loc = NSEvent.mouseLocation()
            for s in NSScreen.screens():
                f = s.frame()
                if (f.origin.x <= loc.x <= f.origin.x + f.size.width
                        and f.origin.y <= loc.y <= f.origin.y + f.size.height):
                    return s
        except Exception:
            pass
        return NSScreen.mainScreen()

    def _reposition(self) -> None:
        scr = self._active_screen().frame()
        w, h = self._panel.frame().size.width, self._panel.frame().size.height
        x = scr.origin.x + scr.size.width - w - 24.0
        y = scr.origin.y + 24.0
        self._panel.setFrameOrigin_((x, y))

    def _show_main(self, state: str) -> None:
        if self._panel is None:
            self._build()
        gray, amber, _green = self._colors()
        text = "Hearing you" if state == "capturing" else "Listening"
        dot = "● "
        self._label.setStringValue_(dot + text)
        self._label.setTextColor_(amber if state == "capturing" else gray)
        self._reposition()
        self._panel.orderFrontRegardless()

    def _hide_main(self) -> None:
        if self._panel is not None:
            self._panel.orderOut_(None)

    def _toast_main(self, text: str, color: str | None, secs: float) -> None:
        if self._panel is None:
            self._build()
        from AppKit import NSColor
        if self._toast_timer is not None:
            self._toast_timer.invalidate()
            self._toast_timer = None
        self._label.setStringValue_(text)
        self._label.setTextColor_(NSColor.whiteColor() if not color else
                                  self._hex_color(color))
        self._reposition()
        self._panel.orderFrontRegardless()

        def _hide_later():
            self._hide_main()
        try:
            from Foundation import NSTimer
            self._toast_timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    secs, self, "_toastElapsed:", None, False))
        except Exception:
            threading.Timer(secs, lambda: self.hide()).start()

    def _toastElapsed_(self, _timer):  # noqa: N802
        self._hide_main()

    def _hex_color(self, h: str):
        from AppKit import NSColor
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


# ───────────────────────── keystroke senders ─────────────────────────
def send_backspaces(n: int) -> None:
    """Tap Backspace n times, ~8ms apart — plain per-character deletes (the
    Mac analog of Windows' send_backspaces)."""
    for _ in range(max(0, int(n))):
        _kb.press(_Key.backspace)
        _kb.release(_Key.backspace)
        time.sleep(0.008)


def send_word_backspaces(n: int) -> None:
    """Option+Backspace n times — mac word-delete, used for untracked text
    ("remove the last word" in a box we never typed into ourselves). The Mac
    analog of Windows' Ctrl+Backspace."""
    for _ in range(max(0, int(n))):
        _kb.press(_Key.alt)
        _kb.press(_Key.backspace)
        _kb.release(_Key.backspace)
        _kb.release(_Key.alt)
        time.sleep(0.008)


def send_enter() -> None:
    _kb.press(_Key.enter)
    _kb.release(_Key.enter)


def activate_app(query: str) -> bool:
    """Bring a running app whose name substring-matches `query` (case-
    insensitive) to the front. NSWorkspace lookup — the Mac analog of
    Windows' window-title substring search."""
    q = (query or "").strip().lower()
    if not q:
        return False
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSWorkspace
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = (app.localizedName() or "").lower()
            if q in name:
                return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
    except Exception:
        return False
    return False


def launch_app(query: str) -> bool:
    try:
        return subprocess.run(["open", "-a", query],
                              capture_output=True).returncode == 0
    except Exception:
        return False


# ───────────────────────── controller ─────────────────────────
class AutoDictateController:
    """Owns the whole Auto-Dictate pipeline on the FlowApp side: endpointing
    off the always-on mic tap, speaker verification, focus-armed dispatch,
    and the status chip. `watcher`/`chip`/`gate`/`endpointer` are injectable
    (real objects by default) so tests can run this headless with fakes."""

    def __init__(self, app, watcher=None, chip=None, gate=None, endpointer=None) -> None:
        self.app = app
        ad_cfg = app.cfg.get("auto_dictate", {})
        self.endpointer = endpointer or ad.Endpointer(
            silence_ms=int(ad_cfg.get("silence_ms", 700)),
            min_speech_ms=int(ad_cfg.get("min_speech_ms", 180)),
            start_rms=float(ad_cfg.get("start_rms", 0.014)),
            end_rms=float(ad_cfg.get("end_rms", 0.008)))
        self.gate = gate or ad.SpeakerGate(
            str(portable.VOICE_PROFILE_PATH),
            threshold=float(ad_cfg.get("similarity", 0.60)),
            adapt=bool(ad_cfg.get("adapt", True)))
        self.watcher = watcher or FocusWatcherMac(self._on_focus_change)
        self.chip = chip or AutoChipMac()

        self._armed = False
        self._armed_id = None
        self._speaking = False
        self._enrolling = False        # Lane C flips this during GUI enrollment
        self._auto_last: dict = {}     # box id -> {"text": ..., "last": ...}
        self._queue: queue.Queue = queue.Queue()
        self._send_in_terminal = bool(ad_cfg.get("send_in_terminal", False))
        excl = set()
        for x in (ad_cfg.get("exclude_apps") or []):
            e = str(x).strip().lower()
            if e:
                excl.add(e)
        self.watcher.excluded_apps = excl
        self._rebuild_fixers()

        # LoopbackMonitor seam — deferred fast-follow plugs in here. Always
        # "not self audio" until that lane lands.
        self.is_self_audio = lambda audio, t0, t1: (False, 0.0)

        self._enabled = False
        try:
            app.recorder.add_tap(self.on_frame)
        except Exception:
            log("auto: could not attach mic tap")
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="vtt-auto-mac", daemon=True)
        self._worker.start()

        want_on = bool(ad_cfg.get("enabled", False))
        if want_on and self.gate.enrolled():
            self._enabled = True
            self.watcher.start()
            self.watcher.poke()
            threading.Thread(target=self.gate.preload, daemon=True).start()
        elif want_on:
            # config says on, but the voice profile is gone/never made —
            # never arm blind (windows app.py ~L1094-1104 parity).
            self._enabled = False
            log("auto: enabled in config but no voice profile — off")

    # ── config ──
    def _rebuild_fixers(self) -> None:
        cfg = self.app.cfg
        personal_raw = cfg.get("personal") or {}
        self._personal = {str(k).lower(): str(v) for k, v in personal_raw.items()
                          if isinstance(v, (str, int)) and str(v).strip()}
        self._fixers = ad.build_fixers(self._personal, cfg.get("replacements") or {})

    # ── enable/disable ──
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> bool:
        """Enable/disable Auto-Dictate. Enabling requires a voice profile."""
        on = bool(on)
        if on and not self.gate.enrolled():
            log("auto: enable refused — no voice profile yet")
            return False
        self._enabled = on
        if on:
            self.watcher.start()
            self.watcher.poke()
            threading.Thread(target=self.gate.preload, daemon=True).start()
        else:
            self.watcher.stop()
            self.chip.hide()
            self.endpointer.reset()
            self._speaking = False
        try:
            self.app._persist("enabled", on, section="auto_dictate")
        except Exception:
            log("auto: persist failed")
        log(f"auto: {'ENABLED' if on else 'disabled'}")
        return True

    def toggle(self) -> None:
        if not self._enabled:
            if not self.set_enabled(True):
                self.app.play_cue("error")
                self.chip.toast("Enroll your voice first (menu: Enroll voice…)")
                return
            self.app.play_cue("start")
            self.chip.toast("Auto-Dictate ON")
        else:
            self.set_enabled(False)
            self.app.play_cue("stop")
            self.chip.toast("Auto-Dictate OFF")

    # ── focus/audio hooks ──
    def _on_focus_change(self, editable: bool, box_id, desc: str) -> None:
        self._armed = bool(editable)
        self._armed_id = box_id
        self.endpointer.reset()
        self._speaking = False
        log(f"auto: {'ARMED' if editable else 'disarmed'} — {desc}")
        if self._enabled and not getattr(self.app, "_paused", False):
            if editable:
                self.chip.show("armed")
            else:
                self.chip.hide()

    def on_frame(self, block, recording: bool) -> None:
        if (recording or self._enrolling or getattr(self.app, "_paused", False)
                or not self._enabled or not self._armed):
            self.endpointer.reset()
            self._speaking = False
            return
        utt = self.endpointer.feed(block)
        if self.endpointer.speaking != self._speaking:
            self._speaking = self.endpointer.speaking
            self.chip.show("capturing" if self._speaking else "armed")
        if utt is not None:
            self._queue.put((utt, self._armed_id, self.endpointer.last_span))

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                return
            utt, cid, span = item
            try:
                self._process_utterance(utt, cid, span)
            except Exception as e:
                self.app.play_cue("error")
                log(f"auto: process error: {e}")

    def shutdown(self) -> None:
        self.watcher.stop()
        self.chip.hide()
        try:
            self.app.recorder.remove_tap(self.on_frame)
        except Exception:
            pass
        self._queue.put(_SHUTDOWN)

    # ── the pipeline (faithful port of windows app.py's _auto_process) ──
    def _process_utterance(self, audio: np.ndarray, cid, span) -> None:
        if not self._enabled or getattr(self.app, "_paused", False):
            return
        t0 = time.time()
        # speech check on head + middle 3s slices, not the whole clip (O(duration)
        # scan) — background noise can arm the endpointer before speech starts.
        win = int(3.0 * SAMPLE_RATE)
        mid = max(0, audio.size // 2 - win // 2)
        if not (core.contains_speech(audio[:win])
                or (audio.size > win and core.contains_speech(audio[mid:mid + win]))):
            log(f"auto: no speech ({len(audio) / SAMPLE_RATE:.1f}s)")
            return
        # the machine hearing itself? (video/music through the speakers)
        echo, corr = self.is_self_audio(audio, span[0], span[1])
        if echo:
            log(f"auto: DROPPED as speaker echo (corr {corr:.2f}, "
                f"{len(audio) / SAMPLE_RATE:.1f}s)")
            return
        ok, score = self.gate.accept(audio)      # LOCAL — nothing uploaded
        t1 = time.time()
        if not ok:
            log(f"auto: DROPPED by voice filter (score {score:.3f}, "
                f"{len(audio) / SAMPLE_RATE:.1f}s)")
            return
        self.gate.maybe_adapt(score)              # profile tracks the user
        text = (self.app.transcribe_for_auto(audio) or "").strip()
        # glossary-echo guard: a long clip whose whole "transcript" is one of
        # the personal values = the model parroting the vocabulary prompt.
        if (len(audio) / SAMPLE_RATE > 4.0 and ad.is_prompt_echo(text, self._personal)):
            log(f"auto: glossary echo suspected ({text[:40]!r}) — retrying unbiased")
            text = (self.app.transcribe_for_auto(audio, vocabulary="") or "").strip()
        t2 = time.time()
        log(f"auto: voice ok ({score:.3f}, echo {corr:.2f}) "
            f"{len(audio) / SAMPLE_RATE:.1f}s clip — gate {int((t1 - t0) * 1000)}ms, "
            f"stt {int((t2 - t1) * 1000)}ms")
        text = core.collapse_repeats(text)
        if not core.has_lexical_content(text) or core.is_hallucination(text):
            return
        if ad.is_noise(text):                     # throat-clear → "Ahem."
            log(f"auto: noise dropped ({text[:30]!r})")
            return
        text = ad.apply_fixers(text, self._fixers)
        # the box may have changed while we transcribed, or a manual capture
        # started — never type into the wrong place, never fight it
        if not self._armed or cid != self._armed_id or self.app.state != "idle":
            log(f"auto: focus moved — dropping {text[:60]!r}")
            return
        if len(self._auto_last) > 64:
            self._auto_last.clear()
        # per-box session record: everything we typed (for precise voice
        # edits) + the most recent utterance (for "scratch that")
        rec = self._auto_last.setdefault(cid, {"text": "", "last": ""})
        sp = ad.special_of(text)
        if sp == "scratch":
            if rec["last"]:
                send_backspaces(len(rec["last"]))
                rec["text"] = rec["text"][:-len(rec["last"])]
                log(f"auto: scratch that ({len(rec['last'])} chars)")
                rec["last"] = ""
            self.app.play_cue("tick")
            return
        if sp == "send":
            in_terminal = (isinstance(cid, tuple) and len(cid) == 2
                           and cid[1] == "terminal")
            if in_terminal and not self._send_in_terminal:
                # Enter at a shell prompt EXECUTES the line — never by voice.
                self.app.play_cue("cancel")
                log("auto: 'send it' blocked in terminal")
                return
            send_enter()
            rec["text"], rec["last"] = "", ""     # box is empty again
            self.app.play_cue("tick")
            log("auto: send it")
            return
        if ad.is_clear_all(text):                 # "delete everything" / "start over"
            n = len(rec["text"])
            if n > 0:
                send_backspaces(n)
                rec["text"], rec["last"] = "", ""
                self.app.play_cue("tick")
                log(f"auto: cleared all ({n} chars)")
            else:
                self.app.play_cue("cancel")        # nothing we typed → don't guess
                log("auto: clear-all but nothing tracked")
            return
        deletion = ad.delete_of(text)             # "remove the last 3 words"
        if deletion is not None:
            unit, count = deletion
            n = ad.chars_to_delete(rec["text"], unit, count)
            if n > 0:
                send_backspaces(n)
                rec["text"] = rec["text"][:-n]
                rec["last"] = ""
                self.app.play_cue("tick")
                log(f"auto: removed last {count} {unit}(s) — {n} chars")
            elif unit == "word":
                # we didn't type this text; Option+Backspace works in most apps
                send_word_backspaces(count)
                self.app.play_cue("tick")
                log(f"auto: word-backspace x{count} (untracked text)")
            else:
                self.app.play_cue("cancel")
                log(f"auto: no tracked text to remove a {unit} from")
            return
        snip = ad.snippet_of(text, self._personal)
        if snip is not None:                       # "type my email"
            log(f"auto: snippet → {len(snip)} chars")
            text = snip
        else:
            target = ad.action_of(text)
            if target is not None:                 # "switch to slack" / "open chrome"
                done = activate_app(target) or launch_app(target)
                self.app.play_cue("tick" if done else "error")
                log(f"auto: action '{target}' → {'ok' if done else 'FAILED'}")
                return
            if ad.is_command(text):                # "write a reply saying ..."
                drafted = self.app.auto_write(text)
                if not drafted:
                    self.app.play_cue("error")
                    return
                text = drafted
            elif ad.is_maybe_command(text):         # "add a paragraph of ..."
                drafted = self.app.auto_write(text, maybe=True)
                if drafted == "":
                    self.app.play_cue("error")
                    return
                if drafted is not None:              # None = model ruled DICTATION
                    text = drafted
        if snip is None:                            # don't mangle an email snippet
            text = core.start_case(text, rec["text"])
        out = text
        if rec["text"] and not rec["text"][-1].isspace():
            out = " " + text                         # utterances flow as prose
        self.app.emit_text(out)
        rec["text"] = (rec["text"] + out)[-20000:]   # cap a marathon session
        rec["last"] = out
        self.app.play_cue("tick")
        log(f"auto: typed {len(out)} chars")


# ───────────────────────── standalone enrollment CLI ─────────────────────────
def _enroll_cli(seconds: int = 30) -> None:
    """Record `seconds` from the default mic and enroll a voice profile — lets
    enrollment happen from the command line before Lane C's GUI flow lands."""
    import sounddevice as sd

    print(f"Recording {seconds}s to enroll your voice for Auto-Dictate.")
    print("Speak naturally the whole time — read something aloud, or just talk.")
    for i in range(3, 0, -1):
        print(f"  starting in {i}...")
        time.sleep(1)
    print("  recording...")
    rec = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                 channels=1, dtype="float32")
    try:
        for remaining in range(seconds, 0, -1):
            print(f"  {remaining}s remaining", end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  stopped early.")
    sd.wait()
    print("\n  done recording.")
    audio = np.asarray(rec).reshape(-1)
    if len(audio) / SAMPLE_RATE < 10.0 or not core.contains_speech(audio):
        print("Not enough speech captured (need at least 10s of real speech) — try again.")
        raise SystemExit(1)
    path = portable.VOICE_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    gate = ad.SpeakerGate(str(path))
    gate.enroll(audio)
    print(f"Voice profile saved to {path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Auto-Dictate maintenance CLI")
    parser.add_argument("--enroll", action="store_true",
                        help="record 30s from the default mic and enroll your voice")
    args = parser.parse_args()
    if args.enroll:
        _enroll_cli()
    else:
        parser.print_help()
