"""Create a clickable Voice-To-Text app icon on Windows.

Generates assets/AppIcon.ico (amber mic glyph, matching the tray icon) and adds
shortcuts to the Desktop and Start Menu that launch the app with the project's
own venv pythonw.exe (no console window). Run once:

    .venv\\Scripts\\python.exe windows\\install_shortcut.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
ASSETS = os.path.join(PROJ, "assets")
ICO = os.path.join(ASSETS, "AppIcon.ico")


def make_ico() -> str:
    """Render the amber-circle / dark-mic glyph at several sizes into one .ico."""
    from PIL import Image, ImageDraw
    os.makedirs(ASSETS, exist_ok=True)
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = []
    for s in sizes:
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        u = s / 64.0  # design grid is 64×64, like _make_icon_image
        d.ellipse((8 * u, 8 * u, 56 * u, 56 * u), fill=(245, 177, 92, 255))
        d.rounded_rectangle((27 * u, 18 * u, 37 * u, 40 * u), radius=5 * u,
                            fill=(26, 20, 10, 255))
        d.rectangle((31 * u, 40 * u, 33 * u, 48 * u), fill=(26, 20, 10, 255))
        imgs.append(img)
    imgs[0].save(ICO, format="ICO",
                 sizes=[(s, s) for s in sizes], append_images=imgs[1:])
    return ICO


def venv_pythonw() -> str:
    """The project's windowless interpreter (falls back to the current one)."""
    cand = os.path.join(PROJ, ".venv", "Scripts", "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return sys.executable.replace("python.exe", "pythonw.exe")


def make_shortcut(path: str, target: str, args: str, workdir: str, icon: str) -> None:
    import win32com.client  # type: ignore
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(path)
    sc.TargetPath = target
    sc.Arguments = args
    sc.WorkingDirectory = workdir
    sc.IconLocation = icon
    sc.Description = "Voice-To-Text — push-to-talk dictation + AI writing"
    sc.save()


def main() -> None:
    ico = make_ico()
    pyw = venv_pythonw()
    app = os.path.join(HERE, "app.py")
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    start_menu = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                              "Start Menu", "Programs")
    targets = [
        os.path.join(desktop, "Voice-To-Text.lnk"),
        os.path.join(start_menu, "Voice-To-Text.lnk"),
    ]
    for lnk in targets:
        try:
            os.makedirs(os.path.dirname(lnk), exist_ok=True)
            make_shortcut(lnk, pyw, f'"{app}"', PROJ, ico)
            print(f"created: {lnk}")
        except Exception as e:
            print(f"failed: {lnk}: {e}")
    print(f"icon: {ico}")


if __name__ == "__main__":
    main()
