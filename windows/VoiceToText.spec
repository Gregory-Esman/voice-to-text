# PyInstaller spec — Voice-To-Text Windows online build.
#
# Build (on Windows, from the repo root):
#   pip install -r windows\requirements-windows.txt pyinstaller
#   pyinstaller --noconfirm windows\VoiceToText.spec
# Result: dist\VoiceToText\  (ONEDIR folder — VoiceToText.exe + support files).
#
# v0.2.0: switched from onefile to onedir. Auto-Dictate bundles torch (CPU) +
# resemblyzer + librosa; a onefile exe would unpack ~0.5 GB to %TEMP% on every
# launch. The Inno installer ships the folder, so users see no difference.
import os
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files, collect_all

WIN = SPECPATH  # this spec lives in windows/, so SPECPATH == that folder

# sounddevice ships the PortAudio DLL as package data — bundle it.
binaries = collect_dynamic_libs("sounddevice")
datas = collect_data_files("sounddevice")
datas += [(os.path.join(WIN, "config.example.toml"), ".")]

# pyaudiowpatch ships its own patched PortAudio (WASAPI loopback).
binaries += collect_dynamic_libs("pyaudiowpatch")

# resemblyzer's model weights (pretrained.pt) are package data.
datas += collect_data_files("resemblyzer")

# Cue WAVs (start/stop/cancel/error) → a "sounds" folder in the bundle.
_snd = os.path.join(WIN, "sounds")
datas += [(os.path.join(_snd, f), "sounds")
          for f in os.listdir(_snd) if f.lower().endswith(".wav")]

# numpy 2.x: force-collect the whole package so the compiled _core
# (numpy._core._multiarray_umath) and its data actually land in the bundle.
# Without this, PyInstaller under-collected numpy and the exe crashed at
# `import numpy` on clean machines with ModuleNotFoundError: numpy._core.
_np_datas, _np_binaries, _np_hidden = collect_all("numpy")
datas += _np_datas
binaries += _np_binaries

# librosa lazy-loads submodules (lazy_loader) — static analysis misses them.
_lb_datas, _lb_binaries, _lb_hidden = collect_all("librosa")
datas += _lb_datas
binaries += _lb_binaries

hiddenimports = _np_hidden + _lb_hidden + [
    "vtt_core", "backend", "autodictate",
    "pystray._win32",
    "PIL.Image", "PIL.ImageDraw",
    "win32clipboard", "win32gui", "win32process", "win32api", "win32con",
    "uiautomation", "comtypes", "comtypes.client", "comtypes.stream",
    "sounddevice", "pynput.keyboard._win32", "pynput.mouse._win32",
    "keyring.backends.Windows", "winsound", "tkinter",
    # Auto-Dictate voice/echo filters
    "torch", "resemblyzer", "webrtcvad", "pyaudiowpatch",
    "scipy", "scipy.signal", "scipy.ndimage",
]

a = Analysis(
    [os.path.join(WIN, "app.py")],
    pathex=[WIN],                       # so `import vtt_core` / `import backend` resolve
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(WIN, "pyi-hooks")],  # webrtcvad-wheels metadata fix
    runtime_hooks=[],
    excludes=["mlx_whisper", "rumps", "objc", "AppKit", "Foundation",
              "ApplicationServices", "PyObjCTools",  # macOS-only — never needed here
              "torch.utils.tensorboard", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,              # onedir — binaries live in the folder
    name="VoiceToText",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                          # UPX mangles torch/llvmlite DLLs
    console=False,                      # windowed (no console) — like the menu-bar app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=False,
    name="VoiceToText",
)
