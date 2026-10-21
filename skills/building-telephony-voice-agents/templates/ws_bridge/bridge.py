"""bridge.py - telephony WebSocket media streams <-> Gemini Live (google-genai), one bridge for many providers.

Providers (adapters below): Twilio Media Streams, Exotel Voicebot applet / Connect Voice AI, Plivo Audio Streams.
Add another provider (including carrier protocols documented only under NDA) by writing one ProviderAdapter and one route.

What the core does per call:
  * Gemini Live session (google-genai `client.aio.live.connect`), opened while the provider handshakes;
    session resumption handle kept; on GoAway, reconnect at the next turn boundary (or 10 s before the deadline)
    while caller audio keeps buffering.
  * Caller audio: wire -> PCM16 16 kHz (telephony_audio.Codec) -> 40 ms chunks -> send_realtime_input.
  * Model audio: 24 kHz -> wire frames -> real-time paced sender (small lead) -> provider.
  * Barge-in: on server_content.interrupted drop the local queue, send the provider's clear, reset the pacer.
  * Greeting: pre-rendered in the Live voice at startup (if GREETING_TEXT is set) and played at `start`, or generated
    by Gemini. Caller audio is HELD (not dropped) during the greeting so line echo can't trigger a phantom reply;
    >= GREETING_BARGE_IN_S of continuous caller speech releases it with replay; otherwise it is discarded.
  * end_call tool -> model says goodbye -> playout drains (provider mark echo) -> grace window (cancelled if the caller
    speaks) -> provider hang-up (REST / socket close / in-band terminate). Model speech after end_call is suppressed.
  * transfer_to_human tool (only if TRANSFER_NUMBER is set) -> provider transfer after playout.
  * Silence monitor: check-in, final warning, goodbye + hang-up; max call duration.
  * DTMF -> text to the model. Provider hold/unhold events (if any) -> mute caller audio and pause the silence timer.

Verified against google-genai 2.21.0 (API names) with `python -m py_compile` and an offline smoke test (fake Live
session + fake provider socket). NOT verified against live phone calls: test on a real call before production.
Run: uvicorn bridge:app --host 0.0.0.0 --port 8080   (behind a TLS terminator; see README.md)
"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, NamedTuple, Optional, Protocol
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from google import genai
from google.genai import errors, types

import telephony_audio as ta

log = logging.getLogger("bridge")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


# =============================================================================================== settings
def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


PUBLIC_HOST = os.getenv("PUBLIC_HOST", "bot.example.com")          # host of the public wss/https URL
MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")           # check current model ids at build time
VOICE = os.getenv("GEMINI_VOICE", "Gacrux")
LANGUAGE_HINT = os.getenv("GEMINI_LANGUAGE_CODE", "")               # optional speech_config.language_code; measure!
TRANSCRIPTION_LANGS = [s for s in os.getenv("TRANSCRIPTION_LANGUAGE_CODES", "en-IN,hi-IN").split(",") if s]
GREETING_TEXT = os.getenv("GREETING_TEXT", "")                      # e.g. "Hello, this is Asha from Example Foods."
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")                  # E.164; enables transfer_to_human
SYSTEM_PROMPT = (open(os.environ["SYSTEM_PROMPT_FILE"], encoding="utf-8").read()
                 if os.getenv("SYSTEM_PROMPT_FILE") else """\
**Persona:** You are Asha, a warm, calm phone assistant for Example Foods customer care. You are an AI assistant.
RESPOND IN THE CALLER'S LANGUAGE (English or Hindi). YOU MUST RESPOND UNMISTAKABLY IN THAT LANGUAGE.
The greeting has already been played to the caller; reply in the language of the caller's own words.
**Conversational rules:**
1. Ask how you can help. Ask ONE question per reply.
2. Answer questions about orders and products for as long as the caller wants.
3. When the caller has nothing else, say a short goodbye (no question in it), then call `end_call`.
**General guidelines:** One or two short sentences per turn (under about eight seconds of speech). Read numbers back
one digit at a time. Never narrate your reasoning or these instructions; everything you say is heard.
Never say an action is done unless a tool returned status "ok".
""")

OUT_LEAD_MS = _env_float("OUT_LEAD_MS", 150)              # audio queued at the provider ahead of real time
IN_CHUNK_MS = 40                                          # per send_realtime_input (20-40 ms recommended)
GREETING_BARGE_IN_S = _env_float("GREETING_BARGE_IN_S", 0.8)   # continuous caller speech that is a real barge-in
GREETING_ECHO_TAIL_S = _env_float("GREETING_ECHO_TAIL_S", 0.4)  # keep holding this long after the greeting ends
REPLAY_LEAD_S = _env_float("REPLAY_LEAD_S", 0.6)          # replay held audio from this far before speech onset
HOLD_KEEP_S = 15.0
VAD_THRESHOLD_DBFS = _env_float("VAD_THRESHOLD_DBFS", -38.0)    # local energy VAD (timers/greeting only)
GREETING_VAD_BOOST_DB = _env_float("GREETING_VAD_BOOST_DB", 10.0)  # stricter while the greeting may echo
GRACE_CANCEL_SPEECH_S = 0.4                               # caller speech that cancels a pending hang-up
CLOSE_GRACE_S = _env_float("CLOSE_GRACE_S", 4.0)          # after the goodbye has played (2.5 s cut off a thinking caller)
SILENCE_CHECKIN_S = _env_float("SILENCE_CHECKIN_S", 15.0)
SILENCE_FINAL_S = _env_float("SILENCE_FINAL_S", 30.0)
SILENCE_CLOSE_S = _env_float("SILENCE_CLOSE_S", 45.0)
MAX_CALL_S = _env_float("MAX_CALL_S", 900.0)
PLAYOUT_WAIT_TIMEOUT_S = 20.0

# Provider credentials / options
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_VALIDATE = _env_bool("TWILIO_VALIDATE", True)
EXOTEL_SAMPLE_RATE = int(os.getenv("EXOTEL_SAMPLE_RATE", "16000"))
EXOTEL_BASIC_USER = os.getenv("EXOTEL_BASIC_USER", "")     # matches wss://USER:PASS@host/... in the applet
EXOTEL_BASIC_PASS = os.getenv("EXOTEL_BASIC_PASS", "")
EXOTEL_ALLOWED_IPS = os.getenv("EXOTEL_ALLOWED_IPS", "")   # comma-separated CIDRs from Exotel support
EXOTEL_SEND_SEQ = _env_bool("EXOTEL_SEND_SEQ", False)      # add chunk/timestamp/sequence_number (see 07 §3.6)
PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN", "")
PLIVO_CONTENT_TYPE = os.getenv("PLIVO_CONTENT_TYPE", "audio/x-l16;rate=16000")
PLIVO_STREAM_SECRET = os.getenv("PLIVO_STREAM_SECRET", "")  # echoed back via extraHeaders; checked at `start`
TRUST_PROXY_IP_HEADER = os.getenv("TRUST_PROXY_IP_HEADER", "")  # e.g. "x-forwarded-for" behind a trusted LB

# Gemini client: Vertex AI (ADC) if GOOGLE_CLOUD_PROJECT is set, else Gemini API key.
_credentials = None


def make_client() -> genai.Client:
    global _credentials
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if project and not _env_bool("FORCE_GEMINI_API_KEY", False):
        import google.auth
        import google.auth.transport.requests
        # Pre-refreshed ADC credentials: otherwise the SDK refreshes a token on every connect() (123-711 ms measured).
        _credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        _credentials.refresh(google.auth.transport.requests.Request())
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "us")      # gemini-3.8-live: us | eu | us-central1
        return genai.Client(vertexai=True, project=project, location=location, credentials=_credentials)
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def _refresh_credentials_forever() -> None:
    if _credentials is None:
        return
    import google.auth.transport.requests
    while True:
        await asyncio.sleep(30 * 60)
        try:
            await asyncio.to_thread(_credentials.refresh, google.auth.transport.requests.Request())
        except Exception:  # noqa: BLE001 - keep the old token, try again later
            log.exception("credential refresh failed")


client: Optional[genai.Client] = None


# =============================================================================================== Gemini config
def tool_declarations() -> list[types.Tool]:
    decls = [types.FunctionDeclaration(
        name="end_call",
        description=("Ends the phone call. Call only after you have said goodbye and the caller has nothing else. "
                     "Never call it while a question is still open. Say nothing after calling it."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    )]
    if TRANSFER_NUMBER:  # never offer a transfer the system can't do
        decls.append(types.FunctionDeclaration(
            name="transfer_to_human",
            description="Transfers the caller to a human agent. Call only when the caller asks for a person.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "reason": types.Schema(type=types.Type.STRING, description="One-line reason for the transfer."),
            }),
        ))
    decls.extend(EXTRA_TOOL_DECLARATIONS)
    return [types.Tool(function_declarations=decls)]


def build_config(handle: Optional[str]) -> types.LiveConnectConfig:
    # Deliberately NOT set for gemini-3.8-live: thinking_config (no audio), enable_affective_dialog (1007),
    # proactivity, small max_output_tokens (turn never completes), non-AUDIO modalities.
    # See references/02-gemini-live-model-and-config.md.
    speech = types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)))
    if LANGUAGE_HINT:
        speech.language_code = LANGUAGE_HINT
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]),
        speech_config=speech,
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=TRANSCRIPTION_LANGS or None),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=100,
                silence_duration_ms=500,
            ),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=100_000, sliding_window=types.SlidingWindow(target_tokens=60_000)),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        tools=tool_declarations(),
    )


# Your business tools: add declarations here and implementations in TOOL_IMPLS. Every implementation returns
# {"status": "ok"|"no_results"|"invalid_argument"|"error", "retryable": bool, "message": "..."} - never {} or None.
ToolFn = Callable[..., Awaitable[dict]]
EXTRA_TOOL_DECLARATIONS: list[types.FunctionDeclaration] = []
TOOL_IMPLS: dict[str, ToolFn] = {}


# =============================================================================================== greeting cache
GREETING_PCM24: list[bytes] = []   # pre-rendered greeting in the Live voice (24 kHz PCM16 chunks)


def _says(transcript: str, text: str) -> bool:
    """Accept a render only if it says the line: >= 70% of its words heard and <= 3 extra words."""
    want = re.findall(r"\w+", text.lower())
    got = re.findall(r"\w+", transcript.lower())
    if not want:
        return False
    heard = sum(1 for w in want if w in got)
    return heard / len(want) >= 0.7 and len(got) - heard <= 3


async def render_greeting(text: str) -> None:
    """Render GREETING_TEXT once in the call's Live voice. Live sometimes paraphrases or adds a preamble, so verify."""
    cfg = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=(
            "You are the voice of a customer-care phone line. When you are given a line, say exactly that line in "
            "a calm, steady, friendly tone at a relaxed pace, and nothing else."))]),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    for attempt in range(3):
        chunks: list[bytes] = []
        transcript = ""
        try:
            async with client.aio.live.connect(model=MODEL, config=cfg) as live:
                await live.send_realtime_input(text=f"Say exactly this line: {text}")
                async for msg in live.receive():  # receive() ends after turn_complete
                    sc = msg.server_content
                    if sc is None:
                        continue
                    for part in (sc.model_turn.parts if sc.model_turn else None) or []:
                        if part.inline_data and part.inline_data.data:
                            chunks.append(part.inline_data.data)
                    if sc.output_transcription and sc.output_transcription.text:
                        transcript += sc.output_transcription.text
        except Exception:  # noqa: BLE001
            log.exception("greeting render failed (attempt %d)", attempt + 1)
            continue
        if chunks and _says(transcript, text):
            GREETING_PCM24[:] = chunks
            log.info("greeting cached: %.1f s, transcript=%r", sum(map(len, chunks)) / 48000, transcript)
            return
        log.warning("greeting render rejected (attempt %d): %r", attempt + 1, transcript)
    log.warning("no cached greeting; Gemini will speak the greeting live")


# =============================================================================================== adapter interface
@dataclass
class CallInfo:
    stream_id: str
    call_id: Optional[str] = None
    caller: Optional[str] = None
    called: Optional[str] = None
    custom: dict = field(default_factory=dict)
    wire_rate: int = 8000


class Event(NamedTuple):
    kind: str          # "audio" | "dtmf" | "mark" | "hold" | "unhold" | "stop" | "ignore"
    value: Any = None


class ProviderAdapter(Protocol):
    name: str
    binary_audio: bool

    def authorize(self, ws: WebSocket) -> bool: ...
    def parse_start(self, msg: dict) -> Optional[CallInfo]: ...
    def make_codec(self, info: CallInfo) -> ta.Codec: ...
    def parse(self, msg: dict) -> Event: ...
    def media(self, frame: bytes) -> str | bytes: ...
    def mark(self, name: str) -> str: ...
    def clear(self) -> str: ...
    async def hangup(self, call: "CallBridge") -> None: ...
    async def transfer(self, call: "CallBridge", target: str) -> bool: ...


def _client_ip(ws: WebSocket) -> str:
    if TRUST_PROXY_IP_HEADER:
        v = ws.headers.get(TRUST_PROXY_IP_HEADER, "")
        if v:
            return v.split(",")[0].strip()
    return ws.client.host if ws.client else ""


def _ip_allowed(ip: str, cidrs: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(c.strip(), strict=False) for c in cidrs.split(",") if c.strip())


def _first(*vals: Any) -> Any:
    return next((v for v in vals if v not in (None, "")), None)


# ----------------------------------------------------------------------------------------------- Twilio
def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def twilio_signature_ok(sig: str, host: str, path: str, query: str, params: dict[str, str], schemes: tuple) -> bool:
    if not sig:
        return False
    q = f"?{query}" if query else ""
    for scheme in schemes:
        for p in {path, path.rstrip("/") + "/"}:
            if hmac.compare_digest(twilio_signature(TWILIO_AUTH_TOKEN, f"{scheme}://{host}{p}{q}", params), sig):
                return True
    return False


class TwilioAdapter:
    """<Connect><Stream url="wss://.../media/twilio">. mu-law 8 kHz, 160 B frames, streamSid (camelCase)."""
    name, binary_audio = "twilio", False

    def __init__(self) -> None:
        self.sid = ""

    def authorize(self, ws: WebSocket) -> bool:
        if not (TWILIO_VALIDATE and TWILIO_AUTH_TOKEN):
            return True
        # Which URL string Twilio signs for the WSS upgrade is not documented: accept wss/https, with/without "/".
        return twilio_signature_ok(ws.headers.get("x-twilio-signature", ""), PUBLIC_HOST, ws.url.path,
                                   ws.url.query, {}, ("wss", "https"))

    def parse_start(self, msg: dict) -> Optional[CallInfo]:
        if msg.get("event") != "start":
            return None
        s = msg.get("start") or {}
        self.sid = _first(s.get("streamSid"), msg.get("streamSid")) or ""
        if TWILIO_ACCOUNT_SID and s.get("accountSid") not in (None, TWILIO_ACCOUNT_SID):
            raise PermissionError("unexpected accountSid")
        return CallInfo(self.sid, s.get("callSid"), custom=s.get("customParameters") or {}, wire_rate=8000)

    def make_codec(self, info: CallInfo) -> ta.Codec:
        return ta.twilio_codec()

    def parse(self, msg: dict) -> Event:
        ev = msg.get("event")
        if ev == "media":
            if (msg.get("media") or {}).get("track", "inbound") != "inbound":
                return Event("ignore")
            return Event("audio", ta.b64d(msg["media"]["payload"]))
        if ev == "mark":
            return Event("mark", (msg.get("mark") or {}).get("name"))
        if ev == "dtmf":
            return Event("dtmf", (msg.get("dtmf") or {}).get("digit"))
        if ev == "stop":
            return Event("stop")
        return Event("ignore")

    def media(self, frame: bytes) -> str:
        return json.dumps({"event": "media", "streamSid": self.sid, "media": {"payload": ta.b64e(frame)}})

    def mark(self, name: str) -> str:
        return json.dumps({"event": "mark", "streamSid": self.sid, "mark": {"name": name}})

    def clear(self) -> str:
        return json.dumps({"event": "clear", "streamSid": self.sid})

    async def _update_call(self, call: "CallBridge", data: dict) -> bool:
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and call.info and call.info.call_id):
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call.info.call_id}.json"
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        log.info("twilio update %s -> %s", data, r.status_code)
        return r.is_success

    async def hangup(self, call: "CallBridge") -> None:
        if not await self._update_call(call, {"Status": "completed"}):
            await call.close_ws()  # with no TwiML after </Connect>, closing the stream ends the call

    async def transfer(self, call: "CallBridge", target: str) -> bool:
        return await self._update_call(call, {"Twiml": f"<Response><Dial>{target}</Dial></Response>"})


# ----------------------------------------------------------------------------------------------- Exotel
ESCALATIONS: dict[str, str] = {}   # call_sid -> transfer target, read by the Passthru/Connect endpoints


class ExotelAdapter:
    """Voicebot applet / Connect Voice AI. PCM16 LE at ?sample-rate (8k/16k/24k); outbound n*320 B, >= 3200 B.
    Accepts snake_case and camelCase; sends documented snake_case."""
    name, binary_audio = "exotel", False

    def __init__(self) -> None:
        self.sid = ""
        self.seq = 0
        self.chunk = 0
        self.t_ms = 0
        self.frame_ms = 100

    def authorize(self, ws: WebSocket) -> bool:
        if EXOTEL_BASIC_USER:
            want = "Basic " + base64.b64encode(f"{EXOTEL_BASIC_USER}:{EXOTEL_BASIC_PASS}".encode()).decode()
            if not hmac.compare_digest(ws.headers.get("authorization", ""), want):
                return False
        if EXOTEL_ALLOWED_IPS and not _ip_allowed(_client_ip(ws), EXOTEL_ALLOWED_IPS):
            return False
        return True

    def parse_start(self, msg: dict) -> Optional[CallInfo]:
        if msg.get("event") != "start":
            return None
        s = msg.get("start") or {}
        self.sid = _first(msg.get("stream_sid"), msg.get("streamSid"), s.get("stream_sid"), s.get("streamSid")) or ""
        mf = s.get("media_format") or s.get("mediaFormat") or {}
        rate = int(_first(mf.get("sample_rate"), mf.get("sampleRate")) or EXOTEL_SAMPLE_RATE)
        custom = s.get("custom_parameters") or s.get("customParameters") or {}
        return CallInfo(self.sid, _first(s.get("call_sid"), s.get("callSid")), s.get("from"), s.get("to"),
                        custom, wire_rate=rate)

    def make_codec(self, info: CallInfo) -> ta.Codec:
        c = ta.exotel_codec(info.wire_rate)
        self.frame_ms = int(c.frame_seconds * 1000)
        return c

    def parse(self, msg: dict) -> Event:
        ev = msg.get("event")
        if ev == "media":
            return Event("audio", ta.b64d(msg["media"]["payload"]))
        if ev == "mark":
            return Event("mark", (msg.get("mark") or {}).get("name"))
        if ev == "dtmf":
            return Event("dtmf", (msg.get("dtmf") or {}).get("digit"))
        if ev == "stop":
            return Event("stop", (msg.get("stop") or {}).get("reason"))
        return Event("ignore")  # includes an inbound "clear" that one Exotel guide mentions

    def media(self, frame: bytes) -> str:
        media: dict[str, Any] = {"payload": ta.b64e(frame)}
        if EXOTEL_SEND_SEQ:  # Exotel's own sample adds these; some Connect streams end early without them
            self.seq += 1
            self.chunk += 1
            media.update(chunk=str(self.chunk), timestamp=str(self.t_ms))
            self.t_ms += self.frame_ms
            return json.dumps({"event": "media", "stream_sid": self.sid, "sequence_number": str(self.seq),
                               "media": media})
        return json.dumps({"event": "media", "stream_sid": self.sid, "media": media})

    def mark(self, name: str) -> str:
        return json.dumps({"event": "mark", "stream_sid": self.sid, "mark": {"name": name}})

    def clear(self) -> str:
        return json.dumps({"event": "clear", "stream_sid": self.sid})

    async def hangup(self, call: "CallBridge") -> None:
        await call.close_ws()  # flow -> next applet (Hangup); Connect Voice AI -> call ends

    async def transfer(self, call: "CallBridge", target: str) -> bool:
        if not (call.info and call.info.call_id):
            return False
        ESCALATIONS[call.info.call_id] = target  # use a shared store (Redis/DB) with more than one instance
        await call.close_ws()                    # flow continues: Passthru (302) -> Connect applet (dynamic URL)
        return True


# ----------------------------------------------------------------------------------------------- Plivo
def _parse_extra_headers(s: Any) -> dict:
    if isinstance(s, dict):
        return s
    out = {}
    for kv in str(s or "").split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class PlivoAdapter:
    """<Stream bidirectional="true" keepCallAlive="true" contentType=...>. playAudio/checkpoint/clearAudio, streamId."""
    name, binary_audio = "plivo", False

    def __init__(self) -> None:
        self.sid = ""
        self.content_type = PLIVO_CONTENT_TYPE.split(";")[0]
        self.rate = int(PLIVO_CONTENT_TYPE.split("rate=")[1]) if "rate=" in PLIVO_CONTENT_TYPE else 8000

    def authorize(self, ws: WebSocket) -> bool:
        return True  # checked at `start` via PLIVO_STREAM_SECRET (X-Plivo-Signature-V3 handshake check: VERIFY)

    def parse_start(self, msg: dict) -> Optional[CallInfo]:
        if msg.get("event") != "start":
            return None
        s = msg.get("start") or {}
        extra = _parse_extra_headers(msg.get("extra_headers") or s.get("extra_headers"))
        if PLIVO_STREAM_SECRET and not hmac.compare_digest(extra.get("secret", ""), PLIVO_STREAM_SECRET):
            raise PermissionError("bad stream secret")
        self.sid = _first(s.get("streamId"), msg.get("streamId")) or ""
        mf = s.get("mediaFormat") or {}
        rate = int(mf.get("sampleRate") or self.rate)
        return CallInfo(self.sid, s.get("callId"), custom=extra, wire_rate=rate)

    def make_codec(self, info: CallInfo) -> ta.Codec:
        return ta.plivo_codec(PLIVO_CONTENT_TYPE)

    def parse(self, msg: dict) -> Event:
        ev = msg.get("event")
        if ev == "media":
            return Event("audio", ta.b64d(msg["media"]["payload"]))
        if ev == "playedStream":
            return Event("mark", msg.get("name"))
        if ev == "dtmf":
            return Event("dtmf", (msg.get("dtmf") or {}).get("digit"))
        if ev == "stop":
            return Event("stop")
        return Event("ignore")  # clearedAudio etc.

    def media(self, frame: bytes) -> str:
        return json.dumps({"event": "playAudio", "media": {"contentType": self.content_type,
                                                           "sampleRate": self.rate, "payload": ta.b64e(frame)}})

    def mark(self, name: str) -> str:
        return json.dumps({"event": "checkpoint", "streamId": self.sid, "name": name})

    def clear(self) -> str:
        return json.dumps({"event": "clearAudio", "streamId": self.sid})

    def _call_url(self, call: "CallBridge") -> Optional[str]:
        uuid_ = call.info.call_id if call.info else None
        if not (PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN and uuid_):
            return None  # NEVER call .../Call/ without a uuid: that disconnects every call on the account
        return f"https://api.plivo.com/v1/Account/{PLIVO_AUTH_ID}/Call/{uuid_}/"

    async def hangup(self, call: "CallBridge") -> None:
        url = self._call_url(call)
        if url is None:
            await call.close_ws()  # keepCallAlive="false" ends the call; with "true" the next XML verb runs
            return
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.delete(url, auth=(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN))
        log.info("plivo hangup -> %s", r.status_code)

    async def transfer(self, call: "CallBridge", target: str) -> bool:
        url = self._call_url(call)
        if url is None:
            return False
        aleg = f"https://{PUBLIC_HOST}/plivo/transfer-xml?to={quote(target)}"
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(url, auth=(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN),
                             json={"legs": "aleg", "aleg_url": aleg, "aleg_method": "GET"})
        log.info("plivo transfer -> %s", r.status_code)
        return r.is_success


# =============================================================================================== the call bridge
class CallerAudioHold:
    """Hold (don't drop) caller audio during the greeting. release(replay_since=t) replays what the caller said
    since t (a real barge-in); release() discards it (it was line echo of the greeting)."""

    def __init__(self) -> None:
        self.held: Optional[collections.deque] = None

    @property
    def holding(self) -> bool:
        return self.held is not None

    def hold(self) -> None:
        self.held = collections.deque()

    def add(self, t: float, pcm16k: bytes) -> None:
        self.held.append((t, pcm16k))
        while self.held and t - self.held[0][0] > HOLD_KEEP_S:
            self.held.popleft()

    def release(self, replay_since: Optional[float] = None) -> list[bytes]:
        held, self.held = self.held, None
        if not held or replay_since is None:
            return []
        return [pcm for t, pcm in held if t >= replay_since]


class CallBridge:
    def __init__(self, ws: WebSocket, adapter: ProviderAdapter):
        self.ws, self.adapter = ws, adapter
        self.info: Optional[CallInfo] = None
        self.codec: Optional[ta.Codec] = None
        self.loop = asyncio.get_running_loop()
        self.started_at = time.monotonic()
        self.send_lock = asyncio.Lock()
        self.ws_closed = False
        self.done = asyncio.Event()
        # caller -> Gemini
        self.mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=250)   # ~10 s of 40 ms chunks (reconnects)
        self.mic_buf = bytearray()
        self.vad = ta.EnergyVAD(VAD_THRESHOLD_DBFS)
        self.hold = CallerAudioHold()
        self.on_hold = False                                              # provider hold/unhold event
        # Gemini -> caller
        self.out_q: asyncio.Queue[tuple[int, str, Any]] = asyncio.Queue()
        self.epoch = 0
        self.pacer: Optional[ta.Pacer] = None
        self.mark_n = 0
        self.mark_waiters: dict[str, asyncio.Event] = {}
        self.suppress_audio = False
        self.suppress_after_turn = False
        self.turn_active = False
        self.turns_completed = 0
        self.last_transcript_at = 0.0
        # Gemini session
        self.session: Any = None
        self.session_ready = asyncio.Event()
        self.handle: Optional[str] = None
        self.goaway_deadline: Optional[float] = None
        self.tool_tasks: dict[str, asyncio.Task] = {}
        # conversation state
        self.greeting_done = asyncio.Event()
        self.greeting_from_cache = False
        self.greeting_interrupted = False
        self.closing = False                 # hang-up armed (after end_call / silence close / max duration)
        self.close_cancellable = True
        self.pending_transfer: Optional[str] = None
        self.last_activity = time.monotonic()
        self.silence_stage = 0
        self.nudge_inflight = False

    # ------------------------------------------------------------------ websocket I/O
    async def ws_send(self, data: str | bytes) -> None:
        if self.ws_closed:
            return
        async with self.send_lock:
            try:
                if isinstance(data, bytes):
                    await self.ws.send_bytes(data)
                else:
                    await self.ws.send_text(data)
            except Exception:  # noqa: BLE001 - provider went away
                self.ws_closed = True
                self.done.set()

    async def close_ws(self) -> None:
        if not self.ws_closed:
            self.ws_closed = True
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.done.set()

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        gemini = asyncio.create_task(self.gemini_loop(), name="gemini")   # connect while the provider handshakes
        tasks: list[asyncio.Task] = [gemini]
        try:
            if not await self.handshake():
                return
            tasks += [asyncio.create_task(self.mic_sender(), name="mic"),
                      asyncio.create_task(self.sender(), name="sender"),
                      asyncio.create_task(self.greeting(), name="greeting")]
            # the call ends when the provider stops, Gemini fails for good, the watchdog finishes, or done is set
            enders = [gemini,
                      asyncio.create_task(self.reader(), name="reader"),
                      asyncio.create_task(self.watchdog(), name="watchdog"),
                      asyncio.create_task(self.done.wait(), name="done")]
            tasks += enders[1:]
            finished, _ = await asyncio.wait(enders, return_when=asyncio.FIRST_COMPLETED)
            for t in finished:
                if not t.cancelled() and t.exception() is not None:
                    log.error("task %s failed: %r", t.get_name(), t.exception())
        finally:
            self.done.set()
            for t in tasks + list(self.tool_tasks.values()):
                t.cancel()
            await self.close_ws()
            log.info("call %s ended after %.1f s", self.info.call_id if self.info else "?",
                     time.monotonic() - self.started_at)

    async def _receive_json(self) -> Optional[dict | bytes]:
        msg = await self.ws.receive()
        if msg.get("type") == "websocket.disconnect":
            return None
        if msg.get("text") is not None:
            try:
                return json.loads(msg["text"])
            except json.JSONDecodeError:
                log.warning("non-JSON text frame ignored")
                return {}
        if msg.get("bytes") is not None:
            data = msg["bytes"]
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return data  # binary audio (Vonage-style adapters)
        return {}

    async def handshake(self) -> bool:
        while True:
            msg = await self._receive_json()
            if msg is None:
                return False
            if not isinstance(msg, dict):
                continue
            try:
                info = self.adapter.parse_start(msg)
            except PermissionError as e:
                log.warning("%s start rejected: %s", self.adapter.name, e)
                await self.ws.close(code=1008)
                self.ws_closed = True
                return False
            if info is not None:
                self.info = info
                self.codec = self.adapter.make_codec(info)
                self.pacer = ta.Pacer(self.codec.frame_seconds * 1000, OUT_LEAD_MS)
                self.hold.hold()   # hold caller audio until the greeting is over
                log.info("%s call=%s stream=%s from=%s to=%s rate=%d custom=%s", self.adapter.name, info.call_id,
                         info.stream_id, info.caller, info.called, info.wire_rate, info.custom)
                return True

    # ------------------------------------------------------------------ caller -> Gemini
    async def reader(self) -> None:
        while True:
            msg = await self._receive_json()
            if msg is None:
                return
            ev = Event("audio", msg) if isinstance(msg, bytes) else self.adapter.parse(msg)
            if ev.kind == "audio":
                self._on_caller_audio(ev.value)
            elif ev.kind == "mark":
                w = self.mark_waiters.pop(ev.value, None)
                if w:
                    w.set()
            elif ev.kind == "dtmf":
                self.last_activity = time.monotonic()
                await self._send_text_to_model(f"(The caller pressed {ev.value} on the keypad.)")
            elif ev.kind == "hold":
                self.on_hold = True
            elif ev.kind == "unhold":
                self.on_hold = False
                self.last_activity = time.monotonic()
            elif ev.kind == "stop":
                log.info("provider stop (%s)", ev.value)
                return

    def _on_caller_audio(self, payload: bytes) -> None:
        now = time.monotonic()
        self.vad.threshold = VAD_THRESHOLD_DBFS + (GREETING_VAD_BOOST_DB if self.hold.holding else 0.0)
        speaking = self.vad.update(self.codec.wire_to_pcm(payload), now)
        pcm16k = self.codec.to_gemini(payload)
        if speaking and not self.on_hold:
            self.last_activity = now
        if self.on_hold:
            pcm16k = bytes(len(pcm16k))  # keep the stream continuous for server VAD, but send silence
        elif self.hold.holding:
            self.hold.add(now, pcm16k)
            pcm16k = bytes(len(pcm16k))
            if self.vad.continuous_speech_s(now) >= GREETING_BARGE_IN_S:
                self._greeting_barge_in(now)
        self._push_mic(pcm16k)

    def _push_mic(self, pcm16k: bytes) -> None:
        self.mic_buf += pcm16k
        n = ta.GEMINI_IN_RATE * 2 * IN_CHUNK_MS // 1000
        while len(self.mic_buf) >= n:
            chunk = bytes(self.mic_buf[:n])
            del self.mic_buf[:n]
            if self.mic_q.full():   # during a long reconnect drop the oldest audio
                self.mic_q.get_nowait()
            self.mic_q.put_nowait(chunk)

    async def mic_sender(self) -> None:
        while True:
            chunk = await self.mic_q.get()
            await self.session_ready.wait()
            try:
                await self.session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={ta.GEMINI_IN_RATE}"))
            except Exception as e:  # noqa: BLE001 - connection dropping; gemini_loop reconnects
                log.debug("audio send failed: %s", e)

    async def _send_text_to_model(self, text: str) -> None:
        if not self.session_ready.is_set():
            return
        try:
            await self.session.send_realtime_input(text=text)
        except Exception as e:  # noqa: BLE001
            log.warning("text send failed: %s", e)

    async def _send_system_turn(self, text: str) -> None:
        """System-driven prompt (silence ladder, max duration). Instructions, not fixed lines: the model says them in
        the call's language."""
        if not self.session_ready.is_set():
            return
        try:
            await self.session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)
        except Exception as e:  # noqa: BLE001
            log.warning("system turn failed: %s", e)

    # ------------------------------------------------------------------ Gemini -> caller
    def _enqueue_audio(self, frames: list[bytes] | Any) -> None:
        for f in frames:
            self.out_q.put_nowait((self.epoch, "audio", f))

    def _enqueue_mark(self) -> str:
        self.mark_n += 1
        name = f"m{self.mark_n}"
        self.mark_waiters[name] = asyncio.Event()
        self.out_q.put_nowait((self.epoch, "mark", name))
        return name

    async def sender(self) -> None:
        while True:
            epoch, kind, val = await self.out_q.get()
            if epoch != self.epoch:
                continue
            if kind == "mark":
                await self.ws_send(self.adapter.mark(val))
                continue
            await self.pacer.wait_turn()
            if epoch != self.epoch:   # flushed while we waited
                continue
            await self.ws_send(self.adapter.media(val))

    def playout_busy(self) -> bool:
        return not self.out_q.empty() or (self.pacer is not None and self.pacer.queued_s() > 0.0)

    async def wait_playout(self, timeout: float = PLAYOUT_WAIT_TIMEOUT_S) -> None:
        """Wait until what we queued has played: a provider mark echo, falling back to the local pacer clock."""
        name = self._enqueue_mark()
        t_end = time.monotonic() + timeout
        try:
            await asyncio.wait_for(self.mark_waiters[name].wait(), timeout)
        except (asyncio.TimeoutError, KeyError):
            while self.playout_busy() and time.monotonic() < t_end:
                await asyncio.sleep(0.05)

    async def flush_playout(self) -> None:
        """Barge-in: drop local audio, clear the provider buffer, reset the pacer. Keep the resamplers."""
        self.epoch += 1
        while not self.out_q.empty():
            self.out_q.get_nowait()
        self.codec.barge_in()
        self.pacer.reset()
        for w in self.mark_waiters.values():   # cleared audio counts as finished (Twilio also echoes the marks)
            w.set()
        self.mark_waiters.clear()
        await self.ws_send(self.adapter.clear())

    # ------------------------------------------------------------------ greeting
    async def greeting(self) -> None:
        if GREETING_PCM24:
            self.greeting_from_cache = True
            for chunk in GREETING_PCM24:            # plays immediately; no dependency on the Live socket
                self._enqueue_audio(self.codec.from_gemini(chunk))
            self._enqueue_audio(self.codec.end_of_turn())
            await self.wait_playout()
            self.greeting_done.set()
            await self.session_ready.wait()
            if not self.greeting_interrupted:       # pushing context after an interruption would cancel the reply
                try:
                    await self.session.send_client_content(
                        turns=types.Content(role="model", parts=[types.Part(text=GREETING_TEXT)]),
                        turn_complete=False)
                except Exception as e:  # noqa: BLE001
                    log.warning("greeting context push failed: %s", e)
        else:
            await self.session_ready.wait()
            line = (f"Greet the caller with exactly these words and nothing else: {GREETING_TEXT}" if GREETING_TEXT
                    else "Greet the caller briefly and ask how you can help.")
            await self._send_text_to_model(f"(The phone call has just connected. {line})")
            await self.greeting_done.wait()          # set at the first turn_complete, after playout
        if self.hold.holding:
            await asyncio.sleep(GREETING_ECHO_TAIL_S)   # the line keeps echoing briefly after playout ends
            if self.hold.holding:
                self.hold.release(replay_since=None)     # it was only echo: discard
        self.last_activity = time.monotonic()

    def _greeting_barge_in(self, now: float) -> None:
        started = self.vad.speech_started_at or now
        replay = self.hold.release(replay_since=started - REPLAY_LEAD_S)
        self.greeting_interrupted = True
        log.info("caller barged in on the greeting; replaying %.1f s", sum(map(len, replay)) / 32000)
        asyncio.create_task(self.flush_playout())
        for pcm in replay:
            self._push_mic(pcm)
        self.greeting_done.set()

    # ------------------------------------------------------------------ Gemini session (resumption + GoAway)
    async def gemini_loop(self) -> None:
        backoff = 0.5
        while not self.done.is_set():
            try:
                async with client.aio.live.connect(model=MODEL, config=build_config(self.handle)) as s:
                    self.session, self.goaway_deadline = s, None
                    self.session_ready.set()
                    backoff = 0.5
                    log.info("gemini connected (resumed=%s)", self.handle is not None)
                    await self._pump_until_reconnect(s)
            except errors.APIError as e:
                log.warning("gemini closed: %s %s", e.code, e.message)
                if e.code not in (1000, 1006, 1011) or self.handle is None:
                    raise
            finally:
                self.session_ready.clear()
                self.session = None
            if self.done.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 4.0)

    async def _pump_until_reconnect(self, s: Any) -> None:
        async def pump() -> None:
            while True:  # receive() ends after each turn_complete
                async for msg in s.receive():
                    await self.on_message(msg)
                if self.goaway_deadline is not None:
                    # turn boundary: let the reply finish playing, then swap connections (caller audio keeps
                    # buffering in mic_q meanwhile; measured resume ~2.3 s)
                    while self.playout_busy() and time.monotonic() < self.goaway_deadline - 10:
                        await asyncio.sleep(0.05)
                    return

        async def deadline() -> None:
            while self.goaway_deadline is None or time.monotonic() < self.goaway_deadline - 10:
                await asyncio.sleep(0.5)

        p, d = asyncio.create_task(pump()), asyncio.create_task(deadline())
        done, pending = await asyncio.wait({p, d}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception() is not None:
                raise t.exception()

    async def on_message(self, msg: types.LiveServerMessage) -> None:
        if msg.session_resumption_update:
            u = msg.session_resumption_update
            if u.resumable and u.new_handle:
                self.handle = u.new_handle
        if msg.go_away is not None:
            left = float(re.sub(r"[^0-9.]", "", msg.go_away.time_left or "") or 30)
            self.goaway_deadline = time.monotonic() + left
            log.info("GoAway: %.0f s left; will reconnect at the next turn boundary", left)
        if msg.tool_call:
            for fc in msg.tool_call.function_calls or []:
                if fc.id in self.tool_tasks:   # duplicate while pending: ignore
                    continue
                self.tool_tasks[fc.id] = asyncio.create_task(self.run_tool(fc))
        if msg.tool_call_cancellation:
            for cid in msg.tool_call_cancellation.ids or []:
                t = self.tool_tasks.pop(cid, None)
                if t:
                    t.cancel()
        sc = msg.server_content
        if sc is None:
            return
        if sc.interrupted:
            log.info("barge-in")
            await self.flush_playout()
        if sc.model_turn and not self.suppress_audio:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    self.turn_active = True
                    self._enqueue_audio(self.codec.from_gemini(part.inline_data.data))
        if sc.input_transcription and sc.input_transcription.text:
            self.last_activity = self.last_transcript_at = time.monotonic()
            log.info("CALLER: %s", sc.input_transcription.text)
        if sc.output_transcription and sc.output_transcription.text:
            log.info("AGENT : %s", sc.output_transcription.text)
        if sc.turn_complete:
            await self._on_turn_complete()

    async def _on_turn_complete(self) -> None:
        if not self.suppress_audio:
            self._enqueue_audio(self.codec.end_of_turn())
        self.turn_active = False
        self.turns_completed += 1
        if self.suppress_after_turn:
            self.suppress_audio, self.suppress_after_turn = True, False
        if not self.nudge_inflight:
            self.last_activity = time.monotonic()
        self.nudge_inflight = False
        if not self.greeting_from_cache and not self.greeting_done.is_set():
            asyncio.create_task(self._finish_live_greeting())

    async def _finish_live_greeting(self) -> None:
        await self.wait_playout()
        self.greeting_done.set()

    # ------------------------------------------------------------------ tools, hang-up, transfer
    async def run_tool(self, fc: types.FunctionCall) -> None:
        args = dict(fc.args or {})
        log.info("tool %s %s", fc.name, args)
        try:
            if fc.name == "end_call":
                result = {"status": "ok", "retryable": False, "message": "The call is ending. Say nothing more."}
                self._arm_close("end_call")
            elif fc.name == "transfer_to_human" and TRANSFER_NUMBER:
                result = {"status": "ok", "retryable": False,
                          "message": "Transferring the caller now. Say nothing more."}
                self.pending_transfer = TRANSFER_NUMBER
                self._arm_close("transfer")
            elif fc.name in TOOL_IMPLS:
                result = await TOOL_IMPLS[fc.name](**args)
            else:
                result = {"status": "error", "retryable": False, "message": f"Unknown tool {fc.name}."}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("tool %s failed", fc.name)
            result = {"status": "error", "retryable": False,
                      "message": "The system is unavailable. Apologise and offer a callback."}
        finally:
            self.tool_tasks.pop(fc.id, None)
        await self.session_ready.wait()
        try:
            await self.session.send_tool_response(function_responses=[
                types.FunctionResponse(id=fc.id, name=fc.name, response=result)])
        except Exception as e:  # noqa: BLE001
            log.warning("tool response not delivered: %s", e)
        if self.closing:
            # Vertex answers even a "say nothing" tool result ("I have ended the call"): suppress what follows.
            # If the goodbye turn is still streaming, start suppressing only after it completes.
            if self.turn_active:
                self.suppress_after_turn = True
            else:
                self.suppress_audio = True

    def _arm_close(self, why: str, *, wait_for_new_turn: bool = False, cancellable: bool = True) -> None:
        if not self.closing:
            log.info("close armed (%s)", why)
            self.closing, self.close_cancellable = True, cancellable
            asyncio.create_task(self._close_after_goodbye(self.turns_completed if wait_for_new_turn else None))

    def _cancel_close(self, why: str) -> None:
        log.info("close cancelled (%s)", why)
        self.closing = False
        self.suppress_audio = self.suppress_after_turn = False

    async def _close_after_goodbye(self, turns_at_arm: Optional[int]) -> None:
        # 1. system-driven close: wait for the goodbye turn we just asked for (bounded)
        if turns_at_arm is not None:
            t_end = time.monotonic() + 15.0
            while self.turns_completed <= turns_at_arm and time.monotonic() < t_end:
                await asyncio.sleep(0.05)
        # 2. let the goodbye finish generating and playing (marks follow the audio in order)
        while self.turn_active:
            await asyncio.sleep(0.05)
        await self.wait_playout()
        # 3. grace window: real caller speech (or a new transcript) cancels the hang-up and un-mutes the model.
        #    Measured only AFTER playout, so the goodbye's own line echo can't cancel it.
        grace_start = time.monotonic()
        t_end = grace_start + (0.5 if self.pending_transfer else CLOSE_GRACE_S)
        while time.monotonic() < t_end:
            if not self.closing:
                return
            if self.close_cancellable and not self.pending_transfer and (
                    self.vad.continuous_speech_s() >= GRACE_CANCEL_SPEECH_S or self.last_transcript_at > grace_start):
                self._cancel_close("caller spoke after the goodbye")
                return
            await asyncio.sleep(0.05)
        if not self.closing:
            return
        # 4. act
        if self.pending_transfer:
            target, self.pending_transfer = self.pending_transfer, None
            if await self.adapter.transfer(self, target):
                log.info("transferred to %s", target)
                await asyncio.sleep(1.0)
                self.done.set()
                return
            self._cancel_close("transfer failed")
            await self._send_system_turn(
                "(System note: the transfer failed and the caller is still on the line. Apologise briefly and "
                "promise that a person will call them back soon. Do not give a time.)")
            return
        log.info("hanging up")
        await self.adapter.hangup(self)
        await asyncio.sleep(1.0)
        self.done.set()

    # ------------------------------------------------------------------ timers
    async def watchdog(self) -> None:
        max_duration_sent = False
        while not self.done.is_set():
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - self.started_at > MAX_CALL_S and not max_duration_sent:
                max_duration_sent = True
                if self.closing:
                    continue
                self.nudge_inflight = True
                await self._send_system_turn(
                    "(System note: the maximum call time has been reached. In the language you have been speaking, "
                    "say a short goodbye now. The phone system ends the call straight after; ask no question and "
                    "do not call any tool.)")
                self._arm_close("max duration", wait_for_new_turn=True, cancellable=False)
                continue
            if (self.closing or self.on_hold or not self.greeting_done.is_set() or self.hold.holding
                    or self.turn_active or self.playout_busy() or not self.session_ready.is_set()
                    or self.vad.speaking):
                continue
            idle = now - self.last_activity
            if self.silence_stage and idle < SILENCE_CHECKIN_S and not self.nudge_inflight:
                self.silence_stage = 0   # the caller came back: re-arm the ladder
            # After a check-in the silence clock is NOT reset (nudge turns don't count as activity), so the ladder
            # progresses: check-in -> final warning -> goodbye + hang-up.
            if self.silence_stage == 0 and idle > SILENCE_CHECKIN_S:
                self.silence_stage, self.nudge_inflight = 1, True
                await self._send_system_turn(
                    "(System note: the caller has gone quiet. In the language you have been speaking with them, "
                    "check gently that they are still there and tell them to take their time. One short sentence. "
                    "This is not the end of the call: do not say goodbye and do not call end_call.)")
            elif self.silence_stage == 1 and idle > SILENCE_FINAL_S:
                self.silence_stage, self.nudge_inflight = 2, True
                await self._send_system_turn(
                    "(System note: the caller is still silent. In the language you have been speaking, tell them "
                    "you will end the call shortly if you cannot hear them. Ask no question. Do not call end_call.)")
            elif self.silence_stage == 2 and idle > SILENCE_CLOSE_S:
                self.silence_stage, self.nudge_inflight = 3, True
                await self._send_system_turn(
                    "(System note: no response from the caller. In the language you have been speaking, say a "
                    "short goodbye. The phone system ends the call straight after; ask no question and do not call "
                    "any tool.)")
                self._arm_close("silence", wait_for_new_turn=True)


# =============================================================================================== FastAPI app
@asynccontextmanager
async def lifespan(_: FastAPI):
    global client
    client = make_client()
    bg = [asyncio.create_task(_refresh_credentials_forever())]
    if GREETING_TEXT and _env_bool("PRERENDER_GREETING", True):
        bg.append(asyncio.create_task(render_greeting(GREETING_TEXT)))  # off the request path; calls fall back
    yield
    for t in bg:
        t.cancel()


app = FastAPI(title="telephony-gemini-live-bridge", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": MODEL, "greeting_cached": bool(GREETING_PCM24)}


async def _serve(ws: WebSocket, adapter: ProviderAdapter) -> None:
    if not adapter.authorize(ws):
        log.warning("%s handshake rejected from %s", adapter.name, _client_ip(ws))
        await ws.close(code=1008)   # before accept -> HTTP 403
        return
    await ws.accept()
    await CallBridge(ws, adapter).run()


@app.websocket("/media/twilio")
async def media_twilio(ws: WebSocket) -> None:
    await _serve(ws, TwilioAdapter())


@app.websocket("/media/exotel")
async def media_exotel(ws: WebSocket) -> None:
    await _serve(ws, ExotelAdapter())


@app.websocket("/media/plivo")
async def media_plivo(ws: WebSocket) -> None:
    await _serve(ws, PlivoAdapter())


# ----------------------------------------------------------------------------------------------- Twilio webhook
@app.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    if TWILIO_VALIDATE and TWILIO_AUTH_TOKEN and not twilio_signature_ok(
            request.headers.get("x-twilio-signature", ""), PUBLIC_HOST, request.url.path, request.url.query,
            form, ("https",)):
        return Response(status_code=403)
    params = "".join(f'<Parameter name="{k}" value={quoteattr(form.get(v, ""))}/>'
                     for k, v in (("from", "From"), ("to", "To")))
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
           f'<Stream url="wss://{PUBLIC_HOST}/media/twilio">{params}</Stream></Connect></Response>')
    return Response(content=xml, media_type="application/xml")


# ----------------------------------------------------------------------------------------------- Exotel helpers
@app.get("/exotel/stream-url")
async def exotel_stream_url(request: Request) -> JSONResponse:
    """Voicebot applet 'dynamic URL': Exotel GETs this with CallSid, From, To, ... and expects {"url": "wss://..."}."""
    call_sid = request.query_params.get("CallSid", "")
    return JSONResponse({"url": f"wss://{PUBLIC_HOST}/media/exotel?sample-rate={EXOTEL_SAMPLE_RATE}"
                                f"&call_sid={quote(call_sid)}"})


@app.get("/exotel/passthru")
async def exotel_passthru(request: Request) -> Response:
    """Passthru after the Voicebot applet: 302 = branch B (transfer via Connect applet), 200 = branch A (hang up)."""
    call_sid = request.query_params.get("CallSid", "")
    log.info("exotel passthru %s status=%s by=%s", call_sid, request.query_params.get("Stream[Status]"),
             request.query_params.get("Stream[DisconnectedBy]"))
    return Response(status_code=302 if call_sid in ESCALATIONS else 200)


@app.get("/exotel/connect")
async def exotel_connect(request: Request) -> JSONResponse:
    """Connect applet dynamic URL: where to send an escalated caller."""
    target = ESCALATIONS.pop(request.query_params.get("CallSid", ""), TRANSFER_NUMBER)
    return JSONResponse({"destination": {"numbers": [target]}, "record": True, "max_ringing_duration": 30})


# ----------------------------------------------------------------------------------------------- Plivo helpers
@app.api_route("/plivo/answer", methods=["GET", "POST"])
async def plivo_answer(_: Request) -> Response:
    extra = f"secret={PLIVO_STREAM_SECRET}" if PLIVO_STREAM_SECRET else ""
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
           f'<Stream bidirectional="true" keepCallAlive="false" contentType="{PLIVO_CONTENT_TYPE}"'
           f' extraHeaders="{extra}">wss://{PUBLIC_HOST}/media/plivo</Stream></Response>')
    return Response(content=xml, media_type="application/xml")


@app.get("/plivo/transfer-xml")
async def plivo_transfer_xml(to: str) -> Response:
    if not re.fullmatch(r"\+?\d{6,15}", to):
        return Response(status_code=400)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial><Number>{to}</Number></Dial></Response>'
    return Response(content=xml, media_type="application/xml")
