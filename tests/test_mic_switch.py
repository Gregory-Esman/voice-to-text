"""Mic switching must never leave the recorder dead, and devices that refuse
16 kHz (Bluetooth headsets) must fall back to native-rate + resampling.

Run: uv run python tests/test_mic_switch.py
No real audio streams are opened — _open_stream is faked.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flow  # noqa: E402

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, f"FAIL {msg}"
    PASS += 1
    print(f"PASS {msg}")


class FakeOpens:
    """Stands in for AudioRecorder._open_stream: raises for BAD, logs opens."""

    def __init__(self):
        self.opened = []

    def __call__(self, rec):
        if rec._device == "BAD":
            raise RuntimeError("cannot open BAD device")
        self.opened.append(rec._device)
        rec._stream = None  # never a real stream in tests


def make_recorder(fake):
    orig = flow.AudioRecorder._open_stream
    flow.AudioRecorder._open_stream = lambda self: fake(self)
    try:
        rec = flow.AudioRecorder(device="GOOD1", preroll_seconds=0.0, warm=True)
    finally:
        flow.AudioRecorder._open_stream = lambda self: fake(self)
    return rec, orig


def main():
    fake = FakeOpens()
    rec, orig = make_recorder(fake)
    try:
        ok(fake.opened == ["GOOD1"], "warm init opens the initial device")

        # Successful switch: device updated, stream reopened on new device.
        rec.set_device("GOOD2")
        ok(rec._device == "GOOD2", "set_device to good device updates _device")
        ok(fake.opened[-1] == "GOOD2", "set_device to good device reopens stream")

        # Failed switch: raises, reverts to previous device, reopens it.
        raised = False
        try:
            rec.set_device("BAD")
        except RuntimeError:
            raised = True
        ok(raised, "set_device to bad device raises")
        ok(rec._device == "GOOD2", "failed switch reverts _device to previous")
        ok(fake.opened[-1] == "GOOD2", "failed switch reopens the previous device")

        # Resampler: 48 kHz sine -> 16 kHz, one third the samples, finite.
        n = 4800
        t = np.arange(n) / 48000.0
        chunk = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        out = flow._resample_to_16k(chunk, 48000)
        ok(abs(len(out) - n // 3) <= 2, "48k->16k output is ~1/3 the length")
        ok(out.dtype == np.float32 and np.all(np.isfinite(out)), "resampled output is finite float32")
        ok(float(np.max(np.abs(out))) > 0.5, "resampled sine keeps its amplitude")

        # Callback path resamples when native rate differs.
        rec._native_rate = 48000
        rec._recording = True
        rec._frames = []
        rec._callback(chunk.reshape(-1, 1), n, None, None)
        ok(abs(len(rec._frames[0]) - n // 3) <= 2, "_callback resamples native-rate chunks to 16k")
    finally:
        flow.AudioRecorder._open_stream = orig

    print(f"\nALL PASS ({PASS})")


if __name__ == "__main__":
    main()
