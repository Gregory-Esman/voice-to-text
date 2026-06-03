# Releasing & sharing

One repo, one link — `https://github.com/Gregory-Esman/voice-to-text`. The top of
the README routes visitors by OS, so you never have to explain "Mac vs Windows."

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
