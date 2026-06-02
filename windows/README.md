# Voice-To-Text — Windows (online / Groq mode)

A Windows port of the macOS app. **Online mode only** — transcription and the
Write/Edit AI both run on Groq's cloud, so there are no multi-GB on-device model
downloads. The macOS app (`flow.py`) is unchanged; this backend reuses the same
core logic via `vtt_core.py`.

> Status: Phases 0–2 implemented. Offline/on-device transcription is **not**
> ported (macOS uses Apple's MLX, which is Apple-silicon only).

## What works
- **Dictate (Right Alt):** tap, speak, tap again → your words are transcribed and
  pasted into the focused app.
- **Write / Edit (Left Alt):** tap, speak an *instruction*, tap again.
  - Text selected → it edits the selection in place.
  - Nothing selected → it drafts the message and pastes it (styled to email vs chat).
- **Thread-context stitching:** repeated Write captures of the same app/thread are
  merged in memory so it "remembers" a conversation you only partly see (session
  only; "Clear thread context" in the tray wipes it).
- **Recording HUD:** a small always-on-top level pill while you talk.
- **Tray menu:** start-at-login toggle, clear thread context, restart, quit.

## Setup
1. **Install Python 3.11+** (3.11 or newer, for stdlib `tomllib`).
2. **Install deps:**
   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r windows\requirements-windows.txt
   ```
3. **Get a free Groq API key** at https://console.groq.com and store it (no plaintext
   in the repo). Easiest:
   ```bat
   setx GROQ_API_KEY "gsk_your_key_here"
   ```
   (reopen the terminal after `setx`). Or put it in Windows Credential Manager under
   service `voice-to-text`, account `groq_key`.
4. **(Optional) config:** copy `windows\config.example.toml` to
   `%APPDATA%\Voice-To-Text\config.toml` to change models or hotkeys.
5. **Run:**
   ```bat
   pythonw windows\app.py     REM no console window
   REM  or, to see logs:  python windows\app.py
   ```
   Look for the amber mic icon in the system tray.

## Notes & known limits
- **Mic permission:** Windows Settings ▸ Privacy & security ▸ Microphone must allow
  desktop apps.
- **Alt as a hotkey** can nudge some apps' menu bars on a tap. If that bothers you,
  set `dictate_key`/`command_key` to `f9`/`f10`/`ctrl_r` in the config.
- **Screen context** uses UI Automation; some Electron apps expose little text
  (same caveat as macOS Accessibility) — it degrades gracefully to no context.
- **No web access:** the Write key answers from the model's own knowledge, it does
  not browse the internet.
