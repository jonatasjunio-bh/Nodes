"""
core/voiceguard.py — voice biometrics: is this the owner speaking?

Every utterance from the PC microphone is checked against the owner's
enrolled voiceprint with a speaker-embedding model (NVIDIA NeMo TitaNet,
trained on thousands of speakers), run locally through sherpa-onnx. No audio leaves the PC for this; one check takes ~20 ms.

Modes (⚙ → plugin settings → Voice ID, or by voice):

  off        no checking.
  sensitive  conversation is unaffected. Irreversible actions — the ones
             that already wait for CONFIRM on the HUD — additionally require
             that the request came in the owner's voice (or was typed: the
             keyboard and the paired phone are authenticated channels).
  strict     the assistant only hears the owner. Each utterance is held back
             until it is verified (usually 1.5–2 s of speech, at most 3 s),
             then released whole — not a word is lost, the first answer just
             starts a little later. Anyone
             else (the TV, visitors) is dropped. Short replies ("sim", "ok")
             right after a verified utterance pass straight through.

Enrolment: ~12 s of the owner's natural speech, captured from the same mic,
averaged into one 512-number voiceprint (memory/voiceprint.npy).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
# TitaNet-small, not CAM++: with sherpa-onnx 1.13 CAM++ scored the same voice
# anywhere from 0.03 to 0.91 depending on clip length; TitaNet holds the owner
# at 0.73–0.94 and other voices at ~0.30 from 1 s of speech up.
MODEL = BASE / "models" / "speaker_titanet_small.onnx"
PRINT = BASE / "memory" / "voiceprint.npy"
META = BASE / "memory" / "voiceprint.json"
NAMESPACE = "voice_id"

SR = 16000
# Accept early, reject late. Measured: a 1 s sample says nothing (three
# different voices all scored 0.21), 1.5–2 s often already clears the owner,
# and the whole utterance separates voices cleanly (owner 0.57, other 0.20).
# So an utterance is accepted as soon as the evidence is good, re-scored on all
# the audio so far every RESCORE seconds, and only rejected at FINAL_SECONDS of
# speech or at its end.
VERIFY_SECONDS = 1.5       # first look
RESCORE = 0.75             # look again after this much more speech
FINAL_SECONDS = 3.0        # verdict by now, either way
MIN_SECONDS = 0.6          # below this an utterance can't be judged on its own
HANGOVER = 0.9             # silence that ends an utterance
FOLLOW_UP = 30.0           # a verified owner keeps "short replies" credit this long
SENSITIVE_WINDOW = 45.0    # how recent the owner's voice must be for a sensitive action
MAX_HOLD = 3.0             # never hold more than this much audio in strict mode
DEFAULT_THRESHOLD = 0.50
ENROLL_TIMEOUT = 90.0      # give up (or settle for what we have) after this long
ENROLL_MIN = 6.0           # enough speech to settle for at the timeout


class VoiceGuard:
    def __init__(self):
        self._ext = None
        self._load_lock = threading.Lock()
        self._print: np.ndarray | None = None
        self._log = print
        self._noise = 150.0                  # adaptive noise floor (int16 RMS)
        self._in_utt = False
        self._silence = 0.0
        self._voiced: list[np.ndarray] = []  # voiced float audio of this utterance
        self._voiced_len = 0.0
        self._held: list[bytes] = []         # strict mode: chunks not yet released
        self._held_len = 0.0
        self._decision: bool | None = None   # this utterance: owner / not / undecided
        self._next_look = VERIFY_SECONDS
        self.last_score: float | None = None
        self.last_owner_at = 0.0
        self.last_text_at = 0.0
        self._enroll: list[np.ndarray] | None = None
        self._enroll_target = 0.0
        self._enroll_done = None
        self._enroll_started = 0.0
        self._enroll_step = 0
        self._peak = 0.0                     # loudest chunk heard while enrolling
        self._load_print()

    # ── settings ─────────────────────────────────────────────────────────────

    def set_logger(self, fn) -> None:
        self._log = fn

    _cfg_cache: tuple[float, dict] = (0.0, {})

    def _cfg(self) -> dict:
        # Read on every mic chunk (15×/s): cache it for 2 s instead of
        # re-reading the config file each time.
        at, cfg = self._cfg_cache
        if time.monotonic() - at < 2.0:
            return cfg
        try:
            from memory.config_manager import get_plugin_config
            cfg = get_plugin_config(NAMESPACE)
        except Exception:
            cfg = {}
        self._cfg_cache = (time.monotonic(), cfg)
        return cfg

    def mode(self) -> str:
        m = str(self._cfg().get("mode", "off")).lower()
        return m if m in ("off", "sensitive", "strict") else "off"

    def threshold(self) -> float:
        try:
            return min(0.9, max(0.2, float(self._cfg().get("threshold") or DEFAULT_THRESHOLD)))
        except ValueError:
            return DEFAULT_THRESHOLD

    def enrolled(self) -> bool:
        return self._print is not None

    def active(self) -> bool:
        return self.enrolled() and self.mode() != "off"

    # ── model ────────────────────────────────────────────────────────────────

    def preload(self) -> None:
        threading.Thread(target=self._extractor, daemon=True, name="voiceguard-load").start()

    def _extractor(self):
        with self._load_lock:
            if self._ext is None:
                if not MODEL.exists():
                    raise FileNotFoundError(f"speaker model missing: {MODEL}")
                import sherpa_onnx
                cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=1)
                self._ext = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            return self._ext

    def embed(self, audio: np.ndarray) -> np.ndarray:
        ext = self._extractor()
        s = ext.create_stream()
        s.accept_waveform(SR, audio.astype(np.float32))
        s.input_finished()
        e = np.asarray(ext.compute(s), dtype=np.float32)
        return e / (np.linalg.norm(e) + 1e-9)

    def score(self, audio: np.ndarray) -> float:
        if self._print is None:
            return 0.0
        return float(np.dot(self.embed(audio), self._print))

    def _load_print(self) -> None:
        try:
            self._print = np.load(PRINT) if PRINT.exists() else None
            # a voiceprint only means something to the model that made it
            meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}
            if self._print is not None and meta.get("model") != MODEL.name:
                self._print = None
        except Exception:
            self._print = None

    # ── the live microphone path ─────────────────────────────────────────────

    def route(self, chunk: np.ndarray) -> list[bytes]:
        """Called for every mic chunk (int16, 16 kHz) that would go to the
        model. Returns the chunks to send now — possibly none (held or
        dropped) or several (a verified utterance released at once)."""
        raw = chunk.tobytes()
        mono = chunk.reshape(-1).astype(np.float32)
        dur = len(mono) / SR
        rms = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
        # Relative to the room, with a low absolute floor: a laptop mic idles
        # at RMS ~80–190 and speech from a desk away is only ~300–800.
        voiced = rms > max(150.0, self._noise * 2.2)
        if not voiced:                          # track the room's noise floor
            self._noise = 0.95 * self._noise + 0.05 * max(rms, 50.0)

        if self._enroll is not None:
            self._peak = max(self._peak, rms)
            if voiced:
                self._enroll_feed(mono / 32768.0)
            elif time.monotonic() - self._enroll_started > ENROLL_TIMEOUT:
                self._enroll_timeout()
            # While enrolling the model hears nothing: otherwise it answers the
            # owner's monologue, and while it talks the mic isn't sampled.
            return []

        if not self.active():
            return [raw]
        mode = self.mode()

        # utterance boundaries
        if voiced:
            if not self._in_utt:
                self._start_utterance()
            self._silence = 0.0
            self._voiced.append(mono / 32768.0)
            self._voiced_len += dur
        elif self._in_utt:
            self._silence += dur
            if self._silence >= HANGOVER:
                return self._end_utterance(raw, mode)

        if not self._in_utt:
            return [raw]                        # silence between utterances flows freely

        if self._decision is None and self._voiced_len >= self._next_look:
            self._decide(final=self._voiced_len >= FINAL_SECONDS)
            self._next_look = self._voiced_len + RESCORE

        if mode == "sensitive":
            return [raw]                        # never held; the decision is only recorded

        # strict
        if self._decision is True:
            out = self._held + [raw]
            self._held, self._held_len = [], 0.0
            return out
        if self._decision is False:
            return []
        self._held.append(raw)
        self._held_len += dur
        if self._held_len >= MAX_HOLD:          # long enough without a verdict: decide now
            self._decide(final=True)
            if self._decision:
                out, self._held, self._held_len = self._held, [], 0.0
                return out
            self._held, self._held_len = [], 0.0
        return []

    def _start_utterance(self):
        self._in_utt = True
        self._voiced, self._voiced_len = [], 0.0
        self._held, self._held_len = [], 0.0
        self._decision = None
        self._next_look = VERIFY_SECONDS

    def _decide(self, final: bool = True):
        """Score all voiced audio of the utterance so far. Accepts as soon as
        the score clears the threshold; rejects only when final."""
        audio = np.concatenate(self._voiced) if self._voiced else np.zeros(1, np.float32)
        try:
            s = self.score(audio)
        except Exception as e:
            self._log(f"SYS: Voice ID unavailable ({e}) — letting audio through.")
            self._decision = True
            return
        self.last_score = s
        ok = s >= self.threshold()
        if not ok and not final:
            return                               # not yet — wait for more speech
        self._decision = ok
        if ok:
            self.last_owner_at = time.monotonic()
        elif self.mode() == "strict":
            self._log(f"SYS: 🔒 Voz não reconhecida (similaridade {s:.2f}) — ignorada.")

    def _end_utterance(self, raw: bytes, mode: str) -> list[bytes]:
        out: list[bytes] = []
        if self._decision is None:
            if self._voiced_len >= MIN_SECONDS:
                self._decide(final=True)
            elif time.monotonic() - self.last_owner_at < FOLLOW_UP:
                self._decision = True            # a short reply in a conversation already verified
            else:
                self._decision = False
        if mode == "strict" and self._decision:
            out = self._held
        self._in_utt = False
        self._held, self._held_len = [], 0.0
        self._voiced, self._voiced_len = [], 0.0
        return out + [raw]

    # ── sensitive actions ────────────────────────────────────────────────────

    def note_text_input(self) -> None:
        """A typed command (PC keyboard, paired phone): authenticated another way."""
        self.last_text_at = time.monotonic()

    def allow_sensitive(self) -> tuple[bool, str]:
        if not self.active():
            return True, ""
        now = time.monotonic()
        if now - self.last_text_at < 60:
            return True, ""
        if now - self.last_owner_at < SENSITIVE_WINDOW:
            return True, ""
        return False, ("this action needs the owner's voice, and the last request was not "
                       "recognised as the owner" +
                       (f" (similarity {self.last_score:.2f})" if self.last_score is not None else ""))

    # ── enrolment ────────────────────────────────────────────────────────────

    def start_enroll(self, seconds: float = 12.0, done=None) -> None:
        self._enroll = []
        self._enroll_target = seconds
        self._enroll_done = done
        self._enroll_started = time.monotonic()
        self._enroll_step = 0
        self._peak = 0.0

    def enrolling(self) -> bool:
        return self._enroll is not None

    def _enroll_feed(self, audio: np.ndarray) -> None:
        self._enroll.append(audio)
        got = sum(len(a) for a in self._enroll) / SR
        step = int(4 * got / self._enroll_target)      # progress in quarters
        if 0 < step < 4 and step > self._enroll_step:
            self._enroll_step = step
            self._log(f"SYS: 🎙️ Cadastro de voz: {step * 25}%…")
        if got >= self._enroll_target:
            self._enroll_finish_now()
        elif time.monotonic() - self._enroll_started > ENROLL_TIMEOUT:
            self._enroll_timeout()

    def _enroll_finish_now(self) -> None:
        samples, self._enroll = np.concatenate(self._enroll), None
        threading.Thread(target=self._finish_enroll, args=(samples,), daemon=True).start()

    def _enroll_timeout(self) -> None:
        got = sum(len(a) for a in self._enroll) / SR
        if got >= ENROLL_MIN:
            self._enroll_finish_now()
            return
        self._enroll = None
        msg = (f"O cadastro da voz não captou fala suficiente em {ENROLL_TIMEOUT:.0f} s "
               f"({got:.0f} s de fala; volume máximo {self._peak:.0f}, ruído {self._noise:.0f}). "
               f"Fale mais perto do microfone ou aumente o volume de entrada e tente de novo.")
        self._log(f"SYS: 🎙️ {msg}")
        if self._enroll_done:
            try:
                self._enroll_done(msg)
            except Exception:
                pass

    def cancel_enroll(self) -> None:
        self._enroll = None

    def _finish_enroll(self, samples: np.ndarray) -> None:
        try:
            seg = len(samples) // 3
            embs = [self.embed(samples[i * seg:(i + 1) * seg]) for i in range(3)]
            mean = np.mean(embs, axis=0)
            mean /= np.linalg.norm(mean) + 1e-9
            consistency = float(np.mean([np.dot(e, mean) for e in embs]))
            PRINT.parent.mkdir(parents=True, exist_ok=True)
            np.save(PRINT, mean)
            META.write_text(json.dumps({"enrolled_at": time.time(), "seconds": len(samples) / SR,
                                        "model": MODEL.name,
                                        "consistency": consistency}), encoding="utf-8")
            self._print = mean
            msg = (f"Voz cadastrada (consistência {consistency:.2f})."
                   + ("" if consistency > 0.75 else " Ficou abaixo do ideal: repita num lugar mais silencioso."))
        except Exception as e:
            msg = f"O cadastro da voz falhou: {e}"
        self._log(f"SYS: 🎙️ {msg}")
        if self._enroll_done:
            try:
                self._enroll_done(msg)
            except Exception:
                pass

    def forget(self) -> None:
        for f in (PRINT, META):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        self._print = None


GUARD = VoiceGuard()
