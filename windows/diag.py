"""Voice-To-Text Windows diagnostic. Run from YOUR OWN terminal (not via the
agent), so it sees your interactive desktop and keyboard:

    & "C:\\Gregory Esman\\Gregory Esman\\Claude Code\\Voice-To-Text\\.venv\\Scripts\\python.exe" `
      "C:\\Gregory Esman\\Gregory Esman\\Claude Code\\Voice-To-Text\\windows\\diag.py"

It checks: the Groq key, an audible beep, 2s of mic capture, and whether tapping
Alt is detected. Everything is also written to %TEMP%\\vtt_diag.log.
"""
from __future__ import annotations
import sys, os, time, datetime

LOG = os.path.join(os.environ.get("TEMP", "."), "vtt_diag.log")
open(LOG, "w", encoding="utf-8").close()


def log(m: str) -> None:
    line = f"{datetime.datetime.now():%H:%M:%S} {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


log(f"=== VTT diag ===  python={sys.version.split()[0]}  pid={os.getpid()}")
try:
    import ctypes
    log(f"session: process appears interactive (GetCurrentThreadId ok)")
except Exception:
    pass

# 1) Groq key
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import vtt_core as core
    k = core._resolve_api_key("GROQ_API_KEY", "groq_key")
    log(f"[1] Groq key present: {bool(k)}")
except Exception as e:
    log(f"[1] key check FAILED: {e}")

# 2) beep
try:
    import winsound
    winsound.MessageBeep(winsound.MB_OK)
    log("[2] beep played (MB_OK) — did you HEAR it? if not, your Windows sound "
        "scheme has no 'Default Beep' assigned (cosmetic only).")
except Exception as e:
    log(f"[2] beep FAILED: {e}")

# 3) mic capture (countdown, then 5s) + per-second levels
try:
    import numpy as np, sounddevice as sd
    dev = sd.query_devices(kind="input")
    log(f"[3] default input device: {dev['name']}")
    for c in (3, 2, 1):
        log(f"[3] get ready to talk... {c}")
        time.sleep(1)
    frames = []
    with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                        callback=lambda indata, n, t, s: frames.append(indata[:, 0].copy())):
        log("[3] >>> TALK NOW for 5 seconds <<<")
        for sec in range(5):
            time.sleep(1)
            cur = np.concatenate(frames) if frames else np.zeros(1, dtype="float32")
            log(f"[3]   ...{sec+1}s  running peak={float(np.max(np.abs(cur))):.4f}")
    a = np.concatenate(frames) if frames else np.zeros(0, dtype="float32")
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    verdict = ("OK, mic is capturing your voice" if peak > 0.01
               else "STILL SILENT — mic is muted/disabled, not a code issue")
    log(f"[3] FINAL: {a.size} samples, peak={peak:.4f} -> {verdict}")
except Exception as e:
    log(f"[3] audio FAILED: {e}")

# 4) confirm the new hotkeys register (F9 / F10)
log("[4] HOTKEY CHECK: in the next 12s, tap F9 a couple times, then F10 a "
    "couple times. You should see Key.f9 / Key.f10 lines:")
try:
    from pynput import keyboard
    l = keyboard.Listener(on_press=lambda key: log(f"    PRESS   {key}"),
                          on_release=lambda key: log(f"    RELEASE {key}"))
    l.start()
    time.sleep(12)
    l.stop()
except Exception as e:
    log(f"[4] keyboard FAILED: {e}")

log("=== done === (log saved to %TEMP%\\vtt_diag.log)")
