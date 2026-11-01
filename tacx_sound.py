"""
tacx_sound.py - time-trial start clock beeps for a running workout.

A short pip at 5, 4, 3, 2 and 1 seconds before each step change, then one long,
higher tone on the change. The beeps run on their own thread, away from the GUI,
for two reasons:

  * Playback goes through Windows' PlaySound, synchronously on this thread, so chart
    redraws on the GUI thread can't starve the audio. (Qt's QSoundEffect is fed from
    the GUI thread, and its tones broke up while the dashboard was busy.)
  * Timing comes from a 5 ms poll that projects the player's countdown forward from
    its last tick (ttc - (now - read_at)). The pips therefore land on the second,
    instead of on the next 10 Hz player tick.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave

import numpy as np

log = logging.getLogger("tacx_sound")

try:
    import winsound
except ImportError:  # not Windows: no beeps
    winsound = None


def tone_wav(freq: float, dur: float, volume: float = 0.45, rate: int = 44100) -> bytes:
    """A square-wave-like tone (odd harmonics), like an electronic start clock, as WAV bytes."""
    t = np.arange(int(rate * dur)) / rate
    wave_ = sum(np.sin(2 * np.pi * k * freq * t) / k for k in (1, 3, 5, 7))
    env = np.minimum(1, np.minimum(t / 0.004, (dur - t) / 0.010))  # click-free edges
    samples = (wave_ * env * volume * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


class CountdownBeeper(threading.Thread):
    """Watches a WorkoutPlayer (via player.clock()) and plays the countdown."""

    PIP = (1000, 0.15)   # Hz, seconds
    GO = (1400, 0.9)
    POLL_S = 0.005

    def __init__(self, player):
        super().__init__(daemon=True, name="tacx-beeps")
        self.player = player
        self._stop = threading.Event()
        self._pip = tone_wav(*self.PIP)
        self._go = tone_wav(*self.GO)

    def stop(self) -> None:
        self._stop.set()

    def _play(self, wav: bytes) -> None:
        if winsound is None:
            return
        try:
            # synchronous on this thread: Windows plays it start to finish
            winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except RuntimeError as e:  # no audio device
            log.warning("beep failed: %s", e)

    def run(self) -> None:
        prev_est = None      # projected time to change at the previous poll
        prev_changes = None
        go_played = -1       # value of `changes` whose change already got its long tone
        last_pip = None      # lowest pip played this step: each of 5..1 sounds once only
        while not self._stop.wait(self.POLL_S):
            ttc, read_at, running, changes = self.player.clock()
            if prev_changes is not None and changes != prev_changes:
                # the step changed on a tick before the projection reached 0: play it now
                if go_played != prev_changes:
                    self._play(self._go)
                prev_est, prev_changes, last_pip = None, changes, None
                continue
            if ttc is None:
                prev_est = None
                continue
            est = ttc - (time.time() - read_at) if running else ttc
            if prev_est is not None and running:
                for n in range(self.player.COUNTDOWN_S, 0, -1):
                    # a pause freezes the projection slightly above where it was, so
                    # without the last_pip check a pip could repeat on resuming
                    if prev_est > n >= est and (last_pip is None or n < last_pip):
                        last_pip = n
                        self._play(self._pip)
                        break
                if prev_est > 0 >= est and go_played != changes:
                    go_played = changes
                    self._play(self._go)
            prev_est, prev_changes = est, changes
