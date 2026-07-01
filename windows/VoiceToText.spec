# PyInstaller spec — Voice-To-Text Windows online build (single windowed .exe).
#
# Build (on Windows, from the repo root):
#   pip install -r windows\requirements-windows.txt pyinstaller
#   pyinstaller --noconfirm windows\VoiceToText.spec
# Result: dist\VoiceToText.exe  (no console window; cloud-only, no models bundled).
import os
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files, collect_all

WIN = SPECPATH  # this spec lives in windows/, so SPECPATH == that folder

# sounddevice ships the PortAudio DLL as package data — bundle it.
binaries = collect_dynamic_libs("sounddevice")
datas = collect_data_files("sounddevice")
datas += [(os.path.join(WIN, "config.example.toml"), ".")]

# numpy 2.x: force-collect the whole package so the compiled _core
# (numpy._core._multiarray_umath) and its data actually land in the bundle.
# Without this, PyInstaller under-collected numpy and the exe crashed at
# `import numpy` on clean machines with ModuleNotFoundError: numpy._core.
_np_datas, _np_binaries, _np_hidden = collect_all("numpy")
datas += _np_datas
binaries += _np_binaries

hiddenimports = _np_hidden + [
    "vtt_core", "backend",
    "pystray._win32",
    "PIL.Image", "PIL.ImageDraw",
    "win32clipboard", "win32gui", "win32process", "win32api", "win32con",
    "uiautomation", "comtypes", "comtypes.client", "comtypes.stream",
    "sounddevice", "pynput.keyboard._win32", "pynput.mouse._win32",
    "keyring.backends.Windows", "winsound", "tkinter",
]

a = Analysis(
    [os.path.join(WIN, "app.py")],
    pathex=[WIN],                       # so `import vtt_core` / `import backend` resolve
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["mlx_whisper", "rumps", "objc", "AppKit", "Foundation",
              "ApplicationServices", "PyObjCTools"],  # macOS-only — never needed here
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="VoiceToText",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                      # windowed (no console) — like the menu-bar app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
