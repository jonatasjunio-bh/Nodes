"""
core/aec.py — acoustic echo cancellation for talking over speakers.

With a desk microphone and a soundbar, the microphone hears the assistant's own
voice. So the mic used to be closed while it spoke: no echo, but no way to cut
in either — a walkie-talkie, not a conversation.

This is WebRTC's audio processing module (the one video calls use), through
the `livekit` package's binding: it is told exactly what is being played (the
"far end", fed from the speaker writer) and subtracts its echo from what the
microphone picks up (the "near end"), so what is left is the user's voice. With
that, the microphone stays open while the assistant talks and the Live server
hears an interruption the way a person would.

WebRTC works on 10 ms frames at 16 kHz here: the far end arrives at 24 kHz and
is resampled; mic blocks of any size are cut into 160-sample frames and the
processed audio handed back with the same length (about 10 ms later).

If the package is missing or anything fails, available() is False and the app
behaves exactly as before (mic closed while speaking).
"""

from __future__ import annotations

import threading

import numpy as np

RATE = 16000
FRAME = RATE // 100            # 10 ms


class EchoCanceller:
    def __init__(self, delay_ms: int = 120):
        from livekit import rtc
        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                              high_pass_filter=True, auto_gain_control=False)
        self._lock = threading.Lock()
        self._far = np.zeros(0, dtype=np.int16)
        self._near_in = np.zeros(0, dtype=np.int16)
        self._near_out = np.zeros(0, dtype=np.int16)
        self.set_delay(delay_ms)
        self.frames_far = 0
        self.frames_near = 0

    def set_delay(self, ms: int) -> None:
        try:
            with self._lock:
                self._apm.set_stream_delay_ms(int(max(0, min(500, ms))))
        except Exception:
            pass

    def _frame(self, samples: np.ndarray):
        return self._rtc.AudioFrame(samples.astype(np.int16).tobytes(), RATE, 1, FRAME)

    # ── far end: what the speakers are about to play (24 kHz int16 bytes) ──
    def feed_far(self, pcm24: bytes) -> None:
        a = np.frombuffer(pcm24, dtype=np.int16)
        if a.size == 0:
            return
        n_out = int(round(a.size * RATE / 24000))
        if n_out <= 0:
            return
        x = np.linspace(0, a.size - 1, n_out)
        res = np.interp(x, np.arange(a.size), a.astype(np.float32)).astype(np.int16)
        with self._lock:
            buf = np.concatenate([self._far, res])
            k = 0
            while buf.size - k >= FRAME:
                try:
                    self._apm.process_reverse_stream(self._frame(buf[k:k + FRAME]))
                    self.frames_far += 1
                except Exception:
                    pass
                k += FRAME
            self._far = buf[k:]

    # ── near end: a microphone block (int16 ndarray) → the same length, cleaned ──
    def process_near(self, block: np.ndarray) -> np.ndarray:
        flat = np.asarray(block, dtype=np.int16).reshape(-1)
        with self._lock:
            buf = np.concatenate([self._near_in, flat])
            outs = [self._near_out]
            k = 0
            while buf.size - k >= FRAME:
                fr = self._frame(buf[k:k + FRAME])
                try:
                    self._apm.process_stream(fr)
                    outs.append(np.frombuffer(bytes(fr.data), dtype=np.int16).copy())
                except Exception:
                    outs.append(buf[k:k + FRAME])
                self.frames_near += 1
                k += FRAME
            self._near_in = buf[k:]
            out = np.concatenate(outs)
            if out.size >= flat.size:
                ret, self._near_out = out[:flat.size], out[flat.size:]
            else:                                   # the very first block: pad the start
                ret = np.concatenate([np.zeros(flat.size - out.size, dtype=np.int16), out])
                self._near_out = np.zeros(0, dtype=np.int16)
        return ret.reshape(np.shape(block))


_instance: EchoCanceller | None = None
_failed = False


def get(delay_ms: int = 120) -> EchoCanceller | None:
    """The shared canceller, or None when WebRTC's module is unavailable."""
    global _instance, _failed
    if _instance is None and not _failed:
        try:
            _instance = EchoCanceller(delay_ms)
            print("[AEC] 🔇 WebRTC echo cancellation on (talk over the assistant to interrupt)")
        except Exception as e:
            _failed = True
            print(f"[AEC] unavailable ({type(e).__name__}: {e}) — mic stays closed while speaking")
    return _instance


def available() -> bool:
    return get() is not None
