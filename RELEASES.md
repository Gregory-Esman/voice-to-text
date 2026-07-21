# Releasing & sharing

One repo, one link — `https://github.com/Gregory-Esman/voice-to-text`. The top of
the README routes visitors by OS, so you never have to explain "Mac vs Windows."

## Mac parity release: Auto-Dictate (hands-free dictation)

The macOS app now matches the Windows build's headline feature — **Auto-Dictate**:
focus any text box and just talk, no hotkey. Ported straight from `windows/`'s
endpointer/matcher/speaker-gate logic (see [README's Auto-Dictate on
macOS](README.md#auto-dictate-on-macos) section), so both platforms share the
same behavior:

- A new global hotkey (default **F10**) toggles Auto-Dictate on/off.
- **Voice enrollment** (Settings ▸ *Enroll voice… (30s)*) trains a local speaker
  profile (`~/.config/voice-to-text/voice_profile.npy`) so only your voice
  arms the mic — nothing types from background chatter, calls, or media.
- Works in **both Offline and Online** modes — the same dual-backend STT/LLM
  routing manual dictation and the Write key already use.
- Settings gained a Name/Email section (`config.personal.toml`, gitignored)
  so the "type my email" / "enter my name" voice snippets and dictation
  spelling know your real details — never sent to Whisper as bias.

Since the macOS app is install-from-source (see below), there's no separate
download for this — existing installs get it with `git pull` + relaunch (or
`./build_app.sh` to rebuild `Voice To Text.app`). No new config is required;
`config.toml` ships with `[auto_dictate]`/`[hotkey]`/`[personal]` defaults, and
Auto-Dictate stays off until you enroll your voice and flip it on.

## What you actually link

| Audience | Link | Why |
|---|---|---|
| Anyone / developers | the **repo** URL | README's OS chooser self-routes them |
| Windows users who just want to run it | the **[Releases](../../releases)** page → `Voice-To-Text-Windows.zip` | download-and-run `.exe`, no Python/terminal |
| macOS users | the **repo** URL (install from source) | the Mac app needs the Python env + on-device models, so it isn't a single binary |

> The macOS app is intentionally **install-from-source** (offline models + local LLM).
> Only the **Windows online build** ships as a standalone `.exe`.

## Cutting a Windows release (automated — no Windows box needed)

The `.exe` is built by GitHub Actions on a Windows runner.

```bash
git tag v0.1.0
git push origin v0.1.0
```

Pushing a `v*` tag triggers `.github/workflows/release-windows.yml`, which builds
`VoiceToText.exe`, zips it with the config template + README, and **attaches
`Voice-To-Text-Windows.zip` to the `v0.1.0` GitHub Release**. Share the Releases
page. (You can also run the workflow manually from the Actions tab — it uploads the
same zip as a build artifact without needing a tag.)

## Building it locally on Windows (optional)

```bat
windows\build_exe.bat
```

Produces `dist\VoiceToText.exe` and `Voice-To-Text-Windows.zip`.

## Heads-up: unsigned binaries

The `.exe` is **not code-signed**, so Windows SmartScreen will warn on first run
("Windows protected your PC" → *More info* → *Run anyway*). The macOS `.app`
launcher is likewise unsigned (right-click → Open). Signing (an EV cert for
Windows, an Apple Developer ID + notarization for macOS) removes the warnings and
is the natural next step before sharing widely.
