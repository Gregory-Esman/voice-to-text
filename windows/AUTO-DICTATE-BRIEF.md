# Auto-Dictate — Brief

## Goal
Hands-free dictation for lean-back use (Wacom pen as the only pointer, no
keyboard). Focusing an editable text box acts like pressing the tilde key:
speech is captured, transcribed, and typed into the box. Clicking away disarms
it. Everything that exists today (tilde, shift+tilde, tray, HUD) is unchanged.

## Non-goals (v1)
- No wake word, no intent router, no app-switching or other voice actions.
- No composition/edit sessions — this is pure dictation.
- No change to command mode (shift+tilde stays the only way to write/edit).

## UX
1. **Toggle**: "Auto-Dictate" checkbox in the tray menu + Settings. Off by
   default; requires voice enrollment before it can be turned on. Persisted in
   config.toml.
2. **Armed**: while the cursor is in an editable text field (UI Automation:
   focused element is an Edit/Document control, not read-only) and the mode is
   on, the app collects mic frames continuously. A small always-on-top HUD chip
   shows state: ARMED (gray) / CAPTURING (accent). No chip = cold.
3. **Utterances**: voice-activity endpointing segments speech — you pause
   ~0.9s, the utterance closes, transcribes via Groq (existing pipeline +
   hallucination filters), and the text types into the box. Think as long as
   you like between utterances; the box stays armed until focus leaves it.
4. **Disarm**: focus leaves the editable field (pen click elsewhere, app
   switch) → instantly back to discarding frames.
5. **Special phrases** (only two; matched after transcription, never typed):
   - "scratch that" → deletes the last utterance it typed (backspace burst).
   - "send it" → presses Enter.
6. **Sounds**: no per-utterance boops (too chatty). Soft tick when text lands;
   the chip is the primary signal. Tilde-mode sounds unchanged.

## Privacy model
- Mic *stream* stays warm always (existing design); frames are **discarded on
  arrival** unless (a) a tilde/shift+tilde capture is live, or (b) Auto-Dictate
  is on AND an editable field is focused.
- **Your-voice filter**: one-time ~30s enrollment (guided from Settings) →
  speaker embedding stored locally (%APPDATA%\Voice-To-Text). Every armed-mode
  utterance is embedded on-device and cosine-compared before upload; music,
  videos, and other voices are dropped locally and never reach Groq.
- Residual risk (accepted): the user's own voice addressed at a human while a
  text box is focused will be transcribed and typed. Mitigations: visible chip,
  "scratch that", the toggle.
- Tray "Pause hotkeys" also fully disarms Auto-Dictate.

## Technical plan
- **Focus watch**: SetWinEventHook(EVENT_OBJECT_FOCUS) (no polling) → on focus
  change, classify the focused element via the `uiautomation` package (already
  a dependency) → set/clear `armed`.
- **Endpointing**: RMS-threshold VAD over the live frame stream (reuse the
  contains_speech thresholds); utterance = speech ≥300ms followed by ≥900ms of
  trailing quiet; 60s hard cap per utterance.
- **Speaker filter**: `resemblyzer` (pip, CPU-only, ~small). Enrollment flow in
  the Settings window records 30s, stores the mean embedding. Runtime: embed
  each closed utterance, accept if cosine ≥ threshold (tunable, default ~0.75,
  exposed in config for tuning).
- **Pipeline reuse**: accepted utterance → existing `contains_speech` →
  `transcribe_remote` → `collapse_repeats` / hallucination filters → `_emit`
  (paste). Track the last emitted string for "scratch that" (emit that many
  backspaces).
- **State interplay**: a tilde/shift+tilde capture always preempts; Auto-Dictate
  ignores frames while a manual capture is live and re-arms after.
- New config section `[auto_dictate]`: enabled, similarity threshold,
  trailing-silence ms.

## Risks / open items
- UIA editable-detection quirks in some apps (Electron, games) — fallback:
  treat unknown as not-editable (fail cold, never hot).
- URL/search bars are editable → armed. Accepted; the chip shows it.
- resemblyzer accuracy on far-field laptop mic — threshold tunable; if it
  rejects the real user too often we lower it and log scores for tuning.
- Frozen-exe packaging (PyInstaller collect for resemblyzer/torch) is a later
  step; v1 runs from the venv first.

## Addendum (v1.1, same day)
Shipped after live testing:
- Terminals arm by process name (_TERMINAL_EXES); web search boxes arm as
  ComboBoxControl with writable ValuePattern.
- Speaker-echo filter (LoopbackMonitor): WASAPI loopback envelope of the
  default speakers, cross-correlated with each utterance — the machine's own
  videos/music can't type. Follows default-device changes. echo_corr = 0.55.
- Adaptive profile: accepts scoring ≥0.75 blend into the profile (α=0.05);
  raw enrollment kept as voice_profile_enrolled.npy.
- "send it" NEVER presses Enter in a terminal (send_in_terminal=false) — a
  spoken Enter at a shell prompt would execute the line. Cancel cue instead.
- exclude_apps config: exe names that never arm.
- Write commands hands-free: utterances starting write/draft/compose/
  reply-saying/respond-to route to generate_text (+ screen context via
  wants_context) instead of verbatim typing.
- App actions as whole utterances: "switch to/go to/jump to/open/launch X" →
  activate best-matching window (exe/title) or shell-start it.
- Specials tolerate Whisper mishears (sender/sent it/send; scratch it/strike
  that). Latency: encoder preloaded+warmed at startup, shared HTTP session,
  embed capped at 6 s. Recall-first tuning: similarity 0.60, silence 700 ms,
  min_speech 180 ms, start_rms 0.014.

## Acceptance
1. Mode off → behavior byte-identical to today.
2. Mode on + Notepad focused: speaking types the words; pausing to think does
   not end the session; clicking the desktop disarms.
3. Music/another voice while armed → nothing typed, nothing uploaded (verify
   via log: dropped-by-speaker-filter lines).
4. "scratch that" removes the last utterance; "send it" presses Enter.
5. Tilde and shift+tilde work exactly as before, including while armed.
6. Log lines cover: arm/disarm (with control name), utterance accepted/dropped
   (with similarity score), specials fired.
