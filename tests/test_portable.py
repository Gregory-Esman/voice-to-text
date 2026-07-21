"""Unit tests for portable.py — the shim that makes windows/ modules importable
on macOS (and anywhere else). Run with the project venv:
  uv run python tests/test_portable.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import portable  # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


check("portable.vtt_core.start_case works",
      portable.vtt_core.start_case("hello") == "Hello")
check("portable.autodictate.special_of works",
      portable.autodictate.special_of("Send it.") == "send")
check("portable.streaming.DictationStream exists",
      hasattr(portable.streaming, "DictationStream"))
check("portable.vtt_core.has_lexical_content digit-aware",
      portable.vtt_core.has_lexical_content("555 1234") is True)
check("portable.VOICE_PROFILE_PATH ends with voice_profile.npy",
      str(portable.VOICE_PROFILE_PATH).endswith("voice_profile.npy"))

print(("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
