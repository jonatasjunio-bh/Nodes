import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen


# ── Console must survive non-UTF-8 code pages ────────────────────────────────
# Every status line in this file carries an emoji, and on a legacy Windows
# console the active code page is the system one — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# because most of these prints sit inside the receive loop it takes the session
# down on startup. Reconfiguring to UTF-8 with a replacement fallback costs
# nothing and makes the app launch the same way in every locale.
import sys as _sys

# ── No console at all (pythonw.exe, a desktop shortcut) ─────────────────────
# Then sys.stdout/stderr are None: every print vanishes, and libraries that
# configure logging against them fail outright — that is how the phone
# dashboard silently never started. Send both to logs/mark.log instead, so
# the app works the same either way and failures can still be read.
if _sys.stdout is None or _sys.stderr is None:
    try:
        from pathlib import Path as _P
        _logdir = _P(__file__).resolve().parent / "logs"
        _logdir.mkdir(exist_ok=True)
        _log = _logdir / "mark.log"
        if _log.exists() and _log.stat().st_size > 5_000_000:      # keep it bounded
            _log.replace(_logdir / "mark.old.log")
        import time as _time

        class _Stamped:
            """Each line starts with the time, so the log reads as a timeline
            (a dropped session is a question of WHEN, and it had no times)."""
            def __init__(self, f):
                self._f, self._bol = f, True
            def write(self, text):
                if not text:
                    return 0
                out = []
                for part in text.splitlines(keepends=True):
                    if self._bol:
                        out.append(_time.strftime("%H:%M:%S "))
                    out.append(part)
                    self._bol = part.endswith("\n")
                self._f.write("".join(out))
                return len(text)
            def flush(self):
                self._f.flush()
            def __getattr__(self, name):
                return getattr(self._f, name)

        _f = _Stamped(open(_log, "a", encoding="utf-8", errors="replace", buffering=1))
        if _sys.stdout is None:
            _sys.stdout = _f
        if _sys.stderr is None:
            _sys.stderr = _f
    except Exception:
        pass

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
from collections import deque
import queue
import threading
import time
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from memory import conversation
from memory import learning
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_jarvis,
# system_status).
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from memory.config_manager     import (
    get_brief_enabled, get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_echo_cancel,
    get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
)
from core                     import gemini as _gemini
from core.voiceguard           import GUARD as voice_guard
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.viseme               import VisemeStream
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# The conversation's model. A NAME, not a decision: the ladder lives in
# core/gemini.py and this is only whichever rung is currently in use, kept here
# as a module attribute because plugins read it (chat_takeover asks main for it
# so that upgrading the assistant upgrades the plugin too).
#
# It is reassigned on every connect, so a model that runs out of quota is
# stepped over and the assistant keeps talking instead of failing to start.
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# ── Viseme extraction ─────────────────────────────────────────────────────────
# The avatar's mouth used to be driven by one RMS value per ~200 ms write batch,
# which is five updates a second averaged over a fifth of a second — it could
# only ever flap. These read the *shape* of each 20 ms slice straight from the
# spectrum of the audio being played, so no transcript, no forced alignment and
# no language assumption: it works the same for Turkish and English.
#
# Two numbers come out. Openness tracks the first formant — F1 climbs as the jaw
# drops, so /a/ reads open and /i/ or /u/ read closed. Width tracks the second —
# F2 is high for spread vowels (/i/, /e/) and low for rounded ones (/u/, /o/).
# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

_VIS_WIN = 1024        # ~43 ms analysis window at 24 kHz: enough for formants
_VIS_HOP = 480         # 20 ms between frames, i.e. 50 shapes a second

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them: one callback period, plus whatever the DAC adds.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE      # ~43 ms
# How far past the device's own buffer the mouth's timeline may drift before it
# is re-anchored. The buffer is the hard limit on how much audio can be queued
# ahead, so anything beyond it plus a margin for clock error is impossible.
_CURSOR_SLACK = 0.15

# Playback headroom. Any Python thread can be held off the GIL for 30-55 ms
# while a tool does real work (measured: a busy tool thread pushed a 10 ms
# timer's lateness from ~2 ms to 55 ms). If the speaker buffer holds less than
# that, it runs dry and the voice breaks up — which is exactly the "choppy
# while thinking" symptom, since THINKING is when tools run. So the device is
# asked for at least this much buffer, and a dedicated writer thread keeps it
# topped up (see _play_audio).
_MIN_OUT_LATENCY = 0.25        # seconds of audio the device may hold
_SPK_AHEAD = 2                 # batches queued for the writer beyond the device

# Jitter buffer. Live audio arrives in bursts, not as an even stream, and the
# old loop started playing the instant the first 50 ms arrived — so the device
# never held more than whatever had come in, and any pause in delivery longer
# than that was a gap in the voice. Collecting this much first (or less, if the
# whole reply is already in) builds a cushion that then rides out the bursts.
# Tools that look things up rather than change things: if one is still running
# after _HANDOFF_S the model is answered with "still working" (so it can say "um
# momento" instead of sitting in silence) and the real result is delivered
# into the conversation when it lands, at the next pause in the talk.
_HANDOFF_TOOLS = {"web_search", "weather_report", "daily_briefing", "file_processor"}
# 6 s, not 3: a search usually lands in 3–6 s, and handing it off at 3 s made
# the real answer LATER (a "one moment" turn, then a wait for a pause, then a
# second turn) for the very requests that would have finished on their own.
_HANDOFF_S = 6.0
_LATENCY_LOG = BASE_DIR / "logs" / "latency.jsonl"

_PREBUFFER_S = 0.30
_PREBUFFER_MAX_WAIT = 0.45     # never hold the first word back longer than this
_PREBUFFER_BYTES = int(_PREBUFFER_S * RECEIVE_SAMPLE_RATE) * 2


class _AudioDiag:
    """Per-reply playback statistics, one line each in logs/audio_diag.log, so a
    choppy voice can be attributed instead of guessed at:

      net gaps      the speaker ran dry and NO audio was waiting — it had not
                    arrived yet (network / model generating slower than speech)
      local gaps    the speaker ran dry WHILE audio was waiting — this machine
                    did not hand it over in time (CPU / GIL)
      server cuts   the server stopped the reply because it heard speech —
                    with nobody talking, that is the speaker echoing into the mic
    """
    def __init__(self):
        self.path = BASE_DIR / "logs" / "audio_diag.log"
        self.reset()

    def reset(self):
        self.t_first = 0.0
        self.bytes = 0
        self.net_gaps = 0
        self.local_gaps = 0
        self.max_wait = 0.0
        self.cuts = 0
        self.rebuffers = 0

    def write(self, what: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {what}"
        print(f"[Audio] {what}")
        try:
            self.path.parent.mkdir(exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def summary(self, latency: float) -> None:
        if not self.bytes:
            return
        audio_s = self.bytes / 2 / RECEIVE_SAMPLE_RATE
        wall = time.monotonic() - self.t_first
        self.write(
            f"reply {audio_s:.1f}s of speech over {wall:.1f}s | "
            f"net gaps {self.net_gaps} (longest wait {self.max_wait*1000:.0f} ms) | "
            f"local gaps {self.local_gaps} | rebuffers {self.rebuffers} | "
            f"server cuts {self.cuts} | device buffer {latency*1000:.0f} ms")
        self.reset()

# Erring early is the safe direction. A viewer tolerates a mouth that moves
# slightly before the sound far better than one that moves after it — the
# broadcast limits are about 45 ms of lag against 125 ms of lead — so where
# this is uncertain it is biased to lead.


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, openness, width) frames, one per 20 ms.

    Returns [] on anything unexpected — the mouth falls back to loudness-only
    articulation rather than the caller having to handle an error.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_WIN:
            return []
        win = np.hanning(_VIS_WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(_VIS_WIN, 1.0 / sr)
        b_f1_lo = (freqs >= 150) & (freqs < 450)     # F1 of close vowels
        b_f1_hi = (freqs >= 450) & (freqs < 1100)    # F1 of open vowels
        b_f2_bk = (freqs >= 600) & (freqs < 1300)    # F2 of rounded vowels
        b_f2_fr = (freqs >= 1700) & (freqs < 3200)   # F2 of spread vowels
        b_hiss = (freqs >= 3800) & (freqs < 8000)    # fricatives

        # One frame per hop across the *whole* block. Stepping only while a full
        # window fits stopped 1024 - 480 samples short of the end, so a 200 ms
        # batch yielded 160 ms of schedule: the mouth ran out of frames before
        # the audio ran out of sound, and each batch no longer lined up with the
        # end of the one before it. Losing 20 % of every batch is most of why
        # the mouth did not track the words.
        out = []
        for start in range(0, x.size, _VIS_HOP):
            # The level gates closures, so it is measured over exactly this
            # 20 ms and never looks ahead. The spectrum needs a longer window
            # to resolve formants and may be short-filled at the very end.
            level = _pcm_level(x[start:start + _VIS_HOP])
            seg = x[start:start + _VIS_WIN]
            if seg.size < _VIS_WIN:
                seg = np.concatenate([seg, np.zeros(_VIS_WIN - seg.size,
                                                    dtype=np.float32)])
            if level <= 0.0:
                out.append((0.0, 0.0, 0.0))
                continue
            mag = np.abs(np.fft.rfft((seg - seg.mean()) * win))
            f1l, f1h = float(mag[b_f1_lo].sum()), float(mag[b_f1_hi].sum())
            f2b, f2f = float(mag[b_f2_bk].sum()), float(mag[b_f2_fr].sum())
            hiss = float(mag[b_hiss].sum())

            openness = f1h / (f1l + f1h + 1e-6)
            width = (f2f - f2b) / (f2f + f2b + 1e-6)
            # A wide-open jaw physically cannot purse, so openness damps width.
            # /a/ has a low enough F2 to read as "rounded" on the bands alone;
            # letting openness suppress the width term is what keeps an open
            # vowel from pursing.
            width *= (1.0 - openness) ** 0.8
            # Fricatives are formed with a nearly closed mouth.
            h = hiss / (f1l + f1h + f2b + f2f + hiss + 1e-6)
            openness *= 1.0 - 0.65 * min(1.0, h * 2.5)
            out.append((level,
                        float(min(1.0, max(0.0, openness))),
                        float(min(1.0, max(-1.0, width)))))
        return out
    except Exception:
        return []


def _describe_tools(declarations) -> str:
    """One line per capability, straight from the live tool declarations.

    Derived rather than written down: the action and plugin registries are
    discovered at startup, so whatever the user has installed is what the model
    is told it can do. Adding a plugin extends this by itself, and removing one
    stops the model from claiming an ability it no longer has.
    """
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# The assistant saying it will put something on the screen. When a reply says
# this and no display tool ran, nothing appears — it once promised a picture of
# the Ship of Theseus twice and called nothing either time.
_VISUAL_NOUN = re.compile(
    r"(?i)\b(imagem|imagens|holograma|desenho|ilustra\w*|diagrama|gr[aá]fico|cart[aã]o|foto|"
    r"esquema|mapa mental|na tela|no hud|image|hologram|drawing|diagram|chart|on (the )?screen)\b")
_VISUAL_VERB = re.compile(
    r"(?i)(\b(vou|vamos|deixa eu|j[aá] vou|vou te|t[oô]|estou|acabei de|olha|veja|"
    r"i'?ll|i will|i am going to|going to|let me|i'?m)\b.{0,40}?\b(mostr\w*|exib\w*|proje\w*|"
    r"desenh\w*|ger\w*|cri\w*|coloc\w*|bot\w*|apresent\w*|show\w*|display\w*|draw\w*|put\w*|"
    r"project\w*)|\baqui est[aá]\b|\bhere (is|are)\b|\bhere'?s\b|\bt[aá] na tela\b|\bj[aá] est[aá] na tela\b)")
_VISUAL_TOOLS = {"hud_display", "screen_overlay", "show_image", "generate_image"}

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined

# A tool call the model wrote out as text instead of calling it
# ("save_memory(category=identity, key=...)") — never part of the conversation.
_LEAKED_CALL_RE = re.compile(r"\b[a-z][a-z0-9_]{2,40}\((?:[a-z_]+\s*=[^()]*)?\)", re.IGNORECASE)


def _clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = _LEAKED_CALL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see JarvisLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "WHENEVER THE USER CORRECTS YOU or says how they want something done from now on "
            "('não, é o Waze', 'é velocidade 3, não velocidad', 'sempre faça X'), save it AT ONCE "
            "with category lessons and a general rule as the value — that is how you stop "
            "repeating a mistake. What you learn about their house (rooms, devices, which remote "
            "does what, routines) goes in category home. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "verify_security_keyword",
        "description": (
            "Check the user's voice security keyword. Ask the user to say it, then pass exactly "
            "what they said. Answers only whether it matches — you never get to know the keyword "
            "itself, so never guess it or say it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"attempt": {"type": "STRING", "description": "What the user said"}},
            "required": ["attempt"],
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVIS"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        # Speaker writer thread (see _play_audio). Interrupt bumps the
        # generation so batches already handed to the writer are dropped.
        self._spk_q: queue.Queue | None = None
        self._spk_gen = 0
        self._adiag = _AudioDiag()
        self._spk_starved = False      # writer ran dry mid-reply → rebuffer
        # Phone as speaker: when the phone was used to talk, replies play there
        # instead of on the PC. play_until is when the phone will have finished
        # what it was sent, so "speaking" (and the mic's echo guard) lasts as
        # long as the phone is actually talking.
        self._phone_play_until = 0.0
        # Tool calls run as their own tasks so receiving never stops for them.
        self._tool_tasks: set[asyncio.Task] = set()
        self._cancelled_calls: set[str] = set()
        # Transcript-driven mouth shapes for the avatar. Fed from the receive
        # loop as words arrive, drained by the playback loop against the audio.
        self._visemes              = VisemeStream()
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # WebRTC echo cancellation (core/aec.py): with it the mic stays open while
        # the assistant speaks, so talking over it interrupts, as in a call.
        self._aec_cfg = get_echo_cancel()
        self._aec = None
        if self._aec_cfg["enabled"]:
            try:
                from core import aec as _aec_mod
                self._aec = _aec_mod.get()
            except Exception as e:
                print(f"[AEC] {e}")
        # residual gate while speaking, as an int16 RMS
        self._aec_gate = 32768.0 * (10 ** (self._aec_cfg["residual_db"] / 20.0))
        self._aec_loud = 0
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The mouth is scheduled against this, never against "now": batches are
        # handed to the device far faster than they play, so "now" ran the lips
        # ahead of the words and cut every schedule short. 0 = nothing playing.
        self._play_cursor          = 0.0
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self._reconnect_event: asyncio.Event | None = None
        self._goaway_pending = False   # the server announced the end of this session
        self._connected_at = 0.0       # when the current session connected
        self._session_resumed = False  # …and whether it resumed an older one
        self._last_server_msg = 0.0    # last message of any kind from the server
        self._voiced_unanswered = 0.0  # seconds of speech sent since the server last said anything
        self._deaf_reconnect_at = 0.0  # last time the watchdog rebuilt a silent session
        self._phone_voice_until = 0.0  # the phone mic carries speech until then
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._tools_running    = 0      # tool calls being executed right now
        self._lat_pending      = False  # the user spoke; waiting for the reply's first audio
        self._lat_play_pending = False  # first audio arrived; waiting for it to play
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        self._recent_tools: list[str] = []         # last tool calls — what a correction is about
        self._turn_tools: set[str] = set()         # tools called during the current reply
        self._last_visual_nudge = 0.0

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel
        self.ui.request_say_idle = self.say_when_idle   # …and for results that can wait for a pause
        self._plugin_registry.start_all(self.ui)  # optional background hooks

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return
        instruction = self._as_taught(instruction)

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def say_when_idle(self, instruction: str, max_wait: float = 90.0) -> None:
        """Thread-safe: like plugin_say, but waits for a pause — the assistant
        not talking, no tool running, the user quiet for 0.8 s — so a result
        that arrives in the background never talks over anyone."""
        loop = getattr(self, "_loop", None)
        if not loop:
            return
        instruction = self._as_taught(instruction)

        async def _deliver():
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                busy = (self._is_speaking or self._tools_running > 0
                        or time.monotonic() - self._last_user_speech < 0.8
                        or not self.audio_in_queue.empty())
                if not busy and self.session:
                    break
                await asyncio.sleep(0.25)
            if not self.session:
                return
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[SayIdle] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_deliver(), loop)
        except Exception as e:
            print(f"[SayIdle] {e}")

    # "in one short sentence", "briefly"… — how long to talk is the user's to
    # teach (habit "notices" + lessons), not each plugin's to fix
    _HOW_LONG = re.compile(r"(?i),?\s*(in (one|ONE|a) (short |brief )?sentence|briefly|in one or two "
                           r"(short )?sentences)(,? in (their|the user's) (own )?language)?")

    @classmethod
    def _as_taught(cls, instruction: str) -> str:
        if not instruction.lstrip().startswith("["):
            return instruction
        body = cls._HOW_LONG.sub("", instruction)
        return f"{body}\n\nHow to say it: {learning.habit('notices')}{learning.guidance()}"

    @staticmethod
    def _lat_log(kind: str, **fields) -> None:
        """One line per measurement in logs/latency.jsonl — for before/after
        comparisons of how quickly the assistant answers."""
        try:
            _LATENCY_LOG.parent.mkdir(exist_ok=True)
            with open(_LATENCY_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                                    "kind": kind, **fields}) + "\n")
        except Exception:
            pass

    async def _run_tool_job(self, name: str, fn) -> tuple[str, bool]:
        """Run a blocking tool in the thread pool. For a look-up tool still
        busy after _HANDOFF_S, answer the model now and deliver the result when
        it lands. Returns (result, handed_off)."""
        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, fn)
        if name not in _HANDOFF_TOOLS:
            return (await fut), False
        try:
            return (await asyncio.wait_for(asyncio.shield(fut), _HANDOFF_S)), False
        except asyncio.TimeoutError:
            pass
        t0 = time.monotonic() - _HANDOFF_S
        print(f"[JARVIS] ⏳ {name} still running — handed off to the background")

        def _landed(f):
            try:
                r = f.result() or "Done."
            except Exception as e:
                r = f"Tool '{name}' failed: {e}"
            secs = time.monotonic() - t0
            print(f"[JARVIS] 📬 {name} landed after {secs:.1f}s → {str(r)[:80]}")
            self._lat_log("tool", name=name, s=round(secs, 2), handed_off=True)
            if name == "web_search" and not str(r).startswith(("No results", "Search failed")):
                try:
                    self.ui.show_content(name.upper(), str(r))
                except Exception:
                    pass
            self.say_when_idle(
                f"[TOOL RESULT] The {name} you started for the user has finished. Result:\n"
                f"{str(r)[:3500]}\n"
                f"Give the user the answer now, naturally, in their language. "
                f"Do not call {name} again for this.")
        fut.add_done_callback(_landed)
        return ("[STILL WORKING] This is taking a few seconds. "
                f"{learning.habit('still_working')} The result will arrive by itself as a "
                f"[TOOL RESULT] message.{learning.guidance()}"), True

    async def _reconnect_at_pause(self, time_left, fresh: bool = False) -> None:
        """After a GoAway: wait for a quiet moment (not talking, no tool
        running, the user silent) but never past the time the server gave,
        then reconnect keeping the conversation — or, when the session was a
        resumed one that got its GoAway straight away (`fresh`), start a new
        one: resuming it again only brings the same GoAway back, which is the
        reconnect loop seen in the logs."""
        m = re.search(r"(\d+(?:\.\d+)?)", str(time_left or ""))
        secs = float(m.group(1)) if m else 30.0
        if hasattr(time_left, "total_seconds"):
            secs = time_left.total_seconds()
        deadline = time.monotonic() + max(1.5, secs - 3.0)
        print(f"[JARVIS] ⏳ Server GoAway (≈{secs:.0f}s left) — reconnecting at the next pause")
        while time.monotonic() < deadline:
            quiet = (not self._is_speaking and self._tools_running == 0
                     and time.monotonic() - self._last_user_speech > 0.8
                     and self.audio_in_queue.empty())
            if quiet:
                break
            await asyncio.sleep(0.2)
        if fresh:
            print("[JARVIS] 🔗 Resumed session was already at its limit — starting a fresh one")
        self.request_reconnect(keep_context=not fresh,
                               reason="session limit (GoAway)" + (" — fresh session" if fresh else ""))

    async def _watch_deaf_session(self):
        """Session-scoped: the session can stay open yet stop answering — the
        user talks, nothing comes back, and only a restart helped. If 1 s or
        more of speech went out and the server has said nothing at all for 20 s,
        rebuild the session (keeping the conversation). At most once every
        3 minutes, so steady noise such as music can't make it churn."""
        while True:
            await asyncio.sleep(2.0)
            now = time.monotonic()
            # A microphone that stopped delivering audio altogether — seen when
            # switching to another window: reopen the audio devices (the session
            # rebuild reopens them) and keep the conversation.
            mic_at = getattr(self, "_mic_cb_at", 0.0)
            if (mic_at and now - mic_at > 3.0 and now - self._connected_at > 8.0
                    and now - getattr(self, "_mic_reopen_at", 0.0) > 30.0):
                self._mic_reopen_at = now
                print(f"[JARVIS] 🩺 Microphone delivered nothing for {now - mic_at:.0f}s — reopening audio")
                self.request_reconnect(keep_context=True, reason="microphone stalled")
                return
            # Every 30 s: if the mic delivered but nothing was sent, say which
            # gate held it — so a deaf spell can be explained from the log.
            g = getattr(self, "_mic_gate", None)
            if g is not None:
                last = getattr(self, "_mic_gate_last", None)
                if last is None or now - last[0] >= 30.0:
                    if last is not None:
                        d = {k: g[k] - last[1].get(k, 0) for k in g}
                        if d["calls"] > 0 and d["sent"] == 0:
                            print("[MicDiag] 30 s without sending to the server — blocks: "
                                  + ", ".join(f"{k} {v}" for k, v in d.items() if v and k != "calls")
                                  + f" (speaking={self._is_speaking}, muted={self.ui.muted}, "
                                  f"phone={self._phone_active})")
                    self._mic_gate_last = (now, dict(g))
            # no reply lasts a minute: a "speaking" flag that old is stuck, and
            # it keeps the mic closed (e.g. the speaker device hung)
            if self._is_speaking and now - getattr(self, "_speaking_since", now) > 60.0:
                print("[JARVIS] 🩺 'Speaking' for over 60 s — reopening the mic")
                self.set_speaking(False)
            if (self._voiced_unanswered >= 1.0
                    and now - self._last_server_msg > 20.0
                    and now - self._connected_at > 15.0
                    and now - self._deaf_reconnect_at > 180.0
                    and not self._is_speaking and self._tools_running == 0):
                self._deaf_reconnect_at = now
                print(f"[JARVIS] 🩺 Spoke {self._voiced_unanswered:.1f}s, no reply from the server for "
                      f"{now - self._last_server_msg:.0f}s — rebuilding the session")
                self.request_reconnect(keep_context=True, reason="no reply from the server")
                return

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _on_phone_control(self, action: str) -> None:
        """The phone HUD's buttons."""
        if action == "interrupt":
            self.interrupt()
        elif action == "mute":
            self.ui.muted = not self.ui.muted

    def _modules_summary(self) -> list[dict]:
        """The modules for the phone's sub-screen: name and a one-line note."""
        if getattr(self, "_modules_cache", None) is None:
            try:
                from ui_hub import META
            except Exception:
                META = {}
            out = []
            try:
                names = [d["name"] if isinstance(d, dict) else d.name
                         for d in self._plugin_registry.get_tool_declarations()]
            except Exception:
                names = []
            for n in names:
                title, sub = (META.get(n) or (n.replace("_", " ").title(), ""))[:2]
                out.append({"name": title, "note": sub,
                            "group": (META.get(n) or ("", "", "", "personal"))[3]})
            self._modules_cache = out
        return self._modules_cache

    async def _push_hud_state(self) -> None:
        """Every 2 s, the HUD's live numbers to the phone (core/hud_data.py)."""
        try:
            from core import hud_data
        except Exception:
            return
        while True:
            await asyncio.sleep(2.0)
            try:
                if not self._dashboard or not self._dashboard._clients:
                    continue
                d = hud_data.snapshot()
                d["name"] = getattr(self, "_asst_name", "") or d.get("name")
                d["modules"] = self._modules_summary()
                await self._dashboard.hud_push({"type": "hudstate", "d": d})
            except Exception as e:
                print(f"[HUD] phone push: {type(e).__name__}: {e}")

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        voice_guard.note_text_input()
        confirm_gate.heard(text, typed=True)
        if not self._loop or not self.session:
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Jarvis" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            if value and not self._is_speaking:
                self._speaking_since = time.monotonic()
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake it, so push-to-talk works
            # without having to say the wake word first.
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        try:
            self.ui.set_state("LISTENING" if held else "SLEEPING")
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        drained = 0
        if q:
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
        self._spk_gen += 1
        # Silence the phone too, if it is the one talking.
        if self._phone_play_until > time.time() and self._dashboard and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._dashboard.send_phone_control({"type": "flush"}), self._loop)
            except Exception:
                pass
        self._phone_play_until = 0.0
        sq = self._spk_q
        if sq:
            while True:
                try:
                    sq.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
        if q or sq:
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        # The words we were about to mouth are never going to be spoken now.
        self._visemes.reset()
        self._play_cursor = 0.0     # next batch starts a fresh timeline
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user with the ordinary respectful form '
                      'for a superior in the language you are currently speaking — '
                      '"sir" in English, its everyday equivalent in any other '
                      'language. Never an archaic or aristocratic form, and never '
                      'the form from a different language than the one you are '
                      'speaking in this sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations())
        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": _describe_tools(_all_decls),
            "limits": _describe_limits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        # The conversation so far, from disk — but only for a fresh session. A
        # resumed one (network blip, device change) still has it server-side.
        if self._resume_handle is None:
            recent = conversation.prompt_block(self._asst_name)
            if recent:
                parts.append(recent)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": _all_decls}],
            # Asks the server to issue session-resumption handles. Enabled but
            # NOT yet used: no handle is captured from the resumption update and
            # none is passed back here, so a reconnect starts a fresh session
            # rather than continuing the old one. Left on because the handles
            # cost nothing and are the prerequisite for doing it properly.
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self._turn_tools.add(name)
        try:
            self._recent_tools.append(f"{name} {json.dumps(args, ensure_ascii=False)[:200]}")
            del self._recent_tools[:-8]
            if name not in ("save_memory", "recall_memory"):
                # the action log routines are learned from (memory/db.py)
                from memory.memory_manager import _db as _mdb
                _tgt = str(args.get("device") or args.get("value") or args.get("name") or "")[:60]
                _mdb().log_event("tool", name, str(args.get("action") or ""), _tgt,
                               json.dumps(args, ensure_ascii=False)[:300])
        except Exception:
            pass
        self.ui.set_state("THINKING")
        _t_tool = time.monotonic()
        _handed_off = False


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                from memory.memory_manager import changes_by
                with changes_by("assistant"):
                    update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "verify_security_keyword":
                from memory.memory_manager import verify_secret
                ok = await asyncio.to_thread(verify_secret, str(args.get("attempt", "")))
                result = ("MATCH — the keyword is correct; the user is verified." if ok
                          else "NO MATCH — that is not the security keyword. Do not reveal or hint at it.")

            elif name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    # The image is attached to this same exchange, so there is
                    # nothing to stall for and nothing to announce. Asking for an
                    # acknowledgement here is what produced two spoken answers —
                    # the model filled that turn by answering the question from
                    # imagination, then answered it again once it could see.
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "[SHUTDOWN] " + learning.habit("goodbye")
                                                                  + learning.guidance()}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r, _handed_off = await self._run_tool_job(
                    name, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r and not _handed_off
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r, _handed_off = await self._run_tool_job(
                        name,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        _secs = time.monotonic() - _t_tool
        print(f"[JARVIS] 📤 {name} ({_secs:.1f}s) → {str(result)[:80]}")
        if not _handed_off:
            self._lat_log("tool", name=name, s=round(_secs, 2))

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )
            try:   # for _watch_deaf_session: how much speech went unanswered
                pcm = np.frombuffer(msg["data"], np.int16)
                if pcm.size and float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) > 600.0:
                    self._voiced_unanswered += pcm.size / 16000.0
            except Exception:
                pass

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        self._mic_cb_at = 0.0
        self._mic_gate = {"calls": 0, "sent": 0, "asleep": 0, "speaking": 0, "tail": 0,
                          "ptt": 0, "muted": 0, "phone": 0}
        gate = self._mic_gate

        def callback(indata, frames, time_info, status):
            # for _watch_deaf_session: is the mic delivering, and where does it stop?
            self._mic_cb_at = time.monotonic()
            gate["calls"] += 1
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                gate["asleep"] += 1
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Echo cancellation ────────────────────────────────────────────
            # What the speakers play is subtracted from the microphone, so what
            # is left is the user. While the assistant speaks the cleaned audio
            # is streamed — quiet residue as silence — and the server hears an
            # interruption. Without the canceller, the old behaviour below.
            aec = self._aec
            if aec is not None:
                try:
                    indata = aec.process_near(indata)
                except Exception:
                    pass
                if jarvis_speaking:
                    try:
                        rms = float(np.sqrt(np.mean(np.asarray(indata, dtype=np.float32) ** 2)))
                    except Exception:
                        rms = 0.0
                    # two loud blocks in a row (≈130 ms) before it counts as a voice
                    self._aec_loud = self._aec_loud + 1 if rms >= self._aec_gate else 0
                    if self._aec_loud < 2:
                        indata = np.zeros_like(indata)
                    gate["speaking"] += 1
                    jarvis_speaking = False          # stream it (below), don't close the mic
                    self._tail_until = 0.0           # the canceller handles the tail too
                else:
                    self._aec_loud = 0

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks.
                #
                # Interrupting by voice used to live here: `EchoGuard` can pick a
                # user out from under our own echo, and `core/echo.py` still does
                # that for the tail below. Re-enabling is small — classify each
                # block here and call interrupt() after `required_blocks` of
                # agreement — but it depends on the listener's room, so it stays
                # out until it can be tried on real hardware.
                gate["speaking"] += 1
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        gate["tail"] += 1
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    gate["tail"] += 1
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                gate["ptt"] += 1
                return
            if self.ui.muted:
                gate["muted"] += 1
            elif self._phone_active:
                gate["phone"] += 1
            else:
                gate["sent"] += 1
            
            if not self.ui.muted and not self._phone_active:
                # Voice ID: strict mode may hold an utterance until it is
                # verified (then release it whole) or drop it; otherwise this
                # is the chunk itself.
                for data in voice_guard.route(indata):
                    loop.call_soon_threadsafe(
                        self.out_queue.put_nowait,
                        {"data": data, "mime_type": "audio/pcm"}
                    )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _answer_tool_calls(self, calls) -> None:
        """Execute one tool_call message's functions, in order, and answer them
        in a single tool response — off the receive loop (see _receive_audio)."""
        session = self.session
        try:
            fn_responses = []
            self._tools_running += 1
            try:
                for fc in calls:
                    print(f"[JARVIS] 📞 {fc.name}")
                    fn_responses.append(await self._execute_tool(fc))
            finally:
                self._tools_running -= 1
            fn_responses = [fr for fr in fn_responses
                            if getattr(fr, "id", None) not in self._cancelled_calls]
            if not fn_responses or session is None or session is not self.session:
                return      # all withdrawn, or the session it belonged to is gone
            await session.send_tool_response(function_responses=fn_responses)
            await self._flush_pending_vision()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[JARVIS] ❌ Tool response: {e}")
            traceback.print_exc()
        finally:
            for fc in calls:
                self._cancelled_calls.discard(getattr(fc, "id", None))

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64
        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
               else "[IMAGE SOURCE: SCREEN CAPTURE]")
        await self.session.send_client_content(
            turns={"role": "user", "parts": [
                {"inline_data": {"mime_type": mime_t, "data": b64}},
                {"text": f"{src}\n\n{question}"},
            ]},
            turn_complete=True,
        )

        if self._vision_cam_active:
            # Camera: stay busy until JARVIS has finished speaking the answer,
            # then close the preview.
            self._vision_cam_active    = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    self._last_server_msg = time.monotonic()
                    self._voiced_unanswered = 0.0

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    # ── GoAway ───────────────────────────────────────────────
                    # A Live session has a maximum length; shortly before it,
                    # the server says so and expects the client to reconnect.
                    # Ignored, it aborts the socket itself (1008 "failed to
                    # close the connection after receiving a GoAway") and the
                    # assistant goes deaf until the error path reconnects. Hand
                    # over at the next pause instead, with the resumption handle,
                    # so the conversation carries on unnoticed.
                    _ga = getattr(response, "go_away", None)
                    if _ga is not None and not self._goaway_pending:
                        self._goaway_pending = True
                        _stale = (self._session_resumed
                                  and time.monotonic() - self._connected_at < 120.0)
                        asyncio.create_task(self._reconnect_at_pause(getattr(_ga, "time_left", None),
                                                                     fresh=_stale))

                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            if self._lat_pending:
                                self._lat_pending = False
                                self._lat_server = time.monotonic() - self._last_user_speech
                                self._lat_play_pending = True
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if getattr(sc, "interrupted", False):
                            self._adiag.cuts += 1
                            if self._aec is not None and not self._interrupted:
                                # barge-in: the server stopped generating; the
                                # audio already queued here must stop as well
                                print("[JARVIS] 🗣️ Interrupted by voice")
                                self.interrupt()
                            elif not self._interrupted:
                                self._adiag.write(
                                    "server cut the reply short: it heard speech. If "
                                    "nobody spoke, the mic is picking up the speakers "
                                    "(lower the volume, use headphones, or enable "
                                    "push-to-talk).")

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)
                                # Hand the words to the mouth as they arrive, so
                                # the avatar can form the consonants the audio
                                # alone cannot show. Pure string work — it adds
                                # nothing measurable to the response path.
                                self._visemes.feed_text(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                confirm_gate.heard(txt)      # an answer to a waiting permission?
                                self._last_user_speech = time.monotonic()
                                self._lat_pending = True

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()
                            try:
                                confirm_gate.turn_done()     # "pode" / "não" to a waiting permission
                            except Exception as e:
                                print(f"[Confirm] {e}")

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                self._visemes.reset()
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""   # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if learning.is_correction(full_in):
                                    _turns = list(self._session_log[-10:])
                                    _tools = list(self._recent_tools)
                                    _n = len(self._session_log)

                                    async def _learn(_turns=_turns, _tools=_tools, _n=_n):
                                        await asyncio.sleep(4)      # let the reply land first
                                        rule = await asyncio.to_thread(
                                                learning.learn_from_correction,
                                            _turns + self._session_log[_n:_n + 2], _tools)
                                        if rule:
                                            self.ui.write_log(f"SYS: 📘 Aprendido: {rule}")
                                    asyncio.create_task(_learn())
                                conversation.append("user", full_in)
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            _promised = bool(full_out and _VISUAL_NOUN.search(full_out)
                                             and _VISUAL_VERB.search(full_out))
                            if _promised and not (self._turn_tools & _VISUAL_TOOLS) \
                                    and time.monotonic() - self._last_visual_nudge > 20:
                                self._last_visual_nudge = time.monotonic()

                                async def _visual_nudge(said=full_out[:300]):
                                    await asyncio.sleep(1.5)          # a late tool call still counts
                                    if self._turn_tools & _VISUAL_TOOLS or not self.session:
                                        return
                                    print("[JARVIS] 🖼️ promised a visual but called nothing — nudging")
                                    try:
                                        await self.session.send_client_content(
                                            turns={"role": "user", "parts": [{"text": (
                                                "[SYSTEM_NUDGE] You just told the user you would show "
                                                "something on the screen, but you did not call hud_display, "
                                                "so NOTHING appeared. Call hud_display now with what you "
                                                "promised — for an idea, concept or scene use draw with a "
                                                "short English description of the picture. Do not apologise "
                                                "and do not explain; after it is shown say at most a few "
                                                f"words. What you said: \"{said}\"")}]},
                                            turn_complete=True)
                                    except Exception as e:
                                        print(f"[JARVIS] visual nudge: {e}")
                                asyncio.create_task(_visual_nudge())
                            self._turn_tools = set()
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                conversation.append("assistant", full_out)
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            if self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    # The server withdrew calls (the user talked over it):
                    # their answers must not be sent.
                    canc = getattr(response, "tool_call_cancellation", None)
                    if canc and getattr(canc, "ids", None):
                        self._cancelled_calls.update(canc.ids)
                        print(f"[JARVIS] ✋ Tool call(s) cancelled: {list(canc.ids)}")

                    if response.tool_call:
                        # Run the tools in their own task and keep receiving.
                        # This used to be awaited right here, which stopped the
                        # receive loop for as long as the tool took: the rest of
                        # the sentence the model was already speaking ("let me
                        # check…") sat unread in the socket, the speaker ran dry,
                        # and the voice broke up exactly while THINKING.
                        calls = list(response.tool_call.function_calls or [])
                        t = asyncio.create_task(self._answer_tool_calls(calls))
                        self._tool_tasks.add(t)
                        t.add_done_callback(self._tool_tasks.discard)
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            # The device's own "high" latency, but never under _MIN_OUT_LATENCY:
            # that floor is what lets playback ride out a busy GIL.
            try:
                info = sd.query_devices(dev if dev is not None else sd.default.device[1])
                lat = max(_MIN_OUT_LATENCY, float(info.get("default_high_output_latency", 0) or 0))
            except Exception:
                lat = _MIN_OUT_LATENCY
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
                latency=lat,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
            if self._aec is not None:
                self._aec.set_delay(self._aec_cfg["delay_ms"] or int(self._out_latency * 1000) + 40)
        except Exception:
            pass

        # The writer: a thread that does nothing but hand audio to the device.
        # Before, each batch was written from the asyncio loop via to_thread, so
        # the next batch waited for that loop to come round again — behind the
        # network receive, the microphone and every tool response. A blocking
        # write releases the GIL, so this thread needs it only for a moment per
        # batch, and the device buffer stays full while everything else is busy.
        spk_q: queue.Queue = queue.Queue()
        self._spk_q = spk_q

        diag = self._adiag

        def _writer():
            # When the audio handed over so far finishes playing. The device
            # drains at exactly real time, so a chunk arriving after this moment
            # means the speaker played silence in between — an audible gap.
            # Measured by the clock because MME (the usual Windows output) never
            # reports underflows: a blocking write returns False regardless.
            play_end = 0.0
            while True:
                t_wait = time.monotonic()
                item = spk_q.get()
                waited = time.monotonic() - t_wait
                if item is None:
                    return
                gen, data, starts_reply = item
                if gen != self._spk_gen:      # interrupted after it was queued
                    continue
                now = time.monotonic()
                if starts_reply:
                    play_end = 0.0
                elif play_end and now > play_end + 0.03:
                    # If the writer had been waiting on an empty queue the audio
                    # had not arrived yet; otherwise it was here and this
                    # machine was late handing it over.
                    if waited > 0.03:
                        diag.net_gaps += 1
                        diag.max_wait = max(diag.max_wait, now - play_end)
                        self._spk_starved = True
                    else:
                        diag.local_gaps += 1
                play_end = max(play_end, now) + len(data) / 2 / RECEIVE_SAMPLE_RATE
                if self._aec is not None:
                    try:
                        self._aec.feed_far(data)
                    except Exception:
                        pass
                try:
                    stream.write(data)
                except Exception as e:
                    print(f"[JARVIS] ❌ Speaker write: {e}")
                    return

        writer = threading.Thread(target=_writer, daemon=True, name="jarvis-speaker")
        writer.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                        and spk_q.empty()
                        and time.time() >= self._phone_play_until
                    ):
                        self._adiag.summary(self._out_latency)
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    elif (
                        # The mic is closed while the assistant talks and only
                        # reopened by the server's turn_complete. A reply that
                        # ends without one (a proactive line, an interrupted or
                        # tool-calling turn, a late chunk after turn_complete)
                        # left it closed: the assistant stayed deaf until some
                        # other reply happened to complete. Nothing to play for
                        # 4 s and the speaker drained = the reply is over.
                        self._is_speaking
                        and self.audio_in_queue.empty()
                        and spk_q.empty()
                        and time.time() >= self._phone_play_until
                        and time.monotonic() - getattr(self, "_last_audio_rx", 0.0) > 4.0
                    ):
                        print("[JARVIS] 🩺 Reply ended without turn_complete — reopening the mic")
                        self._adiag.summary(self._out_latency)
                        self.set_speaking(False)
                    continue
                self._last_audio_rx = time.monotonic()

                # Start of a reply, or the speaker ran dry mid-reply: collect a
                # cushion before playing (see _PREBUFFER_S).
                starts_reply = not self._is_speaking
                if not self._is_speaking or self._spk_starved:
                    if self._spk_starved:
                        self._adiag.rebuffers += 1
                    self._spk_starved = False
                    gen0 = self._spk_gen
                    buf = bytearray(chunk)
                    t0 = time.monotonic()
                    while (len(buf) < _PREBUFFER_BYTES
                           and time.monotonic() - t0 < _PREBUFFER_MAX_WAIT
                           and self._spk_gen == gen0):
                        try:
                            buf.extend(self.audio_in_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                break          # the whole reply is already here
                            await asyncio.sleep(0.01)
                    if self._spk_gen != gen0:
                        continue               # interrupted while buffering
                    chunk = bytes(buf)
                if self._lat_play_pending:
                    self._lat_play_pending = False
                    _srv = getattr(self, "_lat_server", 0.0)
                    _play = time.monotonic() - self._last_user_speech
                    if 0 < _srv < 30 and _play < 30:
                        _hear = _play + (self._out_latency or 0)
                        print(f"[Timing] ⏱ resposta: modelo {_srv:.2f}s · tocando {_play:.2f}s "
                              f"· ouvida ≈{_hear:.2f}s após você parar")
                        self._lat_log("reply", model=round(_srv, 2), play=round(_play, 2),
                                      heard=round(_hear, 2))
                if not self._adiag.bytes:
                    self._adiag.t_first = time.monotonic()
                self._adiag.bytes += len(chunk)

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        more = self.audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    batch.extend(more)
                    self._adiag.bytes += len(more)

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms viseme frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    # The writer's queue sits in front of the device buffer, so
                    # the timeline may legitimately run that much further ahead.
                    horizon = self._out_latency + _CURSOR_SLACK + _SPK_AHEAD * 0.2
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                # Reply on the phone when the phone was the one talking.
                if self._dashboard is not None and self._dashboard.phone_wants_audio():
                    now_t = time.time()
                    self._phone_play_until = (max(self._phone_play_until, now_t + 0.15)
                                              + len(batch) / 2 / RECEIVE_SAMPLE_RATE)
                    asyncio.create_task(self._dashboard.send_phone_audio(bytes(batch)))
                    # Keep roughly real time so an interrupt still has audio
                    # left to cut instead of everything already being sent.
                    while self._phone_play_until - time.time() > 1.0:
                        await asyncio.sleep(0.05)
                    continue
                spk_q.put((self._spk_gen, bytes(batch), starts_reply))
                # Stay at most a couple of batches ahead of the writer. This is
                # the pacing the blocking write used to give: without it the
                # loop would race ahead of the speaker, and the mouth's timeline
                # (anchored to "now") would run seconds early.
                while spk_q.qsize() > _SPK_AHEAD:
                    await asyncio.sleep(0.02)
                if not writer.is_alive():
                    raise RuntimeError("speaker writer stopped")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            self._spk_q = None
            spk_q.put(None)
            try:
                stream.abort()        # unblocks a write in progress
            except Exception:
                pass
            writer.join(timeout=1.0)
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        The opening briefing, in two phases so speech starts at once:
          Phase 1 — a greeting with no tools; speech starts in under a second.
          Phase 2 — weather, today's agenda, national and city news, gathered in
                    parallel (core/briefing.py) while phase 1 plays, then handed
                    over as ready text — no tool round trip — and shown on the
                    content panel.
        """
        from core import briefing

        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        now = datetime.now()
        time_str = now.strftime("%H:%M")
        part_of_day = ("morning" if 5 <= now.hour < 12 else
                       "afternoon" if now.hour < 18 else "evening")

        # Start gathering at once — it runs while the greeting plays.
        loop = asyncio.get_event_loop()
        brief_future = loop.run_in_executor(None, briefing.gather)

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            # data, not an order: whether to bring it up is the greeting habit's call
            session_clause = f" [LAST TIME, {_when}: {last['summary']}]"

        p1 = (
            f"[GREETING] It is the {part_of_day}, {time_str}. {learning.habit('greeting')}"
            f"{session_clause} Do not call any tools.{lang_clause}{name_clause}"
            f"{learning.guidance()}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        print("[JARVIS] Briefing phase 1 (greeting) sent.")

        # ── Phase 2: the briefing itself, as soon as the greeting is done ─────
        async def _deliver_briefing():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                    except asyncio.TimeoutError:
                        pass
                # turn_complete marks the end of *generation*; the greeting may
                # still be playing. A beat here keeps phase 2 from talking over it.
                await asyncio.sleep(0.8)

                try:
                    b = await asyncio.wait_for(asyncio.wrap_future(brief_future), timeout=10.0)
                except Exception as e:
                    self.ui.write_log(f"SYS: Briefing data timed out/failed: {e!r}")
                    b = {}

                if not self.session:
                    return

                got = [k for k in ("weather", "agenda", "national", "local") if b.get(k)]
                if not got:
                    p2 = ("The weather, agenda and news could not be fetched right now "
                          f"(no connection?). Tell the user briefly.{lang_str}")
                else:
                    text = briefing.render(b)
                    self.ui.show_content("BRIEFING — " + datetime.now().strftime("%d/%m %H:%M"), text)
                    city_name = b.get("city") or ""
                    extra = ""
                    if not city_name:
                        extra += (" The user's city is not known yet: at the end, ask which "
                                  "city they live in so local news and weather can be "
                                  "included; when they answer, call daily_briefing with "
                                  "action=set_city.")
                    ag = b.get("agenda") or {}
                    if ag and not ag.get("configured"):
                        extra += (" No calendar is linked: in one short clause, mention they "
                                  "can link their Google Calendar in the settings or ask you "
                                  "to note events.")
                    # how to deliver it is a habit the user can correct, not code
                    p2 = (
                        f"[BRIEFING DATA]\n{text}\n\n"
                        f"Deliver the briefing now. How: {learning.habit('briefing')} "
                        f"Local news are from {city_name or 'their city'}. Do not call any tools."
                        f"{extra}{lang_str}{learning.guidance()}"
                    )

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                print(f"[JARVIS] Briefing phase 2 sent ({', '.join(got) or 'nothing fetched'}).")
            except Exception as e:
                print(f"[JARVIS] Briefing phase 2 failed: {e}")
                self.ui.write_log("SYS: Could not prepare the briefing.")

        asyncio.create_task(_deliver_briefing())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._awake:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        # All of them in ONE turn: one turn per alert, 6 s apart, had
                        # each answer start on top of the one still being spoken.
                        if alerts:
                            listed = "\n".join(f"{i}. {a}" for i, a in enumerate(alerts, 1))
                            msg = (
                                f"{listed}\n\n"
                                f"These are {len(alerts)} new developments on topics the user "
                                f"follows. Tell the user about them in {lang}. How: "
                                f"{learning.habit('topic_alerts')}{learning.guidance()}"
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            print(f"[JARVIS] Monitor alerts sent together ({len(alerts)}).")
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                print("[JARVIS] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session.

        The phone takes the floor from the PC mic only while it carries SPEECH.
        It used to silence the PC mic for as long as any audio arrived, so a
        phone left listening in a pocket — streaming silence — made the
        assistant deaf at the desk. Silent phone audio is dropped (the two mics
        can't be interleaved into one stream); a short pre-roll keeps the first
        syllable when speech starts."""
        q = self._dashboard._phone_audio_queue
        pre: deque = deque(maxlen=4)          # ~250 ms before the voice starts
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                self._phone_active = False
                pre.clear()
                continue
            now = time.monotonic()
            try:
                pcm = np.frombuffer(chunk["data"], np.int16)
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if pcm.size else 0.0
            except Exception:
                rms = 1000.0
            if rms > 420.0:                   # speech on the phone
                if not self._phone_active:
                    print("[JARVIS] 📱 phone mic has the floor")
                self._phone_voice_until = now + 1.6
            was_active = self._phone_active
            self._phone_active = now < self._phone_voice_until
            if not self._phone_active:
                pre.append(chunk)             # silence: keep a little, send nothing
                continue
            voice_guard.note_text_input()   # paired phone: an authenticated channel
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                batch = (list(pre) if not was_active else []) + [chunk]
                pre.clear()
                for c in batch:
                    try:
                        self.out_queue.put_nowait(c)
                    except asyncio.QueueFull:
                        pass

    _RELAY_DEFAULT_APPS = ("whatsapp", "telegram", "messages", "mensagens", "signal",
                           "instagram", "messenger", "sms")

    def _on_phone_notification(self, n: dict) -> None:
        """A notification arrived from the Android app. Messages from the apps
        the user chose are announced; everything else is only kept for
        'what notifications do I have?'."""
        try:
            from memory.config_manager import get_plugin_config
            cfg = get_plugin_config("android_phone")
        except Exception:
            cfg = {}
        if str(cfg.get("relay", "on")).lower() == "off":
            return
        apps = ([a.strip().lower() for a in (cfg.get("relay_apps") or "").split(",") if a.strip()]
                or list(self._RELAY_DEFAULT_APPS))
        who = f"{n.get('app', '')} {n.get('package', '')}".lower()
        if not any(a in who for a in apps):
            return
        if not (self.session and self._awake and not self.ui.muted):
            return
        # At most one announcement per app every 20 s: a burst of messages from
        # one chat is one interruption, not ten.
        now = time.monotonic()
        last = getattr(self, "_relay_last", {})
        if now - last.get(n.get("app"), 0) < 20:
            return
        last[n.get("app")] = now
        self._relay_last = last
        self.ui.write_log(f"SYS: 📱 {n.get('app')}: {n.get('title')}")
        self.plugin_say(
            f"[PHONE NOTIFICATION] {n.get('app')} — {n.get('title')}: {n.get('text', '')[:300]}\n"
            f"Tell the user in one short sentence, in their language, who wrote and the gist "
            f"(do not read a long message in full). "
            + ("If it suits, offer to reply." if n.get("can_reply") else ""))

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # From a paired phone or a logged-in browser: authenticated,
                # so it counts like typing at the PC for Voice ID.
                voice_guard.note_text_input()
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes JARVIS if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)
        voice_guard.set_logger(self.ui.write_log)
        learning.start_background()        # daily memory consolidation (memory/learning.py)
        if voice_guard.enrolled():
            voice_guard.preload()        # first check must not pay for loading the model

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            self._dashboard.set_notification_callback(self._on_phone_notification)
            asyncio.create_task(self._dashboard.serve())
            # The phone's HUD: the same live numbers as the PC's, and its buttons
            self._dashboard.set_control_callback(self._on_phone_control)
            # files sent from the phone's 📎 reach the assistant like ARQUIVOS on the PC
            self._dashboard.set_upload_callback(lambda path: self.ui.load_file(path))
            asyncio.create_task(self._push_hud_state())
            # The HUD's cards, mirrored to the phone app (core/hud_mirror.py)
            from core import hud_mirror
            _dash, _loop = self._dashboard, asyncio.get_event_loop()
            hud_mirror.set_sink(lambda m: asyncio.run_coroutine_threadsafe(_dash.hud_push(m), _loop))
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                # Pick the rung to open the conversation on. A model resting
                # off a quota limit is skipped; the name is published back to
                # LIVE_MODEL so plugins follow whatever is actually in use.
                global LIVE_MODEL
                LIVE_MODEL = _gemini.live_model()
                live_model = LIVE_MODEL
                print(f"[JARVIS] Live model: {live_model}")

                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=live_model, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Jarvis" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: JARVIS online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    self._goaway_pending = False
                    self._connected_at = self._last_server_msg = time.monotonic()
                    self._session_resumed = _resumed_with
                    self._voiced_unanswered = 0.0
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._watch_deaf_session())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Out of quota, or this model is not available to this key —
                # step down the ladder and reconnect straight away. This is the
                # difference between "JARVIS is quieter today" and "JARVIS does
                # not start today": one model means one daily limit, and the
                # limit always arrives mid-conversation.
                if _gemini.note_live_failure(live_model, err_str):
                    nxt = _gemini.live_model()
                    self.ui.write_log(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota."
                        if nxt != live_model else
                        "SYS: Every live model is rate-limited — retrying.")
                    self._conn_backoff = 0 if nxt != live_model else 15
                    if nxt == live_model:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    self._conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    # never from 0: after a model switch it was 0, and 0 × 2 retried
                    # in a tight loop all the time the network was down
                    _conn_backoff = min(max(getattr(self, "_conn_backoff", 3), 1.5) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Tools still running belong to the session that just ended;
                # their answers have nowhere to go.
                for t in list(self._tool_tasks):
                    t.cancel()
                self._tool_tasks.clear()
                self._cancelled_calls.clear()
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

_PID_FILE = BASE_DIR / "logs" / "mark.pid"


def _take_over_previous_instance() -> None:
    """Only one assistant at a time. A copy left running in the background —
    its window closed while a network call held the process open, or simply
    launched twice — keeps its own mic, its own Gemini session, the phone
    dashboard's ports, and talks over the new one: the "session drops and it
    stops answering" seen in the logs. Starting again ends that copy."""
    me = os.getpid()
    try:
        import psutil
        here = str(Path(__file__).resolve()).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] == me or "python" not in (proc.info["name"] or "").lower():
                    continue
                cmd = " ".join(proc.info["cmdline"] or []).lower()
                if "main.py" not in cmd:
                    continue
                if here not in cmd:
                    try:   # started as "pythonw main.py" from the project folder
                        if Path(proc.cwd()).resolve() != BASE_DIR.resolve():
                            continue
                    except Exception:
                        continue
                print(f"[JARVIS] ♻ Another copy of the assistant is running (pid {proc.pid}) — closing it")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        print(f"[JARVIS] single-instance check skipped: {e}")


# ── One assistant at a time ──────────────────────────────────────────────────
# A named mutex says whether a copy is already running; the running copy listens
# on a localhost port (written to logs/mark.pid with its pid) for "activate".
# Launching again — the shortcut, a double click — brings that window to the
# front and exits, so two assistants can never share the mic again. Only a copy
# that does not answer (frozen, or left without a window) is replaced.
import hashlib as _hashlib
_INSTANCE_NAME = "Local\\MarkLV-" + _hashlib.md5(str(BASE_DIR.resolve()).lower().encode()).hexdigest()[:12]
_instance = {"mutex": None, "cb": None, "pending": False, "bridge": None}


def _claim_instance() -> bool:
    """True when this is the only copy (the named mutex is now ours)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        h = k32.CreateMutexW(None, False, _INSTANCE_NAME)
        if ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
            k32.CloseHandle(ctypes.c_void_p(h))
            return False
        _instance["mutex"] = h                    # held until the process ends
        return True
    except Exception as e:
        print(f"[JARVIS] instance lock unavailable: {e}")
        return True


def _ask_running_instance(timeout: float = 12.0) -> bool:
    """Ask the open copy to come to the front. True if it answered. Retries for
    a while: the other copy may itself still be starting."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = json.loads(_PID_FILE.read_text())
            try:   # this process was just launched by the user, so it may hand
                   # the foreground over to the other one
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(int(info["pid"]))
            except Exception:
                pass
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2) as c:
                c.settimeout(4)
                c.sendall(b"activate\n")
                if c.recv(32).startswith(b"ok"):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_instance_server() -> None:
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    try:
        _PID_FILE.parent.mkdir(exist_ok=True)
        _PID_FILE.write_text(json.dumps({"pid": os.getpid(), "port": srv.getsockname()[1]}))
    except Exception:
        pass

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                time.sleep(0.2)
                continue
            try:
                with conn:
                    conn.settimeout(2)
                    if not conn.recv(32).startswith(b"activate"):
                        continue
                    cb = _instance["cb"]
                    if cb is None:                # still starting: front once ready
                        _instance["pending"] = True
                        conn.sendall(b"ok-starting\n")
                        continue
                    done = threading.Event()
                    cb(done)
                    # answer only once the interface really reacted: a frozen
                    # copy stays silent and the new launch replaces it
                    if done.wait(3.0):
                        conn.sendall(b"ok\n")
            except Exception:
                pass

    threading.Thread(target=serve, daemon=True, name="single-instance").start()


def _bind_instance_activation(ui) -> None:
    """Called on the Qt thread once the window exists."""
    from PyQt6.QtCore import QObject, pyqtSignal

    class _Bridge(QObject):
        activate = pyqtSignal(object)

    def bring_to_front(done=None):
        try:
            w = ui._win
            if w.isMinimized():
                w.showNormal()
            w.show()
            w.raise_()
            w.activateWindow()
            if sys.platform == "win32":
                import ctypes
                hwnd = int(w.winId())
                ctypes.windll.user32.ShowWindow(hwnd, 5)          # SW_SHOW
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            ui.write_log("SYS: Já estou aberto — trouxe esta janela para a frente.")
        except Exception as e:
            print(f"[JARVIS] bring to front: {e}")
        finally:
            if done is not None:
                done.set()

    bridge = _Bridge()
    bridge.activate.connect(bring_to_front)      # queued onto the Qt thread
    _instance["bridge"] = bridge
    _instance["cb"] = bridge.activate.emit
    if _instance["pending"]:
        _instance["pending"] = False
        bridge.activate.emit(None)


def main():
    if not _claim_instance():
        if _ask_running_instance():
            print("[JARVIS] Already running — brought the open window to the front instead.")
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(0)
        # it does not answer: frozen or left without a window — replace it
        print("[JARVIS] The open copy is not answering — replacing it.")
        _take_over_previous_instance()
        time.sleep(1.0)
        _claim_instance()
    else:
        _take_over_previous_instance()   # copies from before this lock existed
    _start_instance_server()
    ui = JarvisUI("face.png")
    _bind_instance_activation(ui)

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()
    # The window is closed: end the process now. Left to the interpreter, exit
    # waits for every worker thread — one stuck network call (a news check, a
    # search) and the assistant lived on invisibly, still listening and still
    # connected, until the next launch ran into it.
    print("[JARVIS] Window closed — exiting.")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)

if __name__ == "__main__":
    main()