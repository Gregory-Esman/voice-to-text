"""Make the OS-neutral windows/ modules importable on macOS (and anywhere).
windows/streaming.py does a flat `import vtt_core`, so windows/ must be on
sys.path. APPENDED (not prepended) so nothing there can shadow real packages.
Only vtt_core / autodictate / streaming are imported — backend/gui/app are
Windows-only and never touched."""
import sys
from pathlib import Path

WINDOWS_DIR = Path(__file__).resolve().parent / "windows"
if str(WINDOWS_DIR) not in sys.path:
    sys.path.append(str(WINDOWS_DIR))

import vtt_core      # noqa: E402
import autodictate   # noqa: E402
import streaming     # noqa: E402

VOICE_PROFILE_PATH = Path.home() / ".config" / "voice-to-text" / "voice_profile.npy"
