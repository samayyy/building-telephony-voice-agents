"""Phone agent: LiveKit Agents + Gemini Live (Vertex AI) answering SIP calls.

Generic, production-grade template. Every value comes from environment variables (see Config below and
`env.example` next to this file), so the file runs unchanged on LiveKit Cloud, a self-hosted LiveKit server, or
locally with `python agent_gemini_live.py dev`.

Verified against (production): livekit-agents[google,silero]==1.7.1, livekit-plugins-google==1.7.1,
livekit-plugins-silero==1.7.1, livekit-plugins-noise-cancellation 0.3.0, google-genai==2.21.0, Python 3.12,
model gemini-3.8-live on Vertex AI (location "us", fallback "us-central1"), LiveKit Cloud agent in ap-south.
Every API used here was also checked to exist with the same signature in livekit-agents 1.8.8 (2026-10-21).

1.8.x notes (search for "1.8.x" below):
  * AgentServer / rtc_session / RoomOptions are the same in 1.7.1 and 1.8.x. Nothing to port there.
  * livekit-plugins-google 1.8.2-1.8.4 raise ValueError for vertexai=True + "gemini-3.8-live". vertex_model_name()
    switches to "publishers/google/models/gemini-3.8-live" for those versions. 1.8.5+ accept the short name.
  * The 1.7.1 resume-history bug (livekit/agents#6000) is fixed in 1.8.1; the workaround below only activates on
    plugin versions older than 1.8.1.
  * 1.8.x adds livekit.agents.inference.RealtimeModel("google/gemini-3.8-live") (LiveKit-billed gateway). This
    template uses the Google plugin because it needs Vertex-only settings (compression, vocabulary, regions).

What this template does (each piece exists because a real call failed without it; see
references/03-conversation-mechanics.md and references/13-troubleshooting-and-lessons.md):
  * prewarm: Silero VAD load (fast) + a background thread that probes Gemini Live per region and renders the
    greeting in the Live voice. Nothing slow runs on the 10 s initialize_process_timeout path.
  * Gemini RealtimeModel on Vertex with an in-call region fallback (RealtimeModelFallbackAdapter).
  * Greeting played from cached audio while Gemini connects. Caller audio is HELD (not dropped) during the
    greeting so PSTN echo cannot trigger a phantom reply, and released with replay if the caller really talks
    over it (0.8 s of continuous local-VAD speech = barge-in).
  * end_call tool that returns None, a grace window that re-checks caller speech at commit time, speech
    suppression after the goodbye, and RoomOptions(delete_room_on_close=True) as the actual hang-up.
  * Silence monitor (check-in, final warning, close) and a maximum call duration.
  * Optional background ambience / thinking sound, optional cold transfer, optional outbound dialling.
  * Caller number from SIP participant attributes, resolved before the session starts.
  * Transcript capture hook during the call, post-call hook (on_session_end) after the caller is gone.

Run:
  pip install -r requirements.txt
  python agent_gemini_live.py download-files      # Silero + noise-cancellation model files
  python agent_gemini_live.py dev                 # local worker registered to LIVEKIT_URL
  python agent_gemini_live.py start               # production
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import typing
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import aiohttp
from google import genai
from google.genai import types
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
    llm,
    room_io,
)
from livekit.plugins import google, silero
from livekit.plugins.google.realtime.api_proto import Voice as _LiveVoice
from livekit.plugins.google.realtime.realtime_api import RealtimeSession as _GoogleRealtimeSession

try:  # LiveKit Cloud only (authenticates with the room token); absent or useless when self-hosted
    from livekit.plugins import noise_cancellation
except ImportError:  # pragma: no cover
    noise_cancellation = None

logger = logging.getLogger("phone-agent")

# --------------------------------------------------------------------------------------------- constants
LIVE_LOCATIONS = {"us", "eu", "us-central1"}  # where gemini-3.8-live is served on Vertex (2026-10-21)
COMPRESSION_TARGET_TOKENS = 64_000            # well under the 128k window; audio sessions cap at ~15 min without it
GREETING_RENDER_BUDGET_S = 40.0               # background thread budget per region
PARTICIPANT_WAIT_S = 8.0                      # bound every wait: wait_for_* never returns on its own if nobody joins
GREETING_PLAYOUT_TIMEOUT_S = 20.0
GREETING_ECHO_TAIL_S = 0.4                    # line keeps echoing after our playout ends
GREETING_BARGE_IN_S = 0.8                     # echo/chatter bursts measured <= 0.70 s; real speech detected in ~0.15 s
BARGE_IN_REPLAY_LEAD_S = 0.6                  # replay this far before the VAD "speaking" edge so no word is clipped
CALLER_SPEECH_LOOKBACK_S = 1.0                # caller speech this close before arming the close cancels it
VAD_MIN_SILENCE_S = 0.25
SILENCE_PLACEHOLDERS = re.compile(r"<no speech>|\{pause\}", re.IGNORECASE)  # Live output-transcript artefacts
LIVE_WARMUP_KEY = "live_warmup"

# Never send these on gemini-3.8-live (each silently breaks the call; see references/02):
#   thinking_config (budget 0 / MINIMAL -> no audio at all), enable_affective_dialog=True (1007),
#   proactivity, small max_output_tokens (turn never completes), modalities TEXT (1007 on Vertex),
#   speaking_rate / pitch (rejected at setup), HistoryConfig(initial_history_in_client_content=True).


# --------------------------------------------------------------------------------------------- config
def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float, errors: list[str]) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not a number")
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in _env(name).split(",") if p.strip()]


DEFAULT_INSTRUCTIONS = """You are the voice assistant on <COMPANY>'s customer phone line. You are on a live phone call.

# Language
RESPOND IN THE LANGUAGE THE CALLER IS USING. If they switch language, switch with them. Never mix in a language
the caller has not used.

# How you speak
- Short, plain sentences. Calm, level tone. No exclamations.
- ASK ONLY ONE QUESTION PER REPLY. Callers answer only the last question you ask.
- Everything you say is heard by the caller. Never say notes, reasoning or descriptions of the caller aloud.
- Read numbers back one digit at a time, and ask the caller to repeat if unsure.

# Honesty
- Never say something has been done (registered, booked, sent, transferred) unless a tool returned success.
- If you cannot help, say so and offer a callback.

# Ending the call
- When the caller has nothing else, say a short goodbye that is not a question, then call end_call.
- Never call end_call while your question or the caller's question is still open.
"""


@dataclass(frozen=True)
class Config:
    agent_name: str
    project: str
    model: str
    location: str
    fallback_location: str
    voice: str
    language: str
    transcription_languages: list[str]
    custom_vocabulary: list[str]
    greeting: str
    instructions: str
    noise_cancellation: str
    record: bool
    close_grace_s: float
    silence_check_in_s: float
    silence_final_s: float
    silence_close_s: float
    max_call_s: float
    ambience: str
    ambience_volume: float
    thinking_sound: bool
    transfer_to: str
    outbound_trunk_id: str
    post_call_webhook: str
    post_call_dir: str
    recording_bucket: str
    timezone: str
    parse_errors: tuple[str, ...] = field(default=())

    @classmethod
    def from_env(cls) -> Config:
        errors: list[str] = []
        prompt_file = _env("SYSTEM_PROMPT_FILE")
        if prompt_file:
            try:
                instructions = Path(prompt_file).read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"SYSTEM_PROMPT_FILE unreadable: {exc}")
                instructions = DEFAULT_INSTRUCTIONS
        else:
            instructions = _env("SYSTEM_PROMPT") or DEFAULT_INSTRUCTIONS
        return cls(
            agent_name=_env("AGENT_NAME", "my-agent"),
            project=_env("GOOGLE_CLOUD_PROJECT"),
            model=_env("GEMINI_LIVE_MODEL", "gemini-3.8-live"),
            location=_env("GEMINI_LIVE_LOCATION", "us"),
            fallback_location=_env("GEMINI_LIVE_FALLBACK_LOCATION", "us-central1"),
            voice=_env("GEMINI_LIVE_VOICE", "Gacrux"),
            language=_env("GEMINI_LIVE_LANGUAGE"),  # OFF by default: en-IN measurably pushed Hindi callers to English
            transcription_languages=_env_list("TRANSCRIPTION_LANGUAGES"),  # e.g. "en-IN,hi-IN"
            custom_vocabulary=_env_list("CUSTOM_VOCABULARY"),  # PHRASES a recogniser would mangle, never bare words
            greeting=_env("GREETING_TEXT", "Hello, thank you for calling. How can I help you today?"),
            instructions=instructions,
            noise_cancellation=_env("NOISE_CANCELLATION", "bvc_telephony").lower(),  # bvc_telephony | none
            record=_env_bool("RECORD_SESSION", False),  # fail closed: needs a governance decision
            close_grace_s=_env_float("CLOSE_GRACE_SECONDS", 4.0, errors),  # 2.5 s cut off a caller still thinking
            silence_check_in_s=_env_float("SILENCE_CHECK_IN_SECONDS", 20.0, errors),
            silence_final_s=_env_float("SILENCE_FINAL_WARNING_SECONDS", 40.0, errors),
            silence_close_s=_env_float("SILENCE_CLOSE_SECONDS", 60.0, errors),
            max_call_s=_env_float("MAX_CALL_SECONDS", 900.0, errors),
            ambience=_env("BACKGROUND_AMBIENCE").upper(),  # "" | OFFICE_AMBIENCE | CITY_AMBIENCE | ...
            ambience_volume=_env_float("BACKGROUND_AMBIENCE_VOLUME", 0.15, errors),  # keep 0.1-0.2 on PSTN
            thinking_sound=_env_bool("THINKING_SOUND", False),
            transfer_to=_env("TRANSFER_TO"),  # "+91XXXXXXXXXX" or "sip:+91XXXXXXXXXX@host"; empty = no transfer tool
            outbound_trunk_id=_env("SIP_OUTBOUND_TRUNK_ID"),  # ST_xxx, needed only for outbound calls
            post_call_webhook=_env("POST_CALL_WEBHOOK_URL"),
            post_call_dir=_env("POST_CALL_DIR"),
            recording_bucket=_env("RECORDING_GCS_BUCKET"),
            timezone=_env("BUSINESS_TIMEZONE", "UTC"),
            parse_errors=tuple(errors),
        )

    def errors(self) -> list[str]:
        """Problems that would make calls fail or misbehave. Checked at prewarm and per call."""
        errs = list(self.parse_errors)
        if not self.project:
            errs.append("GOOGLE_CLOUD_PROJECT is required for Vertex AI")
        for name, loc in (("GEMINI_LIVE_LOCATION", self.location), ("GEMINI_LIVE_FALLBACK_LOCATION", self.fallback_location)):
            if loc and loc not in LIVE_LOCATIONS:  # Vertex accepts any string until connect time (1008)
                errs.append(f"{name}={loc!r}; gemini-3.8-live is served only in {sorted(LIVE_LOCATIONS)}")
        voices = {v.lower() for v in typing.get_args(_LiveVoice)}
        if voices and self.voice.lower() not in voices:  # unknown voice -> 1007 at setup
            errs.append(f"GEMINI_LIVE_VOICE={self.voice!r} is not a prebuilt Live voice")
        if self.language and not re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}", self.language):  # Live accepts any string
            errs.append(f"GEMINI_LIVE_LANGUAGE={self.language!r} is not BCP-47 like en-IN")
        if not (0 < self.silence_check_in_s < self.silence_final_s < self.silence_close_s < self.max_call_s):
            errs.append("need 0 < SILENCE_CHECK_IN < SILENCE_FINAL_WARNING < SILENCE_CLOSE < MAX_CALL")
        if self.ambience and self.ambience not in BuiltinAudioClip.__members__:
            errs.append(f"BACKGROUND_AMBIENCE={self.ambience!r} not in {list(BuiltinAudioClip.__members__)}")
        return errs


# --------------------------------------------------------------------------------------------- credentials
def resolve_google_credentials() -> str | None:
    """Find a mounted service-account key and export GOOGLE_APPLICATION_CREDENTIALS.

    LiveKit Cloud `--secret-mount` decides the mount path; do not hard-code it. A wrong path fails at the first
    Google client construction, i.e. silence on a connected line. Falls back to ADC when nothing is found.
    """
    current = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if current and Path(current).is_file():
        return current
    for root in ("/etc/secrets", "/run/secrets", "/secrets", "/var/secrets", "/app"):
        base = Path(root)
        if not base.is_dir():
            continue
        for candidate in [*base.glob("*.json"), *base.glob("*/*.json")]:
            try:
                if json.loads(candidate.read_text(encoding="utf-8")).get("type") == "service_account":
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(candidate)
                    logger.info("google credentials: %s", candidate)
                    return str(candidate)
            except (OSError, ValueError):
                continue
    logger.info("google credentials: no mounted key found, using ADC")
    return None


# --------------------------------------------------------------------------------------------- plugin version
def _plugin_version() -> tuple[int, ...]:
    try:
        raw = metadata.version("livekit-plugins-google")
    except metadata.PackageNotFoundError:
        return (0,)
    return tuple(int(x) for x in re.findall(r"\d+", raw)[:3])


def vertex_model_name(model: str) -> str:
    """1.8.x: plugin 1.8.2-1.8.4 list gemini-3.8-live as Gemini-API-only and raise for vertexai=True.

    The path form is not in the plugin's lists, google-genai keeps names starting with "publishers/" and adds
    projects/<p>/locations/<l>/, so the request on the wire is the same. 1.7.1 and 1.8.5+ take the short name.
    """
    if "/" not in model and (1, 8, 2) <= _plugin_version() < (1, 8, 5):
        return f"publishers/google/models/{model}"
    return model


# --------------------------------------------------------------------------------------------- caller audio hold
class CallerAudioHold:
    """Hold caller frames while the greeting plays; replay them only if the caller really spoke.

    LiveKit applies no AEC to SIP participants. PSTN echo of the greeting reached Gemini's server VAD and produced
    a phantom reply over the greeting in 24/24 replays. Dropping caller audio fixed that (0/57) but made the
    caller unheard. Holding keeps both properties.
    """

    def __init__(self, *, keep_seconds: float = 15.0) -> None:
        self._keep_seconds = keep_seconds
        self._held: deque[tuple[float, rtc.AudioFrame]] | None = None
        self._replay: list[rtc.AudioFrame] = []
        self._lock = threading.Lock()

    @property
    def holding(self) -> bool:
        return self._held is not None

    def hold(self) -> None:
        with self._lock:
            self._held, self._replay = deque(), []

    def release(self, *, replay_since: float | None = None) -> None:
        with self._lock:
            held, self._held = self._held, None
            if held and replay_since is not None:
                self._replay = [frame for at, frame in held if at >= replay_since]
            # else: it was only echo; discard

    def admit(self, frame: rtc.AudioFrame) -> list[rtc.AudioFrame]:
        with self._lock:
            if self._held is not None:
                now = time.monotonic()
                self._held.append((now, frame))
                while self._held and now - self._held[0][0] > self._keep_seconds:
                    self._held.popleft()
                return []
            if self._replay:
                frames, self._replay = [*self._replay, frame], []
                return frames
            return [frame]


# --------------------------------------------------------------------------------------------- realtime model
class _ReplaySkippingConnect:
    """1.7.1 only (livekit/agents#6000, fixed in 1.8.1): on a resumed connection the plugin replays the whole
    local history as the first client content (turn_complete=False), so the model sees every turn twice after
    the ~9 min GoAway reconnect. Drop that first send on resumed connections."""

    def __init__(self, inner: Any, resuming: bool) -> None:
        self._inner, self._resuming = inner, resuming

    async def __aenter__(self) -> Any:
        session = await self._inner.__aenter__()
        if self._resuming:
            original = session.send_client_content

            async def first_send(*args: Any, **kwargs: Any) -> Any:
                session.send_client_content = original
                if kwargs.get("turn_complete") is False:
                    return None
                return await original(*args, **kwargs)

            session.send_client_content = first_send
        return session

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)


def _skip_replay_on_resume(live: Any) -> None:
    connect = live.connect

    def resume_aware_connect(*, model: str, config: Any) -> _ReplaySkippingConnect:
        resumption = getattr(config, "session_resumption", None)
        return _ReplaySkippingConnect(connect(model=model, config=config), bool(resumption and resumption.handle))

    live.connect = resume_aware_connect


class _HoldingRealtimeSession(_GoogleRealtimeSession):
    # Relies on plugin internals (RealtimeSession.push_audio, ._client, model._sessions). Checked identical in
    # livekit-plugins-google 1.7.1 and 1.8.8. Re-check on every upgrade.
    def __init__(self, realtime_model: HoldingRealtimeModel) -> None:
        super().__init__(realtime_model)
        if _plugin_version() < (1, 8, 1):
            _skip_replay_on_resume(self._client.aio.live)

    def push_audio(self, frame: rtc.AudioFrame) -> None:
        hold: CallerAudioHold = self._realtime_model.caller_audio  # type: ignore[attr-defined]
        for admitted in hold.admit(frame):
            super().push_audio(admitted)


class HoldingRealtimeModel(google.realtime.RealtimeModel):
    def __init__(self, *, caller_audio: CallerAudioHold, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.caller_audio = caller_audio

    def session(self, *, turn_detection_disabled: bool = False) -> _GoogleRealtimeSession:
        sess = _HoldingRealtimeSession(self)
        self._sessions.add(sess)
        return sess


def build_live_model(cfg: Config, location: str, hold: CallerAudioHold) -> HoldingRealtimeModel:
    kwargs: dict[str, Any] = {
        "model": vertex_model_name(cfg.model),
        "vertexai": True,
        "project": cfg.project,  # pass explicitly; ADC may not carry a project
        "location": location,  # "us"/"eu" -> aiplatform.{us|eu}.rep.googleapis.com
        "voice": cfg.voice,
        "input_audio_transcription": types.AudioTranscriptionConfig(
            language_codes=cfg.transcription_languages or None,
            custom_vocabulary=cfg.custom_vocabulary or None,
        ),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        "context_window_compression": types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(target_tokens=COMPRESSION_TARGET_TOKENS),
        ),
        # The plugin requests session resumption itself and reconnects on GoAway (~9-10 min), ~2.2 s gap.
    }
    if cfg.language:
        kwargs["language"] = cfg.language  # speech_config.language_code; does NOT force the output language
    return HoldingRealtimeModel(caller_audio=hold, **kwargs)


def build_realtime_llm(cfg: Config, location: str, hold: CallerAudioHold) -> llm.RealtimeModel:
    """Primary region plus fallback region. The plugin does not retry a failed first connect: without the
    adapter one failed connect closed the session and the caller heard half a greeting and a dead line."""
    primary = build_live_model(cfg, location, hold)
    other = cfg.fallback_location if location == cfg.location else cfg.location
    if not other or other == location:
        return primary
    return llm.RealtimeModelFallbackAdapter([primary, build_live_model(cfg, other, hold)])


# --------------------------------------------------------------------------------------------- greeting render
def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def greeting_render_matches(transcript: str, text: str) -> bool:
    """Live sometimes paraphrases or adds a preamble ("Sure, here it is"), which would then play on EVERY call."""
    want, got = _words(text), _words(transcript)
    if not want or not got:
        return False
    heard = sum(1 for w in set(want) if w in set(got)) / len(set(want))
    extra = len([w for w in got if w not in set(want)])
    return heard >= 0.7 and extra <= 3


async def render_greeting(cfg: Config, location: str) -> dict[str, Any] | None:
    """One raw Live session in the call's voice. Doubles as the region reachability probe (raises on failure).

    Returns cached 24 kHz PCM16 mono chunks, or None when the render did not say the greeting verbatim.
    """
    client = genai.Client(vertexai=True, project=cfg.project, location=location)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=(
            "You are the voice of a customer phone line. When you are given a line, say exactly that line in a calm, "
            "steady, professional tone at a relaxed pace, and nothing else. "
            "<Add the same accent instruction your main prompt uses, e.g. 'Speak English with a clear Indian accent.'>"
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.voice)),
            language_code=cfg.language or None,  # same hint as the call it opens
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    chunks: list[bytes] = []
    transcript = ""
    async with client.aio.live.connect(model=cfg.model, config=config) as live:
        if not cfg.greeting:
            return None  # probe only
        await live.send_realtime_input(text=f"Say exactly this line: {cfg.greeting}")
        async for message in live.receive():
            content = message.server_content
            if content is None:
                continue
            for part in (content.model_turn.parts if content.model_turn else None) or []:
                if part.inline_data and part.inline_data.data:
                    chunks.append(part.inline_data.data)
            if content.output_transcription and content.output_transcription.text:
                transcript += content.output_transcription.text
            if content.turn_complete:
                break
    if not chunks or not greeting_render_matches(transcript, cfg.greeting):
        logger.warning("greeting render rejected (transcript=%r); calls will generate the greeting live", transcript)
        return None
    return {"chunks": chunks, "sample_rate": 24000, "num_channels": 1}


def greeting_frames(cached: dict[str, Any]) -> AsyncIterator[rtc.AudioFrame]:
    """Fresh AudioFrames per call from the cached bytes (never share frame objects across calls)."""
    sample_rate, channels = int(cached["sample_rate"]), int(cached["num_channels"])

    async def _iter() -> AsyncIterator[rtc.AudioFrame]:
        for chunk in cached["chunks"]:
            samples = len(chunk) // (channels * 2)
            if samples:
                yield rtc.AudioFrame(chunk[: samples * channels * 2], sample_rate, channels, samples)

    return _iter()


def _warm_live_in_background(proc: JobProcess, cfg: Config) -> None:
    """Probe primary then fallback region; publish the result only when complete (never a half render)."""
    for location in (cfg.location, cfg.fallback_location):
        if not location:
            continue
        started = time.monotonic()
        try:
            cached = asyncio.run(asyncio.wait_for(render_greeting(cfg, location), GREETING_RENDER_BUDGET_S))
        except Exception as exc:  # noqa: BLE001 - any failure means "try the next region"
            logger.error("live warmup failed location=%s: %r", location, exc)
            continue
        proc.userdata[LIVE_WARMUP_KEY] = {"ok": True, "location": location, "greeting": cached}
        logger.info("live warmup: ok=True location=%s greeting_cached=%s in %.2fs",
                    location, cached is not None, time.monotonic() - started)
        return
    proc.userdata[LIVE_WARMUP_KEY] = {"ok": False, "location": None, "greeting": None}
    logger.error("live warmup: ok=False in every region. Production design answers such processes on a cascaded "
                 "STT-LLM-TTS pipeline (references/01 section 6); this template still tries Live per call.")


def prewarm(proc: JobProcess) -> None:
    """Runs once per worker process under a HARD 10 s initialize_process_timeout (the pool kills and respawns past
    it, forever). Only fast local work here; network work goes on a daemon thread nothing waits for."""
    resolve_google_credentials()
    cfg = Config.from_env()
    proc.userdata["cfg"] = cfg
    for err in cfg.errors():
        logger.error("config: %s", err)
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=VAD_MIN_SILENCE_S,
        prefix_padding_duration=0.5,
        activation_threshold=0.5,
        sample_rate=16000,
        force_cpu=True,
    )
    if not cfg.errors():
        threading.Thread(target=_warm_live_in_background, args=(proc, cfg), name="live-warmup", daemon=True).start()


# --------------------------------------------------------------------------------------------- call state
@dataclass
class CallState:
    started_at: float = field(default_factory=time.monotonic)
    last_caller_speech_at: float | None = None
    silence_anchor: float = field(default_factory=time.monotonic)
    silence_stage: int = 0
    system_prompt_in_flight: bool = False
    close_after_response: bool = False  # end_call ran; close when the agent next hands the floor back
    close_pending: bool = False  # grace window running
    close_generation: int = 0
    system_closing: bool = False  # silence / max-duration goodbye in progress (do not suppress it)
    closing: bool = False
    caller_turns: int = 0
    end_reason: str = ""
    direction: str = "inbound"
    live_location: str = ""
    greeting_prerendered: bool = False
    transcript: list[dict[str, Any]] = field(default_factory=list)  # live capture; the report is the fallback


def caller_number_from(participant: rtc.RemoteParticipant | None) -> str | None:
    """Caller's own number. sip.trunkPhoneNumber is the number DIALLED (your DID): never use it."""
    attributes = (participant.attributes if participant else None) or {}
    logger.info("sip attributes present: %s", {k: ("***" + v[-4:] if v else "") for k, v in attributes.items()
                                                 if k.startswith("sip.")})
    for key in ("sip.phoneNumber", "sip.from", "sip.fromUser"):
        value = str(attributes.get(key) or "").strip()
        if value and any(ch.isdigit() for ch in value):
            return value
    return None


def build_instructions(cfg: Config, *, caller_number: str | None, outbound: bool, greeting_text: str) -> str:
    """Per-call values go LAST so every call shares the longest identical prefix. Built once, before
    session.start: on Vertex, update_instructions() mid-call appends the whole prompt as a model turn."""
    today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%A %d %B %Y")
    parts = [cfg.instructions.rstrip(), "", "# This call", f"- Today is {today}."]
    if outbound:
        parts.append("- You placed this call. Wait for the person to speak first, then introduce yourself briefly.")
    elif greeting_text:
        parts.append(f"- The phone system has already greeted the caller with: \"{greeting_text}\". "
                     "Do not greet again; answer what the caller says next.")
    if caller_number:  # only when a number exists: telling it to confirm a number it lacks broke callbacks
        parts.append("- The caller's phone number is known to the system. If you need a callback number, ask once "
                     "whether this number is best, and do not read the digits aloud.")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------------------------- agent + tools
class PhoneAgent(Agent):
    def __init__(self, *, instructions: str, controller: CallController, tools: list[Any]) -> None:
        super().__init__(instructions=instructions, tools=tools)
        self._controller = controller

    @function_tool(
        name="end_call",
        description=(
            "End the phone call. Call this only after you have said goodbye: either the caller told you there is "
            "nothing else, or they said they are finished. Never call it while a question of yours or of the "
            "caller's is still open. Say nothing after calling it."
        ),
    )
    async def end_call(self, context: RunContext) -> None:
        await context.wait_for_playout()  # grace is measured from the END of the farewell
        self._controller.request_close(reason="agent_end_call")
        # Return NOTHING. A string result makes LiveKit hold agent_state at "thinking" waiting for a tool reply;
        # Gemini usually stays silent after end_call, so the "listening" edge never came and the line never hung up.


def make_transfer_tool(controller: CallController) -> Any:
    @function_tool(
        name="transfer_to_human",
        description=(
            "Transfer the caller to a human colleague. Call this only after the caller agreed, and after you told "
            "them you are transferring them now."
        ),
    )
    async def transfer_to_human(context: RunContext, reason: str) -> str | None:
        await context.wait_for_playout()
        return await controller.transfer(reason)

    return transfer_to_human


# --------------------------------------------------------------------------------------------- controller
class GreetingBargeIn:
    """Barge-in during the greeting from the LOCAL Silero VAD (Gemini's server VAD hears nothing while held)."""

    def __init__(self, hold: CallerAudioHold) -> None:
        self._hold = hold
        self._handle: Any = None
        self._speech_started_at: float | None = None
        self._timer: asyncio.Task[None] | None = None
        self._closed = False

    def watch(self, handle: Any) -> None:
        self._handle = handle

    def on_user_state(self, old: str, new: str) -> None:
        if self._closed:
            return
        if new == "speaking":
            self._speech_started_at = time.monotonic()
            self._timer = asyncio.create_task(self._fire_after_delay())
        elif old == "speaking":
            if self._timer and not self._timer.done():
                self._timer.cancel()
            self._speech_started_at = None

    async def _fire_after_delay(self) -> None:
        await asyncio.sleep(GREETING_BARGE_IN_S)
        if self._closed or self._speech_started_at is None:
            return
        logger.info("greeting barge-in: caller spoke over the greeting")
        self._hold.release(replay_since=self._speech_started_at - BARGE_IN_REPLAY_LEAD_S)
        if self._handle is not None and not self._handle.interrupted:
            self._handle.interrupt()

    def replay_since(self) -> float | None:
        """At greeting end: replay only if a speech run is still in progress (caller started as it ended)."""
        return None if self._speech_started_at is None else self._speech_started_at - BARGE_IN_REPLAY_LEAD_S

    def close(self) -> None:
        self._closed = True
        if self._timer and not self._timer.done():
            self._timer.cancel()


class CallController:
    def __init__(self, ctx: JobContext, cfg: Config, hold: CallerAudioHold) -> None:
        self.ctx, self.cfg, self.hold = ctx, cfg, hold
        self.call = CallState()
        self.session: AgentSession | None = None
        self.agent: PhoneAgent | None = None
        self.sip_participant: rtc.RemoteParticipant | None = None
        self.barge_in: GreetingBargeIn | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    # ---- events -------------------------------------------------------------------------------------------
    def wire(self, session: AgentSession) -> None:
        self.session = session
        session.on("user_state_changed", self._on_user_state_changed)
        session.on("agent_state_changed", self._on_agent_state_changed)
        session.on("speech_created", self._on_speech_created)
        session.on("conversation_item_added", self._on_item_added)
        session.on("user_input_transcribed", self._on_user_transcribed)
        session.on("error", self._on_error)
        session.on("close", self._on_close)

    def _on_user_state_changed(self, ev: Any) -> None:
        now = time.monotonic()
        if ev.new_state == "speaking" or ev.old_state == "speaking":  # stamp BOTH edges (local VAD, not transcripts)
            self.call.last_caller_speech_at = now
            self.call.silence_anchor = now
            self.call.silence_stage = 0
        if ev.new_state == "speaking" and self.call.close_pending and not self.call.system_closing:
            self.cancel_close("caller spoke during grace")
        if self.barge_in is not None:
            self.barge_in.on_user_state(ev.old_state, ev.new_state)

    def _on_agent_state_changed(self, ev: Any) -> None:
        if ev.new_state != "listening":
            return
        if self.call.system_prompt_in_flight:  # a check-in must not reset the silence ladder
            self.call.system_prompt_in_flight = False
        else:
            self.call.silence_anchor = time.monotonic()
        if self.call.close_after_response:
            self._arm_close()

    def _on_speech_created(self, ev: Any) -> None:
        # Vertex answers even an empty end_call result ("I have ended the call", or a "no speech" placeholder);
        # tool_response_scheduling=SILENT is ignored there. Kill any speech while a close is armed or pending.
        if (self.call.close_after_response or self.call.close_pending) and not self.call.system_closing:
            ev.speech_handle.interrupt(force=True)

    def _on_item_added(self, ev: Any) -> None:
        item = ev.item
        if getattr(item, "type", None) != "message" or item.role not in ("user", "assistant"):
            return
        text = SILENCE_PLACEHOLDERS.sub("", item.text_content or "").strip()
        if text:
            turn = {"role": item.role, "text": text, "at": time.time()}
            self.call.transcript.append(turn)
            on_transcript_turn(self.ctx, turn)

    def _on_user_transcribed(self, ev: Any) -> None:
        # on_user_turn_completed never fires on a realtime model; count caller turns here instead.
        if ev.is_final:
            self.call.caller_turns += 1

    def _on_error(self, ev: Any) -> None:
        # Emit provider errors in their own right: per-turn latency only covers turns that produced a reply.
        logger.warning("provider_error source=%s recoverable=%s error=%r",
                       type(ev.source).__name__, getattr(ev.error, "recoverable", None), ev.error)

    def _on_close(self, ev: Any) -> None:
        self.call.closing = True
        logger.info("session closed reason=%s end_reason=%s caller_turns=%d",
                    getattr(ev, "reason", None), self.call.end_reason or "caller_hangup", self.call.caller_turns)
        for task in self._tasks:
            task.cancel()

    # ---- closing ------------------------------------------------------------------------------------------
    def request_close(self, *, reason: str) -> None:
        self.call.close_after_response = True
        self.call.end_reason = reason
        if self.session is not None and self.session.agent_state == "listening":
            self._arm_close()  # the listening edge may already have passed

    def _arm_close(self) -> None:
        self.call.close_after_response = False
        self.call.close_pending = True
        self.call.close_generation += 1
        if self._close_task and not self._close_task.done():
            self._close_task.cancel()
        self._close_task = asyncio.create_task(self._close_after_grace(time.monotonic(), self.call.close_generation))

    async def _close_after_grace(self, armed_at: float, generation: int) -> None:
        await asyncio.sleep(self.cfg.close_grace_s)
        if generation != self.call.close_generation or not self.call.close_pending:
            return  # a stale task never commits or cancels a later farewell
        # Decide at COMMIT time: a caller already mid-sentence produces no new "speaking" edge.
        last = self.call.last_caller_speech_at
        if (self.session and self.session.user_state == "speaking") or (
            last is not None and last >= armed_at - CALLER_SPEECH_LOOKBACK_S
        ):
            self.cancel_close("caller speech near the goodbye")
            return
        self.call.closing = True
        logger.info("closing call reason=%s", self.call.end_reason)
        assert self.session is not None
        self.session.shutdown(drain=True)  # RoomOptions(delete_room_on_close=True) then drops the SIP leg

    def cancel_close(self, why: str) -> None:
        if self.call.close_pending or self.call.close_after_response:
            logger.info("close cancelled: %s", why)
        self.call.close_pending = False
        self.call.close_after_response = False
        self.call.close_generation += 1
        if self._close_task and not self._close_task.done():
            self._close_task.cancel()

    async def system_goodbye(self, instructions: str, reason: str) -> None:
        assert self.session is not None
        self.call.system_closing = True
        self.call.end_reason = reason
        handle = self.session.generate_reply(instructions=instructions, allow_interruptions=True)
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=GREETING_PLAYOUT_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("system goodbye playout timed out")
        self.call.closing = True
        self.session.shutdown(drain=True)

    # ---- transfer -----------------------------------------------------------------------------------------
    async def transfer(self, reason: str) -> str | None:
        sip = self.sip_participant
        target = self.cfg.transfer_to
        if target.startswith("+"):
            target = f"tel:{target}"
        if sip is None or sip.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP or not target:
            return "Transfer is not available on this call. Tell the caller a colleague will call them back."
        try:
            await self.ctx.transfer_sip_participant(sip, target)  # SIP REFER; carrier must allow it
        except Exception as exc:  # noqa: BLE001 - api.SipCallError and transport errors alike
            logger.warning("transfer failed reason=%s: %r", reason, exc)
            return ("The transfer failed and the caller is still on the line. Apologise briefly and tell them a "
                    "colleague will call them back as soon as possible.")
        self.call.end_reason = "transferred"
        return None

    # ---- silence + max duration ---------------------------------------------------------------------------
    def start_monitor(self) -> None:
        self._tasks.append(asyncio.create_task(self._silence_monitor()))

    def _system_prompt(self, instructions: str) -> None:
        assert self.session is not None
        self.call.system_prompt_in_flight = True
        self.session.generate_reply(instructions=instructions, allow_interruptions=True)

    async def _silence_monitor(self) -> None:
        cfg, call = self.cfg, self.call
        # Instructions, not fixed lines, so the model speaks them in the call's language. Each says it is NOT the
        # close, or the system prompt's closing rules leak in (a check-in once hung up two stages early).
        check_in = ("The caller has gone quiet. In the language you have been speaking with them, check gently that "
                    "they are still there and tell them to take their time. One short sentence. This is not the end "
                    "of the call: do not say goodbye, do not ask whether there is anything else, do not call end_call.")
        final = ("The caller is still silent. In the language of the call, say you cannot hear them and that the call "
                 "will end shortly if they do not respond. One short sentence. Do not call end_call.")
        goodbye = ("The caller has not responded. In the language of the call, say a short polite goodbye. The phone "
                   "system ends the call straight after this, so ask no question and do not call end_call.")
        max_duration = ("The call has reached its time limit. In the language of the call, apologise briefly, say how "
                        "the caller can get further help, and say goodbye. Ask no question and do not call end_call.")
        while not call.closing:
            await asyncio.sleep(0.5)
            session = self.session
            if session is None or call.closing or call.system_closing:
                continue
            now = time.monotonic()
            if now - call.started_at >= cfg.max_call_s:
                await self.system_goodbye(max_duration, "max_duration")
                return
            if call.close_pending or call.close_after_response:
                continue
            # "away" is LiveKit's state for a caller silent for user_away_timeout (15 s). Gating on "listening" only
            # meant no silent caller was EVER checked on, because "away" arrived before the 20 s check-in.
            if not (session.agent_state == "listening" and session.user_state in ("listening", "away")):
                continue
            silent_for = now - call.silence_anchor
            if call.silence_stage < 1 and silent_for >= cfg.silence_check_in_s:
                call.silence_stage = 1
                self._system_prompt(check_in)
            elif call.silence_stage < 2 and silent_for >= cfg.silence_final_s:
                call.silence_stage = 2
                self._system_prompt(final)
            elif call.silence_stage < 3 and silent_for >= cfg.silence_close_s:
                call.silence_stage = 3
                await self.system_goodbye(goodbye, "silence")
                return

    # ---- greeting -----------------------------------------------------------------------------------------
    async def greet(self, text: str, cached: dict[str, Any] | None) -> bool:
        """Returns True when cached audio was used. Caller audio is held for the whole greeting + echo tail."""
        assert self.session is not None and self.agent is not None
        self.hold.hold()
        self.barge_in = GreetingBargeIn(self.hold)
        started = time.monotonic()
        try:
            if cached is not None:
                handle = self.session.say(text, audio=greeting_frames(cached), add_to_chat_ctx=True,
                                          allow_interruptions=True)
            else:
                handle = self.session.generate_reply(
                    instructions=f"Greet the caller with exactly these words and nothing else: {text}")
            self.barge_in.watch(handle)
            try:
                # Bounded: wait_for_playout() never returns when there is no participant to play to.
                await asyncio.wait_for(handle.wait_for_playout(), timeout=GREETING_PLAYOUT_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("greeting playout timed out")
            if cached is not None and not handle.interrupted:
                # say(audio=...) only updates the LOCAL chat context; push it so Gemini knows it greeted.
                # Never after an interruption: the caller's reply is already in flight and a push cancels it.
                await self.agent.update_chat_ctx(self.agent.chat_ctx.copy())
        finally:
            if self.hold.holding:
                await asyncio.sleep(GREETING_ECHO_TAIL_S)  # let the line's echo die, keep listening
                replay_since = self.barge_in.replay_since()
                self.barge_in.close()
                self.hold.release(replay_since=replay_since)  # None -> it was echo, discard
            else:
                self.barge_in.close()
            self.barge_in = None
        logger.info("greeting_played prerendered=%s duration_ms=%d", cached is not None,
                    int((time.monotonic() - started) * 1000))
        return cached is not None


# --------------------------------------------------------------------------------------------- hooks
def on_transcript_turn(ctx: JobContext, turn: dict[str, Any]) -> None:
    """Called for every committed caller/agent turn during the call. Keep it cheap and local: no network I/O
    on the call path (append to a local outbox instead). Note: Live output transcripts are lossy and ~1.7 s
    late, and the caller transcript arrives once per turn at end of turn."""
    logger.debug("turn %s: %s", turn["role"], turn["text"][:120])


def _transcript_from_report(report: Any) -> list[dict[str, Any]]:
    turns = []
    for item in getattr(report.chat_history, "items", []):
        if getattr(item, "type", None) == "message" and item.role in ("user", "assistant"):
            text = SILENCE_PLACEHOLDERS.sub("", item.text_content or "").strip()
            if text:
                turns.append({"role": item.role, "text": text, "created_at": getattr(item, "created_at", None)})
    return turns


def _upload_recording(cfg: Config, path: Path, job_id: str) -> str | None:
    """Optional: copy the recorder's audio.ogg (stereo: caller ch0, agent ch1) to GCS before the container dies."""
    if not cfg.recording_bucket:
        return None
    try:
        from google.cloud import storage  # optional dependency: google-cloud-storage
    except ImportError:
        logger.warning("RECORDING_GCS_BUCKET set but google-cloud-storage is not installed")
        return None
    day = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y/%m/%d")  # business-day partition, not UTC
    blob = storage.Client().bucket(cfg.recording_bucket).blob(f"{day}/{job_id}.ogg")
    blob.upload_from_filename(str(path), content_type="audio/ogg")  # explicit type or browsers refuse to play it
    return f"gs://{cfg.recording_bucket}/{blob.name}"


async def _post_with_retries(url: str, payload: dict[str, Any]) -> None:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for attempt in range(3):
            try:
                async with http.post(url, json=payload) as resp:
                    if resp.status < 400:
                        return
                    if resp.status not in (408, 429) and resp.status < 500:
                        logger.error("post-call webhook rejected %s: %s", resp.status, await resp.text())
                        return  # never retry a validation error
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("post-call webhook attempt %d failed: %r", attempt + 1, exc)
            await asyncio.sleep(1.5 * (2**attempt))
    logger.error("post-call webhook gave up after 3 attempts")


async def on_session_end(ctx: JobContext) -> None:
    """Runs after the caller is disconnected (delete_room_on_close). Budget: session_end_timeout (300 s default),
    after which the process is killed. Put explicit timeouts on every network call. Do CRM writes, extraction,
    QA and uploads here, never on the call path."""
    resolve_google_credentials()
    cfg: Config = ctx.proc.userdata.get("cfg") or Config.from_env()
    job_id = ctx.job.id
    caller_number = ctx.proc.userdata.pop(f"caller_number:{job_id}", None)  # participant is gone by now
    call: CallState | None = ctx.proc.userdata.pop(f"call:{job_id}", None)
    report = ctx.make_session_report()
    # Usage from the TYPED objects: SessionReport.to_dict() uses exclude_defaults and strips the usage `type`
    # discriminator, which made every call report zero cost.
    usage = [u.model_dump() for u in (report.model_usage or [])]
    payload: dict[str, Any] = {
        "job_id": job_id,
        "room": ctx.room.name,
        "caller_number": caller_number,
        "started_at": report.started_at,
        "duration_s": report.duration,
        "transcript": _transcript_from_report(report),
        "model_usage": usage,
    }
    if call is not None:
        payload.update(direction=call.direction, live_location=call.live_location,
                       greeting_prerendered=call.greeting_prerendered, caller_turns=call.caller_turns,
                       end_reason=call.end_reason or "caller_hangup")
        if not payload["transcript"]:
            payload["transcript"] = call.transcript
    # The report's audio_recording_path came back EMPTY in production; the recorder writes session_directory/audio.ogg.
    for candidate in (report.audio_recording_path, ctx.session_directory / "audio.ogg"):
        if candidate and Path(candidate).is_file() and Path(candidate).stat().st_size > 0:
            try:
                payload["recording_uri"] = await asyncio.wait_for(
                    asyncio.to_thread(_upload_recording, cfg, Path(candidate), job_id), timeout=120)
            except Exception as exc:  # noqa: BLE001 - a storage failure must not lose the transcript
                logger.error("recording upload failed: %r", exc)
            break
    if cfg.post_call_dir:
        out = Path(cfg.post_call_dir) / f"{job_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.post_call_webhook:
        await _post_with_retries(cfg.post_call_webhook, json.loads(json.dumps(payload, default=str)))
    logger.info("post-call done turns=%d recording=%s", len(payload["transcript"]), payload.get("recording_uri"))


# --------------------------------------------------------------------------------------------- entrypoint
server = AgentServer(setup_fnc=prewarm)  # prod: num_idle_processes = CPU count, health on :8081
AGENT_NAME = _env("AGENT_NAME", "my-agent")


async def _wait_for_caller(ctx: JobContext) -> rtc.RemoteParticipant | None:
    try:
        return await asyncio.wait_for(ctx.wait_for_participant(), timeout=PARTICIPANT_WAIT_S)  # SIP/CONNECTOR/STANDARD
    except asyncio.TimeoutError:
        logger.warning("no participant within %.0fs; continuing without caller attributes", PARTICIPANT_WAIT_S)
        return None


async def _dial_out(ctx: JobContext, cfg: Config, number: str) -> rtc.RemoteParticipant | None:
    identity = "sip-" + re.sub(r"\D", "", number)
    try:
        await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=cfg.outbound_trunk_id,
            sip_call_to=number,
            participant_identity=identity,
            participant_name="Callee",
            wait_until_answered=True,  # raises SipCallError on busy/no answer/5xx; voicemail returns 200 (use AMD)
        ))
    except api.SipCallError as exc:
        logger.info("outbound call failed: %s %s", exc.sip_status_code, exc.sip_status)
        ctx.shutdown(reason="outbound_failed")  # USER_UNAVAILABLE / SIP_TRUNK_FAILURE do not auto-close
        return None
    return await ctx.wait_for_participant(identity=identity)


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=on_session_end)  # explicit dispatch only
async def entrypoint(ctx: JobContext) -> None:
    cfg: Config = ctx.proc.userdata.get("cfg") or Config.from_env()
    errors = cfg.errors()
    if errors:
        raise RuntimeError("refusing call, config errors: " + "; ".join(errors))
    ctx.log_context_fields = {"room": ctx.room.name, "job_id": ctx.job.id}
    await ctx.connect()

    meta = json.loads(ctx.job.metadata or "{}")
    dial_number: str | None = meta.get("phone_number")  # present => outbound call placed by this agent
    if dial_number and not cfg.outbound_trunk_id:
        raise RuntimeError("outbound dispatch without SIP_OUTBOUND_TRUNK_ID")
    participant = await (_dial_out(ctx, cfg, dial_number) if dial_number else _wait_for_caller(ctx))
    if dial_number and participant is None:
        return

    caller_number = dial_number or caller_number_from(participant)
    if caller_number:
        ctx.proc.userdata[f"caller_number:{ctx.job.id}"] = caller_number  # popped in on_session_end

    warm = ctx.proc.userdata.get(LIVE_WARMUP_KEY)  # None => cold call beat prewarm
    location = warm["location"] if warm and warm["ok"] else cfg.location
    cached_greeting = warm["greeting"] if warm and warm["ok"] else None
    logger.info("pipeline=realtime live_location=%s warmup=%s greeting_cached=%s plugin=%s",
                location, "unprobed" if warm is None else ("ok" if warm["ok"] else "failed"),
                cached_greeting is not None, ".".join(map(str, _plugin_version())))

    hold = CallerAudioHold()
    controller = CallController(ctx, cfg, hold)
    controller.call.direction = "outbound" if dial_number else "inbound"
    controller.call.live_location = location
    ctx.proc.userdata[f"call:{ctx.job.id}"] = controller.call  # per job id; popped in on_session_end
    if participant is not None and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        controller.sip_participant = participant
    tools = [make_transfer_tool(controller)] if cfg.transfer_to else []  # register tools BEFORE start
    greeting_text = "" if dial_number else cfg.greeting
    agent = PhoneAgent(
        instructions=build_instructions(cfg, caller_number=caller_number, outbound=bool(dial_number),
                                        greeting_text=greeting_text),
        controller=controller,
        tools=tools,
    )
    controller.agent = agent

    session = AgentSession(
        llm=build_realtime_llm(cfg, location, hold),
        vad=ctx.proc.userdata["vad"],  # drives user_state + true end of speech; cannot end a realtime turn
        aec_warmup_duration=None,  # default 3 s for inbound SIP sends SILENCE to the model; the hold replaces it
    )
    controller.wire(session)

    audio_input = room_io.AudioInputOptions(sample_rate=16000, frame_size_ms=50)  # one resample, Gemini's input rate
    if cfg.noise_cancellation == "bvc_telephony" and noise_cancellation is not None:
        audio_input.noise_cancellation = noise_cancellation.BVCTelephony()  # LiveKit Cloud rooms only
    room_options = room_io.RoomOptions(
        audio_input=audio_input,
        audio_output=True,  # 24 kHz default = Gemini's native output rate
        close_on_disconnect=True,  # caller BYE -> session closes
        delete_room_on_close=True,  # THE hang-up for SIP: session close -> room deleted -> BYE (after drain)
    )
    if participant is not None:
        room_options.participant_identity = participant.identity
    await session.start(agent=agent, room=ctx.room, room_options=room_options, record=cfg.record)

    if cfg.ambience or cfg.thinking_sound:
        # Ambience is its own track (not in the agent recorder's file). On realtime the thinking sound plays only
        # while a tool runs, not in the end-of-turn gap.
        background = BackgroundAudioPlayer(
            ambient_sound=AudioConfig(BuiltinAudioClip[cfg.ambience], volume=cfg.ambience_volume) if cfg.ambience else None,
            thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.3),
                            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.25)] if cfg.thinking_sound else None,
        )
        await background.start(room=ctx.room, agent_session=session)

    if greeting_text:
        controller.call.greeting_prerendered = await controller.greet(greeting_text, cached_greeting)
    # Outbound: no greeting; the callee says "hello" and Gemini answers (the prompt says so).
    controller.start_monitor()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    cli.run_app(server)
