# 03. Conversation mechanics on a phone line: greeting, echo, barge-in, silence, hang-up, tools, turn-taking, language, voice, background audio

This is the most important reference. It covers how a Gemini Live phone call behaves from the first ring to the hang-up, and what code you must add, because the model owns the conversation but not the timing.

Every section gives concrete code for both stacks:
- **LiveKit Agents (Python)** with `livekit-plugins-google`.
- **A raw WebSocket bridge** (carrier media stream to google-genai Live).

Versions:
- LiveKit code is written against **livekit-agents 1.7.1**, the production version, with API names checked in the installed source. Differences in 1.8.x (current is 1.8.8) are noted.
- Bridge code uses google-genai 2.21.0 names.
- The bridge snippets plug into the `CallBridge`/`Playout` skeleton in `references/07-websocket-media-streams.md`.

Model and session configuration is in `references/02-gemini-live-model-and-config.md`; prompt writing is in `references/09-prompting-voice-agents.md`. Measured numbers come from a production inbound consumer-care deployment (Gemini 3.8 Live on Vertex, a LiveKit Cloud agent in India, Plivo SIP trunk) and its replay harnesses.

## Contents

1. [Who owns what, and the timeline of a call](#1-who-owns-what-and-the-timeline-of-a-call)
2. [Greeting and pre-greet](#2-greeting-and-pre-greet)
3. [Echo during the greeting on PSTN (the phantom-reply problem)](#3-echo-during-the-greeting-on-pstn-the-phantom-reply-problem)
4. [Interruptions and barge-in, generally](#4-interruptions-and-barge-in-generally)
5. [Silence handling and maximum call duration](#5-silence-handling-and-maximum-call-duration)
6. [Hang-up](#6-hang-up)
7. [Tool and function calling](#7-tool-and-function-calling)
8. [VAD and turn-taking](#8-vad-and-turn-taking)
9. [Multilingual callers](#9-multilingual-callers)
10. [Voice, accent and tone](#10-voice-accent-and-tone)
11. [Background audio](#11-background-audio)
12. [Transfer to a human, and DTMF](#12-transfer-to-a-human-and-dtmf)
13. [Other mechanics and configuration: caller number, consent line, prewarm, compression, resumption, failover, transcription](#13-other-mechanics-and-configuration-caller-number-consent-line-prewarm-compression-resumption-failover-transcription)
14. [Pre-launch checklist](#14-pre-launch-checklist)

---

## 1. Who owns what, and the timeline of a call

**Principle:** the model owns the conversation; code owns destructive actions and timing. No keyword or regex decides anything conversational. Infrastructure interlocks are allowed, for example "never hang up on a caller who is talking".

| Concern | Owner on Gemini Live | Why |
|---|---|---|
| End of the caller's turn, barge-in | **Gemini server VAD** | Through LiveKit, client-side turn detection is broken on 3.8 (section 8) |
| Speaking first | **Your code** | The model never speaks without input (proactive audio is always on) |
| Silence check-ins, auto hang-up | **Your code** | The model stays silent forever on a silent line |
| Hanging up the phone leg | **Your code**, triggered by a model tool call | The model can only ask for it |
| Echo protection during the greeting | **Your code** | No acoustic echo cancellation (AEC) for SIP or carrier streams |
| What to say, language, tone | Model, steered by the prompt | |
| Business record of the call | Post-call extraction (section 7.8) | Output transcripts are lossy |

```text
t=0     INVITE / WS "start"        ring until the agent publishes audio (SIP) / stream opens (WS)
t≈0.0   job dispatched             LiveKit: entrypoint; bridge: WS accepted
        ├─ open Gemini session      1.3-3.0 s (region, prompt size, credentials)
        └─ play CACHED greeting     starts at once; caller audio HELD (section 3)
t≈5-8   greeting ends              +0.4 s echo tail; release or discard held audio; push greeting into context
        caller speaks → server VAD end → first audio        median ≈1.6 s
        ... turns, tools, silence ladder (20/40/60 s), max duration cap (900 s)
        model says goodbye → calls end_call → playout drains → 4 s grace (cancel if caller speaks)
        → delete room (SIP BYE) / provider hang-up API
after   post-call: extraction → CRM → QA → storage (caller already gone)
```

---

## 2. Greeting and pre-greet

### 2.1 Why pre-render the greeting

- A Gemini session takes **1.3-3.0 s to connect**. It is about 1.46-1.6 s to `us` with a small prompt (2.1-2.3 s to `us-central1`), and 2.67-3.05 s with a 21k-token prompt (2.2 s with a tiny prompt in that same probe set).
- A generated greeting then needs about **0.9 s more** to its first audio.
- **Measured without a cache:** connect plus greeting took about 3.9-4.0 s of dead air after the call was answered.
- The fix: render the greeting **once per worker process, in the same Live voice**, then play the cached PCM the instant the call starts, while Gemini connects in parallel.

### 2.2 Render at prewarm: one warm-up session that doubles as a health probe

```python
# warmup.py  (google-genai 2.21.0)
from google import genai
from google.genai import types

RENDER_INSTRUCTION = (
    "You are the voice of a customer-care phone line in <city>, <country>. SPEAK <LOCALE> ENGLISH WITH A "
    "CLEAR, NATURAL <LOCALE> ACCENT, never American or British. When you are given a line, say exactly that "
    "line in a calm, steady, professional tone at a relaxed pace, and nothing else.")

async def render_line(project: str, location: str, voice: str, text: str,
                      language_hint: str | None = None) -> tuple[list[bytes], str]:
    client = genai.Client(vertexai=True, project=project, location=location)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=RENDER_INSTRUCTION,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)),
            language_code=language_hint),                       # the SAME hint the call will use
        output_audio_transcription=types.AudioTranscriptionConfig())
    chunks, transcript = [], ""
    async with client.aio.live.connect(model="gemini-3.8-live", config=config) as live:
        await live.send_realtime_input(text=f"Say exactly this line: {text}")
        async for msg in live.receive():                        # receive() ends at turn_complete
            sc = msg.server_content
            if sc is None:
                continue
            for part in (sc.model_turn.parts if sc.model_turn else None) or []:
                if part.inline_data and part.inline_data.data:
                    chunks.append(part.inline_data.data)        # 24 kHz PCM16 mono
            if sc.output_transcription and sc.output_transcription.text:
                transcript += sc.output_transcription.text
            if sc.turn_complete:
                break
    return chunks, transcript

def says(transcript: str, expected: str) -> bool:
    """Live sometimes paraphrases or adds a preamble ('Sure, here it is'), which would then play on EVERY call."""
    norm = lambda s: [w.strip(".,!?'\"").lower() for w in s.split()]
    heard, want = norm(transcript), norm(expected)
    covered = sum(1 for w in want if w in heard) / max(len(want), 1)
    extra = len([w for w in heard if w not in want])
    return covered >= 0.70 and extra <= 3

async def warm_live(settings, greeting_text: str) -> dict:
    """Probe the primary region, then the fallback; whichever renders becomes this process's Live location."""
    for location in (settings.live_location, settings.live_fallback_location):
        try:
            chunks, transcript = await asyncio.wait_for(
                render_line(settings.project, location, settings.voice, greeting_text, settings.language_hint), 30)
        except Exception as exc:                                # 1008 wrong region, auth, timeout...
            log.warning("live warmup failed at %s: %s", location, exc)
            continue
        if chunks and says(transcript, greeting_text):
            return {"ok": True, "location": location,
                    "greeting": {"chunks": chunks, "sample_rate": 24000, "num_channels": 1, "text": greeting_text}}
        return {"ok": True, "location": location, "greeting": None}   # reachable, render rejected: no cache
    return {"ok": False, "location": None, "greeting": None}          # Live down: use the fallback pipeline
```

- **Verify the render before caching it.** Without the accent instruction, 1 of 4 renders came out American.
- If the probe fails in every region, send this process's calls to a fallback pipeline (STT, then a text LLM, then TTS) rather than greeting and then going silent.
- Log one health line per process, for example `live warmup: ok=True location=us greeting_cached=True`. Grep for it after every deploy.

### 2.3 LiveKit: prewarm without breaking the 10 s process budget

`initialize_process_timeout` is a **hard 10 s**. A `setup_fnc` that runs past it is killed by the supervisor and respawned forever, and a `try/except` doesn't help because the kill is external. Network work therefore goes on a daemon thread, and the cache is published only when it is complete:

```python
from livekit.agents import AgentServer, JobProcess
from livekit.plugins import silero

LIVE_WARMUP_KEY = "live_warmup"

def prewarm(proc: JobProcess) -> None:
    settings = Settings.from_env()
    proc.userdata["vad"] = silero.VAD.load(              # local VAD: user_state, latency, barge-in (section 8)
        min_speech_duration=0.1, min_silence_duration=0.25, prefix_padding_duration=0.5,
        activation_threshold=0.5, sample_rate=16000, force_cpu=True)
    threading.Thread(target=_warm, args=(proc, settings), name="warm", daemon=True).start()

def _warm(proc: JobProcess, settings) -> None:
    warmup = asyncio.run(warm_live(settings, settings.greeting_text))
    proc.userdata[LIVE_WARMUP_KEY] = warmup              # published only when complete: never a truncated greeting

server = AgentServer(setup_fnc=prewarm)                  # 1.8.x: same; WorkerOptions(prewarm_fnc=) is legacy
```

- **Cold container, measured:** 11.2 s from container start to worker registration, of which 7.4 s was network warm-up inside `setup_fnc`. The job was dispatched 30 ms after registration, and the caller had already hung up. Moving the work to a thread fixed it.
- `JobProcess.userdata` outlives one call. Key any per-call stash by job id and pop it on read.
- Scale-to-zero (LiveKit Cloud Build plan) adds 10-20 s. Callers reported about 10 rings. Keep production warm for a phone line: Ship/Scale plans keep production deployments warm, and `lk` has no min-replicas flag (`references/04-livekit-cloud.md` section 6). A self-hosted bridge sets min instances >= 1.

### 2.4 LiveKit: play the cached greeting while Gemini connects

`session.say(text)` **raises** on a Gemini realtime model with no TTS (`supports_say=False`). `session.say(text, audio=frames)` works: it plays your frames and adds `text` to the **local** chat context.

```python
from livekit import rtc

def greeting_frames(cached: dict):
    """Fresh rtc.AudioFrame objects per call from the cached bytes (never share frame objects between calls)."""
    sr, ch = int(cached["sample_rate"]), int(cached["num_channels"])
    async def _iter():
        for chunk in cached["chunks"]:
            samples = len(chunk) // (ch * 2)                 # PCM16
            if samples:
                yield rtc.AudioFrame(chunk, sr, ch, samples)
    return _iter()

async def play_greeting(session, agent, warmup, text: str, on_handle=None) -> bool:
    cached = (warmup or {}).get("greeting")
    if cached:
        handle = session.say(text, audio=greeting_frames(cached), add_to_chat_ctx=True, allow_interruptions=True)
    else:   # cold start or render rejected: let Gemini speak it (adds ~0.9 s after connect)
        handle = session.generate_reply(
            instructions=f"Greet the caller with exactly these words and nothing else: {text}")
    if on_handle:
        on_handle(handle)                                    # barge-in watcher (section 3)
    try:
        await asyncio.wait_for(handle.wait_for_playout(), timeout=20.0)   # NEVER unbounded (see below)
    except asyncio.TimeoutError:
        log.warning("greeting playout timed out")
    if cached and not handle.interrupted:
        await agent.update_chat_ctx(agent.chat_ctx)          # push the greeting into Gemini's context
    return bool(cached)
```

- **Push the greeting into Gemini's context, or Gemini doesn't know it greeted.** `say(audio=...)` only adds the text locally. `agent.update_chat_ctx(agent.chat_ctx)` diff-pushes the new assistant message (sent with `turn_complete=False`). Verified: a fact appended this way is recalled later.
- **Never push it if the greeting was interrupted.** Gemini's first reply is already under way, and a context push **cancels it**. The caller then talks over the greeting and gets no answer. Have the prompt say what the greeting was instead (the per-call section of the prompt).
- **`wait_for_playout()` never returns if there is no participant to play to.** A real call logged `participant=none`; the greeting blocked forever and the entrypoint was cancelled 35 s later. Bound it (20 s), and bound the wait for the participant link too (`ctx.wait_for_participant` inside `asyncio.wait_for(..., 8)`).
- **Cold start:** a call that arrives before prewarm finished (`warmup is None`) goes to realtime on the primary region with `generate_reply`. A failed probe (`ok=False`) sends it to the fallback pipeline.
- Emit `greeting_played{prerendered: bool, duration_ms}`, because nothing before `ctx.connect()` is visible in analytics otherwise. In production, the two slow-pickup calls were exactly the two with `prerendered=false`.

### 2.5 Raw bridge: the same pattern

```python
# inside CallBridge (references/07-websocket-media-streams.md), started as soon as the carrier WS is accepted
async def greet(self) -> None:
    cached = GREETING_CACHE.get(self.voice)              # rendered at process start with render_line()
    if cached:
        self.hold.start()                                # section 3.4: hold caller audio, watch for barge-in
        for chunk in cached["chunks"]:
            self.playout.feed(chunk)                     # 24k -> carrier rate, paced at real time
        self.playout.end_of_turn()
        await self.ws_send(self.adapter.mark("greeting-end"))   # carrier echoes it when played
        return
    # no cache: once the Gemini session is up, ask the model to speak first
    await self.session_ready.wait()
    await self.session.send_realtime_input(
        text=f"(The phone call has just connected. Greet the caller with exactly: {GREETING_TEXT})")

async def on_greeting_played(self) -> None:              # called on the "greeting-end" mark echo
    await self.hold.finish(tail_s=0.4)                   # section 3.4
    if not self.hold.barged_in:
        await self.session_ready.wait()
        await self.session.send_client_content(          # tell Gemini it greeted (model turn, no reply)
            turns=types.Content(role="model", parts=[types.Part(text=GREETING_TEXT)]),
            turn_complete=False)
```

- **Kick-off options when nothing is cached:** `send_realtime_input(text=...)` takes about 0.76 s to first audio, and `send_client_content(turns=user, turn_complete=True)` about 0.71 s. The model never speaks first on its own.
- **Connect early.** Open the Gemini session as soon as the carrier WS is accepted, in parallel with the carrier's `start` handshake. Better still, open it from the HTTP webhook that precedes the WS (Twilio voice webhook, Exotel dynamic URL), so the session is warm when audio arrives.
- **Where the mark comes from:** Twilio, Exotel and Telnyx echo a `mark`; Plivo uses `checkpoint`, answered by `playedStream`; Vonage uses `notify`. See `references/07-websocket-media-streams.md`.

### 2.6 What goes in the greeting

- **One or two short sentences:** who you are, a consent or recording line if required, and an open question.
- **If you offer languages,** do it as one clause ("You can speak to me in English, Hindi or Kannada"), never as a menu.
- **A fixed legal line** (recording consent, "this is an automated assistant") belongs in the **cached audio**, not in a model-generated turn. Live doesn't guarantee to say a quoted line verbatim; India-specific wording is in `references/12-india-telephony-compliance.md`.
- **Measured:** the greeting started 1.2-2.5 s after the recording start on every production call.

---

## 3. Echo during the greeting on PSTN (the phantom-reply problem)

### 3.1 The problem

PSTN lines carry the agent's own voice back on the caller channel: line or hybrid echo, plus speakerphone acoustic echo. Carriers and handsets cancel some of it (G.168), but **LiveKit applies no AEC to SIP participants**, and a WebSocket bridge has none at all.

During a 5-10 s greeting, Gemini's server VAD took the echo for the caller and **answered a phantom utterance over the greeting** ("I can help you with <product>..."), before the caller had said a word.
- Replaying real call recordings into Live reproduced it **24/24**.
- Dropping caller audio during the greeting fixed it (**0/57**), but then the caller could not interrupt the greeting and their first words were lost.

### 3.2 Why the obvious LiveKit switches don't help

| Switch | What it actually does with a Google realtime model |
|---|---|
| `allow_interruptions=False` on `say`/`generate_reply` | **Downgraded with a warning**, because server-side turn detection is on. At the `Agent` level it raises `ValueError`. Gemini still hears the echo and still barges in. In 1.7.1 the session-level flag is also deprecated in favour of `turn_handling=TurnHandlingOptions(...)`. |
| `discard_audio_if_uninterruptible` | Same: inert |
| `aec_warmup_duration` (default **3.0 s for inbound SIP**; LiveKit clears it early for outbound SIP) | While active, `push_audio` sends **silence** to the realtime session (not buffered). It covered only the first 3 s of a 5-10 s greeting, still let echo through after that, and **lost the caller's first words**. |
| `session.input.set_audio_enabled(False)` around the greeting | Frames are **dropped**, not buffered. No phantom reply, but the caller is unheard and can't interrupt. A valid simple option if your greeting is very short. |
| `noise_cancellation.BVCTelephony()` | Noise and background-voice suppression, **not echo cancellation**. It doesn't remove the agent's own voice. |

### 3.3 The design that worked: hold, don't drop (`CallerAudioHold`)

1. While the greeting plays, **hold** caller audio in a buffer with timestamps. Send nothing to Gemini.
2. Watch the **local Silero VAD**. If the caller speaks **continuously for 0.8 s**, treat it as a real barge-in:
   - interrupt the greeting;
   - **replay the held audio from 0.6 s before** the VAD's speech start, so the first word isn't clipped;
   - stream live from then on.
3. If no barge-in happens, wait for the greeting to finish, plus a **0.4 s echo tail**, then **discard** the held audio (it was only echo) and start streaming.
4. Set `aec_warmup_duration=None`; the hold replaces it and keeps the words.

**Threshold evidence (measured, Silero at the production settings in section 8.3):**
- Echo and office chatter produced speech bursts of **at most 0.70 s** on real recorded caller channels during the greeting window.
- A real sentence mixed in at 3.0 s was detected within **0.15 s**.
- An end-to-end test against Live (echo, mixed-in caller speech, production hold and replay timing) confirmed the caller's words were heard and answered.
- **The 0.8 s threshold is specific to this VAD and these settings.** If you use a different VAD (as in the bridge below), re-measure on your own recordings (`references/10-testing-and-evaluation.md`).

**Trade-off:** a caller who says only a short word over the greeting ("hello?") stays below 0.8 s. Their audio is discarded and they hear the end of the greeting. That was judged acceptable.

### 3.4 LiveKit implementation (1.7.1)

```python
import asyncio, time
from collections import deque
from livekit.agents import AgentSession
from livekit.plugins import google
from livekit.plugins.google.realtime.realtime_api import RealtimeSession   # 1.7.1 internal: pin the version

GREETING_ECHO_TAIL_SECONDS = 0.4
GREETING_BARGE_IN_SECONDS = 0.8
BARGE_IN_REPLAY_LEAD_SECONDS = 0.6

class CallerAudioHold:
    """Holds caller frames while the greeting plays; releases them with or without replay."""
    def __init__(self, *, keep_seconds: float = 15.0, clock=time.monotonic):
        self._keep, self._clock = keep_seconds, clock
        self._held: deque | None = None
        self._replay: list = []

    @property
    def holding(self) -> bool:
        return self._held is not None

    def hold(self) -> None:
        self._held, self._replay = deque(), []

    def release(self, *, replay_since: float | None = None) -> None:
        held, self._held = self._held, None
        if held and replay_since is not None:
            self._replay = [f for at, f in held if at >= replay_since]   # caller spoke over the greeting
        # else: discard; it was only echo

    def admit(self, frame) -> list:
        if self._held is not None:
            now = self._clock()
            self._held.append((now, frame))
            while self._held and now - self._held[0][0] > self._keep:
                self._held.popleft()
            return []
        if self._replay:
            frames, self._replay = [*self._replay, frame], []
            return frames
        return [frame]

class HoldingRealtimeSession(RealtimeSession):
    def push_audio(self, frame):                               # every caller frame passes through here
        for admitted in self._realtime_model.caller_audio.admit(frame):
            super().push_audio(admitted)

class HoldingRealtimeModel(google.realtime.RealtimeModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.caller_audio = CallerAudioHold()
    def session(self, *, turn_detection_disabled: bool = False):   # 1.7.1 signature; check on upgrade
        live = HoldingRealtimeSession(self)
        self._sessions.add(live)
        return live

class GreetingBargeIn:
    """Local-VAD watcher: 0.8 s of continuous caller speech during the greeting = real barge-in."""
    def __init__(self, session: AgentSession, hold: CallerAudioHold, clock=time.monotonic):
        self._session, self._hold, self._clock = session, hold, clock
        self._handle = None
        self._timer: asyncio.TimerHandle | None = None
        self._speech_started_at: float | None = None
        self._replay_since: float | None = None
        session.on("user_state_changed", self._on_user_state)

    def watch(self, handle) -> None:
        self._handle = handle

    def _on_user_state(self, ev) -> None:
        if ev.new_state == "speaking" and self._timer is None and self._hold.holding:
            self._speech_started_at = self._clock()
            self._timer = asyncio.get_running_loop().call_later(GREETING_BARGE_IN_SECONDS, self._barge_in)
        elif ev.old_state == "speaking" and self._timer is not None:
            self._timer.cancel()                               # burst shorter than 0.8 s: echo/chatter
            self._timer = None

    def _barge_in(self) -> None:
        self._timer = None
        self._replay_since = self._speech_started_at - BARGE_IN_REPLAY_LEAD_SECONDS
        self._hold.release(replay_since=self._replay_since)
        if self._handle is not None and not self._handle.done():
            self._handle.interrupt()

    def replay_since(self) -> float | None:
        return self._replay_since

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._session.off("user_state_changed", self._on_user_state)

async def greet_realtime(session, agent, warmup, text, hold: CallerAudioHold) -> None:
    hold.hold()
    barge_in = GreetingBargeIn(session, hold)
    try:
        await play_greeting(session, agent, warmup, text, on_handle=barge_in.watch)   # section 2.4
    finally:
        if hold.holding:                                       # no barge-in during playout
            await asyncio.sleep(GREETING_ECHO_TAIL_SECONDS)    # line keeps echoing briefly; keep listening
            barge_in.close()
            hold.release(replay_since=barge_in.replay_since())   # None -> discard
        else:
            barge_in.close()

# entrypoint
model = HoldingRealtimeModel(model="gemini-3.8-live", vertexai=True, project="<PROJECT>",
                             location=warmup_location, voice="Gacrux", **COMMON_LIVE_KWARGS)
session = AgentSession(llm=model, vad=ctx.proc.userdata["vad"], aec_warmup_duration=None)
await session.start(room=ctx.room, agent=agent, room_options=ROOM_OPTIONS)   # section 13
await greet_realtime(session, agent, ctx.proc.userdata.get(LIVE_WARMUP_KEY), GREETING_TEXT, model.caller_audio)
```

Notes:
- **The override touches plugin internals** (`RealtimeSession`, `_sessions`, `_realtime_model`), so pin the plugin version exactly and keep a unit test that asserts `push_audio` is still the path caller audio takes.
- **With `RealtimeModelFallbackAdapter`,** give every regional model the **same** `CallerAudioHold` instance (`secondary.caller_audio = primary.caller_audio`).
- **`aec_warmup_duration=None` must be passed explicitly.** Otherwise LiveKit sets 3.0 s for inbound SIP when the participant links (`_on_room_io_participant_linked`).

### 3.5 Raw bridge implementation (local VAD on the bridge)

The bridge has no Silero by default. This version uses `webrtcvad` (`pip install webrtcvad-wheels`) on 20 ms, 16 kHz frames, with a 250 ms hangover that mirrors Silero's `min_silence_duration`. **Re-measure the 0.8 s threshold** for this VAD on your recordings (**VERIFY**).

```python
import collections, webrtcvad

class BridgeGreetingHold:
    FRAME_BYTES = 640                       # 20 ms @ 16 kHz PCM16 (after inbound resampling)

    def __init__(self, bridge, *, sustain_s=0.8, lead_s=0.6, hangover_s=0.25, keep_s=15.0, aggressiveness=2):
        self.bridge, self.vad = bridge, webrtcvad.Vad(aggressiveness)
        self.sustain_s, self.lead_s, self.hangover_s, self.keep_s = sustain_s, lead_s, hangover_s, keep_s
        self.held: collections.deque | None = None
        self.speech_start: float | None = None
        self.last_speech: float | None = None
        self.barged_in = False

    @property
    def holding(self) -> bool:
        return self.held is not None

    def start(self) -> None:
        self.held, self.speech_start, self.last_speech, self.barged_in = collections.deque(), None, None, False

    def admit(self, frame16k: bytes, now: float) -> list[bytes]:
        """Called for every 20 ms inbound frame. Returns frames to send to Gemini now."""
        if self.held is None:
            return [frame16k]
        self.held.append((now, frame16k))
        while self.held and now - self.held[0][0] > self.keep_s:
            self.held.popleft()
        if self.vad.is_speech(frame16k, 16000):
            self.last_speech = now
            if self.speech_start is None:
                self.speech_start = now
            elif now - self.speech_start >= self.sustain_s:
                return self._barge_in()
        elif self.last_speech is not None and now - self.last_speech > self.hangover_s:
            self.speech_start = self.last_speech = None          # burst ended: echo or chatter
        return []

    def _barge_in(self) -> list[bytes]:
        since = self.speech_start - self.lead_s
        frames = [f for at, f in self.held if at >= since]
        self.held, self.barged_in = None, True
        asyncio.create_task(self.bridge.playout.flush())       # stop the greeting: local queue + carrier clear
        return frames                                          # replayed to Gemini in a burst, then live audio

    async def finish(self, tail_s: float) -> None:
        """Greeting played out (mark echoed). Keep watching through the echo tail, then discard."""
        if self.held is None:
            return
        await asyncio.sleep(tail_s)
        self.held = None                                       # discard: it was only echo
```

Wire it into `_push_mic`: resample to 16 kHz, cut 20 ms frames, `for f in self.hold.admit(frame, loop.time()): mic_q.put_nowait(f)`.

**Simpler fallback for the bridge:** send **silence** to Gemini while the greeting plays, plus about 300 ms. This is the gemini-live-api-examples#53 workaround: it keeps the stream continuous and avoids the phantom reply, but the caller can't interrupt the greeting.

---

## 4. Interruptions and barge-in, generally

### 4.1 What Gemini does

- **Defaults:** `activity_handling=START_OF_ACTIVITY_INTERRUPTS` and `START_SENSITIVITY_HIGH`. Caller speech during model speech stops generation.
- **Event order:** the server sends `server_content.interrupted=True` (measured **about 0.5-0.58 s after the caller's voice onset**), then `turn_complete` about 0.12 s later. There is no `generation_complete` on an interrupted turn.
- **Any NON_BLOCKING tool calls in flight are cancelled:** their ids arrive in `tool_call_cancellation`.
- **No truncation.** The server keeps the **whole** reply in its context, including the part the caller never heard. The LiveKit plugin reports `message_truncation=False`, and `truncate()` is a no-op. LiveKit's local history keeps only the text that was played. The model may later assume the caller heard something they didn't; keep turns short so less is lost.

### 4.2 Flushing playback

**LiveKit:** handled for you. On `interrupted`, the plugin ends the generation, RoomIO clears the audio source, and the SIP bridge stops. Nothing to code.

**Bridge:** on `interrupted`, do all four, at once:

```python
if sc.interrupted:
    await self.playout.flush()     # 1) drop the local queue and bump the epoch so a sleeping sender discards
                                   # 2) send the provider's clear  3) reset the pacer clock
                                   # 4) reset outbound resampler state (or keep it; it is a filter, either is fine)
```

| Provider | Clear message | Ack | Caveat |
|---|---|---|---|
| Twilio | `{"event":"clear","streamSid":...}` | pending `mark`s echoed back | |
| Exotel | `{"event":"clear","stream_sid":...}` | | **The chunk already playing finishes**, so keep chunks at 3,200 B |
| Plivo | `{"event":"clearAudio","streamId":...}` | `clearedAudio` | |
| Telnyx | `{"event":"clear"}` | queued marks returned | |
| Vonage | `{"action":"clear"}` (text frame) | `websocket:cleared` | |

**The talk-over tail is mostly your own buffering.** Carriers play whatever you have already sent: Twilio buffers everything, and Plivo and Vonage hold about 60 s. If you burst Gemini's faster-than-real-time audio into the carrier, barge-in depends entirely on `clear`, and on Exotel the playing chunk still finishes. **Pace outbound audio at real time with a 100-200 ms lead.** Use `mark` echoes to know what was actually heard. Details are in `references/08-audio-formats-and-conversion.md`.

### 4.3 Sensitivity trade-offs

| Symptom | Lever | Cost |
|---|---|---|
| The agent cuts itself off on "haan", "ok" or coughs | `START_SENSITIVITY_LOW`, `prefix_padding_ms` 100-200. Prompt: "if the caller says 'hmm', 'okay', 'haan' while you speak, keep going". Proactive audio already ignores some of this. | Slower real barge-in (LOW measured about 0.57 s) |
| The agent interrupts itself mid-call on line echo | Same as above, plus `BVCTelephony`. Keep outbound levels moderate and never add gain. | |
| Callers cut off while pausing mid-address | Do **not** shorten `silence_duration_ms`. Consider `END_SENSITIVITY_LOW` / a longer silence window. | Slower replies (LOW plus 1,500 ms measured 2.43 s) |
| A critical disclosure must not be interrupted | `activity_handling=NO_INTERRUPTION`, or play it as cached audio with caller audio held | Needs a reconnect to toggle; the model talks over callers |

LiveKit's own interruption controls are **inert** on a Google realtime model: `min_interruption_duration`, `min_interruption_words`, `false_interruption_timeout`, `resume_false_interruption`, adaptive interruption and `backchannel_boundary`. Gemini's server VAD alone decides. For contrast, the cascaded pipeline needed `min_duration=0.6`, `min_words=2` and `backchannel_boundary=(0.0, 1.0)`; before that, 17-25% of agent turns were cut off by backchannels.

---

## 5. Silence handling and maximum call duration

Gemini **never speaks to a silent caller**: it stayed silent for 12 s in a probe and would stay silent forever. You need a monitor.

### 5.1 Design

| Stage | After silence of | Action |
|---|---|---|
| Check-in | 20 s | Gentle "are you still there? take your time", in the call's language |
| Final warning | 40 s | "I'll end the call shortly if I can't hear you", **no question** |
| Close | 60 s | Short goodbye, then hang up |
| Max duration | 900 s total | Short goodbye, then hang up (must exceed the silence close) |

Rules learned the hard way:
- **Prompts are instructions, not fixed lines.** A fixed English check-in was spoken to silent Hindi callers. Use `generate_reply(instructions=...)`, for example: *"The caller has gone quiet. In the language you have been speaking with them, check gently that they are still there and tell them to take their time. One short sentence, and do not ask them for any detail."*
- **Every system-driven prompt says it is not the end:** *"This is not the end of the call: do not give the reference number, do not ask whether there is anything else, and do not call end_call."* For the final stages: *"The phone system ends the call itself straight after this, so ask no question."* Without that, the system prompt's closing rules leaked in, and a check-in hung up two stages early.
- **Restarting the clock on realtime.** No user turn is ever committed on the realtime path (`on_user_turn_completed` never fires). Start the clock when the agent returns to `listening`, **only if the caller spoke since the last reply**. After a check-in, leave the clock and the ladder stage alone; otherwise the check-in repeats forever and never reaches the goodbye.
- **The `away` state:** LiveKit flips `user_state` to `"away"` after `user_away_timeout` (default 15 s). A monitor gated only on `"listening"` stopped at 15 s, before the 20 s check-in was due, so **no silent caller was ever prompted**. Accept `"away"` as "the caller has the floor".
- **A caller who coughs after the goodbye** must put the call back to LISTENING (section 6). Otherwise the call sits open until the max-duration cap.

### 5.2 LiveKit implementation

```python
CHECK_IN_S, FINAL_WARNING_S, SILENCE_CLOSE_S, MAX_CALL_S = 20.0, 40.0, 60.0, 900.0

class SilenceClock:
    def __init__(self, now: float):
        self.started_at, self.quiet_since, self.stage = now, None, 0
        self.caller_spoke_since_reply = True

    def caller_spoke(self) -> None:          # from local VAD edges and final input transcripts
        self.caller_spoke_since_reply, self.quiet_since, self.stage = True, None, 0

    def floor_returned(self, now: float) -> None:     # agent_state -> "listening"
        if self.caller_spoke_since_reply or self.quiet_since is None:
            self.quiet_since, self.caller_spoke_since_reply = now, False

    def due(self, now: float) -> str | None:
        if now - self.started_at >= MAX_CALL_S:
            return "max_duration"
        if self.quiet_since is None:
            return None
        quiet = now - self.quiet_since
        for stage, (limit, action) in enumerate(
                [(CHECK_IN_S, "check_in"), (FINAL_WARNING_S, "final_warning"), (SILENCE_CLOSE_S, "close")], 1):
            if self.stage < stage and quiet >= limit:
                self.stage = stage
                return action
        return None

INSTRUCTIONS = {
    "check_in": ("The caller has gone quiet. In the language you have been speaking with them, check gently "
                 "that they are still there and tell them to take their time. One short sentence; ask for no "
                 "detail. This is not the end of the call: do not give the reference number, do not ask whether "
                 "there is anything else, and do not call end_call."),
    "final_warning": ("The caller is still silent. In the language of the call, say in one short sentence that "
                      "you will end the call soon if you cannot hear them. Ask no question and do not call end_call."),
    "close": ("The caller has not answered. In the language of the call, say a short polite goodbye. The phone "
              "system ends the call itself straight after this, so ask no question and do not call end_call."),
    "max_duration": ("The call has reached its time limit. In the language of the call, briefly say you have to "
                     "end the call now and say goodbye. The phone system ends the call itself straight after "
                     "this, so ask no question and do not call end_call."),
}

async def silence_monitor(session, clock: SilenceClock, closer) -> None:
    while not closer.closing:
        await asyncio.sleep(0.5)
        now = time.monotonic()
        if not (session.agent_state == "listening" and session.user_state in {"listening", "away"}
                and not closer.close_pending):
            if clock.due(now) == "max_duration":       # the cap applies even mid-conversation
                await closer.say_and_close(session, INSTRUCTIONS["max_duration"], reason="max_duration")
                return
            continue
        action = clock.due(now)
        if action in ("close", "max_duration"):
            await closer.say_and_close(session, INSTRUCTIONS[action], reason=action)
            return
        if action:
            session.generate_reply(instructions=INSTRUCTIONS[action], allow_interruptions=True)

# wiring
clock = SilenceClock(time.monotonic())
session.on("user_state_changed", lambda ev: clock.caller_spoke() if ev.new_state == "speaking" else None)
session.on("user_input_transcribed", lambda ev: clock.caller_spoke() if ev.is_final else None)
session.on("agent_state_changed",
           lambda ev: clock.floor_returned(time.monotonic()) if ev.new_state == "listening" else None)
```

`say_and_close` does `speech = session.generate_reply(instructions=...)`, awaits `speech.wait_for_playout()` with a timeout, then `session.shutdown(drain=True)`. With `delete_room_on_close=True`, that drops the SIP leg (section 6). `allow_interruptions=False` on that final goodbye is ignored on realtime, which is acceptable.

Skip the monitor in LiveKit text simulations (`ctx.simulation_context().simulation_mode == SIMULATION_MODE_TEXT`; **VERIFY** the name on 1.8.x).

### 5.3 Raw bridge

Same ladder. "Caller spoke" comes from the bridge's local VAD (section 3.5) or from `voice_activity` ACTIVITY_START and `input_transcription` messages. "Floor returned" is when the playout goes idle after a reply (the end-of-turn `mark` echo). Nudge with a user-role note:

```python
await self.session.send_client_content(
    turns=types.Content(role="user", parts=[types.Part(text=(
        "(System note, not the caller: the caller has been silent for 20 seconds. In the language of the call, "
        "check gently that they are still there. One short sentence. This is not the end of the call; "
        "do not call end_call.)"))]),
    turn_complete=True)
```

**Never pause the stream to Gemini during silence.** Keep forwarding the carrier's silence frames: silence costs about nothing, and server VAD needs continuity.

---

## 6. Hang-up

### 6.1 Why a tool, not a text marker

On Live, an end marker written into the reply appears in the output **transcript**, which is lossy and can diverge from the audio. With marker instructions left in the prompt, the model ignored the tool and wrote the marker. With a tool only, 3/3 runs spoke the goodbye and **then** called `end_call`, after the audio. **Remove all marker text from a realtime prompt.**

### 6.2 LiveKit: `end_call` must return `None`

```python
from livekit.agents import Agent, RunContext, function_tool

class CareAgent(Agent):
    def __init__(self, *, instructions: str, closer: "CallCloser"):
        super().__init__(instructions=instructions)
        self.closer = closer

    @function_tool(name="end_call", description=(
        "End the phone call. Call this only after you have said goodbye: either the caller told you there is "
        "nothing else, or they said they are finished. Never call it while a question of yours or of the "
        "caller's is still open. Say nothing after calling it."))
    async def end_call(self, context: RunContext) -> None:
        await context.wait_for_playout()        # grace is measured from the END of the farewell
        self.closer.close_after_response = True
        # Return NOTHING. A string result makes LiveKit hold agent_state at "thinking" waiting for a tool
        # reply. Gemini usually stays silent after end_call, so the "listening" edge that arms the close
        # never came and the line NEVER hung up (production blocker).
```

Why not LiveKit's `EndCallTool` (`livekit.agents.beta.tools`)? It waits for and plays the tool-reply speech (Gemini said "I have ended the call"), then shuts down **with no grace window**. It is fine for demos. For production, use the guarded close below.

### 6.3 LiveKit: the guarded close (grace window, cancel if the caller speaks)

```python
CLOSE_GRACE_SECONDS = 4.0          # was 2.5: a caller thinking in Hindi was cut 2.75 s after a question
SPEECH_LOOKBACK_SECONDS = 1.0

class CallCloser:
    def __init__(self, session):
        self.session = session
        self.close_after_response = False   # set by end_call
        self.close_pending = False
        self.closing = False
        self.generation = 0
        self.last_caller_speech_at: float | None = None
        self._task: asyncio.Task | None = None
        session.on("agent_state_changed", self._on_agent_state)
        session.on("user_state_changed", self._on_user_state)
        session.on("speech_created", self._on_speech_created)

    # caller speech: stamped on BOTH edges, from the local VAD (never from transcripts: they arrive late)
    def _on_user_state(self, ev) -> None:
        if "speaking" in (ev.old_state, ev.new_state):
            self.last_caller_speech_at = time.monotonic()
        if ev.new_state == "speaking" and self.close_pending:
            self.cancel()                   # caller said something after goodbye: back to LISTENING

    def _on_agent_state(self, ev) -> None:
        if ev.new_state != "listening" or not self.close_after_response:
            return
        self.close_after_response, self.close_pending = False, True
        self.generation += 1
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._close_after_grace(time.monotonic(), self.generation))

    async def _close_after_grace(self, armed_at: float, generation: int) -> None:
        await asyncio.sleep(CLOSE_GRACE_SECONDS)
        if generation != self.generation or not self.close_pending:
            return                          # a stale task can neither commit nor cancel a later farewell
        # Decide at COMMIT time: a caller already mid-sentence produces no new "speaking" edge.
        talking = self.session.user_state == "speaking"
        recent = (self.last_caller_speech_at is not None
                  and self.last_caller_speech_at >= armed_at - SPEECH_LOOKBACK_SECONDS)
        if talking or recent:
            self.cancel()
            return
        self.closing = True
        self.session.shutdown(drain=True)   # goodbye never clipped; delete_room_on_close drops the SIP leg

    def cancel(self) -> None:
        self.close_pending = False
        self.generation += 1

    # Vertex answers even an empty end_call result (SILENT scheduling is dropped): Gemini said
    # "I have ended the call" or read out a "<no speech>" placeholder. Kill any speech while a close is armed.
    def _on_speech_created(self, ev) -> None:
        if self.close_after_response or self.close_pending:
            ev.speech_handle.interrupt(force=True)

    async def say_and_close(self, session, instructions: str, *, reason: str) -> None:
        self.closing = True
        speech = session.generate_reply(instructions=instructions)
        try:
            await asyncio.wait_for(speech.wait_for_playout(), timeout=20)
        finally:
            session.shutdown(drain=True)
```

A caller who speaks cancels the pending close **before** their reply would be suppressed, so a real answer is never killed.

### 6.4 LiveKit: actually dropping the SIP leg

- `AgentSession.shutdown()` **does not touch the telephony leg**. The SIP participant keeps a silent open line ("If the agent session ends but the room is not deleted, the user continues to hear silence").
- Use `room_io.RoomOptions(delete_room_on_close=True, close_on_disconnect=True)`. The room is deleted on the session `close` event, **after** the drain, so the caller is disconnected before `on_session_end` runs.
- **Don't** call `ctx.delete_room()` yourself right after `shutdown()`. Shutdown is non-blocking, so "after" is really before the drain, which clips the goodbye. A fire-and-forget delete also lands in pending tasks that teardown cancels.
- When the **caller** hangs up first, the SIP BYE surfaces as the participant disconnecting (`CLIENT_INITIATED`), and `close_on_disconnect=True` closes the session.
- With `delete_room_on_close=True`, the SIP participant is gone by the time `on_session_end` runs. **Stash the caller number during the call** in `ctx.proc.userdata[f"caller_number:{job_id}"]` and pop it in the hook.
- Warm transfer needs `delete_room_on_close=False` (section 12).

### 6.5 Raw bridge: hang up after the playback mark

```python
async def run_tool(self, fc):                       # end_call branch
    if fc.name == "end_call":
        self.hangup_requested = True
        result = {"status": "ok", "message": "The call is ending. Do not say anything more."}
        await self.session.send_tool_response(function_responses=[
            types.FunctionResponse(id=fc.id, name=fc.name, response=result)])
        await self.ws_send(self.adapter.mark("goodbye-end"))   # after the goodbye audio already queued

async def on_mark(self, name: str):
    if name == "goodbye-end" and self.hangup_requested:
        await asyncio.sleep(CLOSE_GRACE_SECONDS)                # cancel if the caller speaks (local VAD/ACTIVITY_START)
        if not self.caller_spoke_since(self.hangup_armed_at - 1.0):
            await self.adapter.hang_up()
```

Also drop (do not play) any Gemini audio that arrives after `end_call`, or flush it. Measured: with the "say nothing more" result, the model was silent in 2/2 runs, but said "I have ended the call" in another probe.

| Provider | Hang up |
|---|---|
| Twilio Media Streams | REST `POST .../Calls/{CallSid}.json` with `Status=completed`, or close the WS when no TwiML follows `<Connect>` |
| Exotel Voicebot | Close the WS. The flow then runs the next applet (Hangup); with Connect Voice AI the call ends |
| Plivo | `DELETE https://api.plivo.com/v1/Account/{auth_id}/Call/{call_uuid}/`. **Never omit `call_uuid`: that disconnects every call on the account.** |
| Telnyx | `POST /v2/calls/{call_control_id}/actions/hangup` |
| Vonage | `PUT /v1/calls/{uuid}` `{"action":"hangup"}` (**VERIFY**) |

Details are in `references/07-websocket-media-streams.md`.

---

## 7. Tool and function calling

### 7.1 Declarations

- **All tools are declared at connect.** Changing them mid-session costs a reconnect (about 2.2 s of dead air) in LiveKit (`mutable_tools=False`). Register tools before `session.start`, including conditional ones such as `transfer_to_human`, which should be added only when a transfer number exists.
- **Keep schemas flat:** `str`, `int`, `bool`, enums. Free-form `dict` parameters are stripped to `{"type":"OBJECT"}` and filled with `{}`.
- **Keep descriptions to one sentence, plus an explicit invocation condition** ("Call only after the caller has given X and Y. Call once."). In one report, about 1.7 KB of unused declarations made 3.8 mispronounce a name in 5-10 of 20 sessions (https://github.com/googleapis/python-genai/issues/3120).
- **Keep the tool count small.** Google: "The model performs best on tasks with single function calls."
- `google_search` combined with function declarations was accepted on Vertex 3.8 and the function still got called, even though the docs say they can't be combined. Don't rely on it.
- A bad tool schema in plugin 1.7.1 raised outside the `try`, leaving the agent silent with a single log line (#7371, fixed in 1.8.3). Test every schema against the live API.

### 7.2 Blocking vs non-blocking on 3.8

| | BLOCKING | NON_BLOCKING (3.8 default) |
|---|---|---|
| Model while the tool runs | Waits silently | Keeps talking (usually says a filler itself) |
| Caller speaks while it runs | Server cancels the pending call (`tool_call_cancellation`) | Pending calls are discarded on barge-in, ids in `tool_call_cancellation` |
| Result delivery | Model continues from the result | Spoken according to `FunctionResponse.scheduling` |
| Set via | `FunctionDeclaration(behavior=types.Behavior.BLOCKING)`; LiveKit `RealtimeModel(tool_behavior=...)` (session-wide only) | default |

- **Plugin versions:** 1.7.1 sends no `behavior`, so 3.8 treats tools as NON_BLOCKING (livekit/agents #7302). 1.8.3+ defaults `tool_behavior=NON_BLOCKING` for 3.8.
- **Conflict (VERIFY):** Pipecat docs say Vertex Live doesn't support NON_BLOCKING tools; Vertex docs say it is the default.
- **Rule:** don't make hang-up or transfer depend on blocking semantics. Gate them in your own code, as in section 6.

### 7.3 Scheduling (NON_BLOCKING results only)

`FunctionResponse(id=..., name=..., response=..., scheduling=types.FunctionResponseScheduling.X)`:
- `SILENT` adds the result to context with no speech.
- `WHEN_IDLE` (the default) speaks at the next pause.
- `INTERRUPT` speaks now. On 3.8, INTERRUPT while the **caller** is speaking is downgraded to WHEN_IDLE.

Caveats:
- **On Vertex, scheduling is ignored.** The LiveKit plugin warns and drops `tool_response_scheduling`. A result meant to be silent **is answered anyway**: after `end_call`, the model said "I have ended the call". Suppress that speech in code (section 6.3), and add a prompt line ("say nothing after calling end_call").
- `scheduling` is a field of `FunctionResponse`, **not** a key inside `response` (the Gemini API tools sample gets this wrong).
- `will_continue` (streamed results) is not supported on Vertex.

### 7.4 Handler shape: sync vs async, results

- **LiveKit:** write `async def` tools. A sync tool blocks the event loop and with it the audio. Use `await asyncio.to_thread(...)` for blocking SDKs. The plugin has `auto_tool_reply_generation=True`, so after your tool returns, Gemini continues on its own.
- **Results** (Vertex guide): send the exact call `id`, and **never return `{}` or `None` to Gemini**. Return `{"status": "ok" | "no_results" | "invalid_argument" | "error", "retryable": bool, "message": "<what to tell the caller>"}`, plus `valid_options` or `retry_hint` where useful. Empty results make 3.8 "sweep" 2-5 back-to-back calls with varied arguments.
- **The one exception is `end_call` in LiveKit,** which returns Python `None` because of the agent-state bug in section 6.2. The plugin still sends Gemini a response.
- **Add a retry cap to the system prompt:** *"When a tool returns no results, tell the caller before calling it again. If a call fails because of its arguments, ask the caller to clarify instead of retrying with variations. Never make more than two function calls in a row without saying something to the caller. Never say something is done unless the tool returned status ok."*
- **Duplicates:** the model may emit a duplicate call while the first is pending, and you may ignore it. In a bridge, key by id or name. In LiveKit 1.7.1, use `@function_tool(on_duplicate="reject")`.

### 7.5 Long-running tools: filler speech and a thinking sound

- **The model's own filler:** on NON_BLOCKING it usually says "let me check" by itself. On BLOCKING, or for anything over about 2 s, add a prompt line: "Before calling a lookup, tell the caller in a few words that you are checking."
- **A pre-recorded or forced filler (bridge):** right after receiving the call, send the model a line to say, such as `send_client_content(user: "(say: 'One moment while I check that.')")`, as Google's async-calling page suggests. Or play a cached clip directly to the carrier.
- **LiveKit `RunContext.with_filler(source, delay=, interval=, max_steps=)`** exists in 1.7.1. A **string** source is spoken via `session.say`, which **fails on Gemini without a TTS**. Pass a callable that returns `context.session.generate_reply(instructions=...)`, or cached frames via `say(text, audio=...)` (**VERIFY** on your version):

```python
@function_tool()
async def lookup_order(self, context: RunContext, order_id: str) -> dict:
    """Get delivery status for an order id. Call only after the caller has read out the order id."""
    async with context.with_filler(
            lambda step: context.session.say("One moment, please.", audio=cached_frames("one_moment")),
            delay=1.5, max_steps=1):
        return await asyncio.to_thread(crm.order_status, order_id)
```

- **Thinking sound:** `BackgroundAudioPlayer(thinking_sound=...)` plays while `agent_state == "thinking"`. On realtime that state only occurs **during tool execution**, so it never covers the normal end-of-turn gap (section 11).

### 7.6 Cancellation on barge-in

- **Gemini:** pending calls are discarded and their ids arrive in `tool_call_cancellation`.
- **LiveKit plugin 1.7.1 only logs this** ("server cancelled tool calls"). Your tool keeps running, and its result is sent anyway. Make side-effecting tools idempotent (an external id per call), or check a flag before committing.
- **Bridge:** keep `tool_tasks[id]` and cancel the task on `tool_call_cancellation`. If the result arrives after a cancellation or a reconnect, sending it may error; tolerate that.
- `ToolFlag.CANCELLABLE` in LiveKit is different: it exposes extra `cancel_task` / `get_running_tasks` tools to the **model**. That adds declarations, so avoid it on Gemini.

### 7.7 Tools can't change mid-session; the prompt can't either

Build the complete toolset and the final prompt before connecting (`references/02-gemini-live-model-and-config.md` section 11). For per-stage tool sets, Google suggests **prompt chaining**: a `get_next_prompt` tool that returns the next stage's instructions as its result. This was untested here.

### 7.8 Why data capture moved out of tools into post-call extraction

Early designs had the model fill a `create_ticket`/`save_details` tool during the call. Problems seen:
- A tool to fetch the reference number failed with `MALFORMED_FUNCTION_CALL`. The reference was then derived from the session id and put in the prompt.
- Every tool adds declaration weight (pronunciation and latency), and barge-in can cancel calls mid-flight.
- The model "invents actions without tools" ("I have registered your complaint") whether or not a tool exists. Commitments have to be gated in the prompt anyway (`references/09-prompting-voice-agents.md`).
- Data was spread across turns; callers corrected themselves later; and a tool call made too early captured wrong values.

**What worked:** after hang-up, run a strong text model over the full transcript with a JSON schema. That gives route, category, fields and `evidence_turn_ids`, with validation and a deterministic policy pass that may only escalate. Latency costs nobody after hang-up. Pipeline details are in `references/11-deployment-ops-and-integrations.md`.

**The split:** keep in-call tools for things that must happen **during** the call (a lookup whose answer the caller needs, a transfer, ending the call). Capture the record of the call after it. Where a record must be exact in real time (a booking), a tool with flat arguments plus a read-back is still right.

---

## 8. VAD and turn-taking

### 8.1 Two VADs, two jobs

| | Gemini server VAD | Local Silero VAD (LiveKit `AgentSession(vad=...)`) or bridge VAD |
|---|---|---|
| Ends the caller's turn | **Yes**, the only one that does | No: on realtime it cannot interrupt or end a turn |
| Barge-in on agent speech | **Yes** | No (except the greeting hold, section 3) |
| `user_state` in LiveKit | Without a local VAD, `user_state` says "speaking" exactly when the caller has **stopped** (the plugin drops `voice_activity` and maps its events to the model's reply) | **Correct** speaking/listening edges |
| Uses | | Greeting barge-in, the hang-up interlock, the silence clock, true end-of-speech for latency measurement |

- Client-side turn detection (`AutomaticActivityDetection(disabled=True)` plus LiveKit's turn detector) is **broken on 3.8 through the plugin**: replies come off by one. Keep server VAD. See `references/02-gemini-live-model-and-config.md` section 8.4.
- **Inert on a Google realtime model:** `turn_detection=` (turn detector/EOU model ignored with a warning), `turn_handling` endpointing, `min_endpointing_delay`, `preemptive_generation`, `on_user_turn_completed` (**never called**), and `tts_node` (never called; audio goes through `realtime_audio_output_node`). `transcription_node` still runs on output transcription.
- **Realtime substitute for user-turn events:** emit your "user turn" analytics from `user_input_transcribed` when `is_final`. Otherwise every realtime call shows zero caller turns.
- **Risk:** realtime playout waits on the session's user-silence event, so a local VAD stuck on "speaking" on a noisy line can delay a reply. Keep `activation_threshold` at 0.5 or above, and watch for it.

### 8.2 Latency: where the reply gap comes from

Mouth-to-ear estimate for an India caller with Gemini in the US:

| Hop | Time |
|---|---|
| PSTN plus SIP provider, each way | 60-150 ms |
| Provider to LiveKit SIP | 20-50 ms |
| SFU to agent | 30-70 ms |
| Agent buffering | 0-50 ms |
| Agent to Gemini (US), one way | 115-140 ms |
| **Gemini server VAD hangover plus first audio** | **about 1.4-1.75 s** |
| **Total** | **about 1.6-2.2 s** |

Measured:
- **Raw API, India to US, default VAD:** median about 1.6 s (1.47-1.91 s). Manual `activity_end` gives 0.93-1.18 s, so the **server VAD hangover is about 0.4-0.6 s**.
- **Production realtime (local VAD end of speech to first audio):** 1.2-1.6 s typical, occasionally 2-3.5 s.
- **Production recordings (energy-based caller-end to agent-start gaps):** median 1.58 s, p75 2.27 s, p90 3.20 s over 50 caller-to-agent gaps from six real test calls (stereo recordings, energy thresholds, measured with `templates/testing/measure_reply_gaps.py`'s method). The long tail (4.5-5.4 s) came after long caller explanations and from background speech holding the turn open.
- **Text turn to first audio:** about 0.74-0.78 s.
- **Cascaded comparison:** STT, then LLM, then TTS gave p50 3.17 s and p90 3.98 s over 196 real turns.

Levers, in order of effect:
1. A lean prompt (TTFA grows with prompt size).
2. `location="us"` with pre-refreshed credentials.
3. Keep the agent next to the **SIP media** (India), not next to Gemini. A US agent moves the ocean crossing onto lossy real-time UDP media and saves nothing per turn.
4. No input buffering beyond 20-50 ms (LiveKit: `AudioInputOptions(sample_rate=16000, frame_size_ms=50)` gives one resample).
5. VAD tuning: weak and inconsistent (`references/02-gemini-live-model-and-config.md` section 8.2).

### 8.3 Measuring latency on realtime correctly

`RealtimeModelMetrics.ttft` reads **0.000-0.013 s on every turn**, because it is measured from the first server message of the reply, which already carries audio. The real caller wait is measured nowhere by default. Measure it from **the local VAD end of speech minus `min_silence_duration`** to **the first audio chunk** (tap the SDK's `receive()`):

```python
@session.on("user_state_changed")
def _on_user_state(ev):
    if ev.old_state == "speaking" and session.agent_state != "speaking":   # backchannels during agent speech
        signals.note_speech_end(time.monotonic() - VAD_MIN_SILENCE_S)      # made fake 250-400 ms samples
```

Rules learned:
- Gemini sometimes starts replying **before** the local VAD's min-silence elapses. Hold that first-audio time and pair it when the VAD end arrives; dropping those turns (the fastest ones) biased the metric up.
- `ACTIVITY_START` resets pending anchors.
- Gemini's `ACTIVITY_END` often arrives in the **same message** as the first audio, so it can't be used alone.

The receive-tap code is in `references/10-testing-and-evaluation.md`.

Production Silero settings: `min_speech_duration=0.1`, `min_silence_duration=0.25`, `prefix_padding_duration=0.5`, `activation_threshold=0.5`, `sample_rate=16000`, `force_cpu=True`. The 1.7.1 defaults are 0.05 / 0.55 / 0.5 / 0.5.

### 8.4 Background chatter and noise cancellation

- **Background voices:** TV, family or a shop floor can hold Gemini's turn open (no `ACTIVITY_END` while someone is talking) or trigger false barge-ins. Proactive audio filters some "non-addressed" speech, but not reliably.
- **`noise_cancellation.BVCTelephony()`** on `AudioInputOptions` (LiveKit Cloud only, `livekit-plugins-noise-cancellation~=0.3`) runs inside the native audio stream, so Gemini and its VAD hear only denoised audio. Background-voice cancellation matters **more** on realtime, because server VAD alone decides barge-in.
- **Limits:**
  - It is not echo cancellation.
  - It can clip soft onsets ("haan", "ji", digits). A/B test it on real calls, and turn it off only if clipped first syllables appear.
  - Use **one** NC method only: not BVC in code **and** Krisp on the trunk.
  - Self-hosted LiveKit can't use it (Cloud-authenticated). Options there are in `references/05-livekit-self-hosted.md`.
- **Bridge:** no built-in equivalent. Options are a server-side denoiser before resampling (**VERIFY** licensing and latency), or `START_SENSITIVITY_LOW`.

---

## 9. Multilingual callers

### 9.1 The rules that worked (prompt block)

Put this straight after the persona and accent sections, in capitals for the first line only. This is Google's own template. Adapt `<L2>`:

```text
# Language
RESPOND IN THE LANGUAGE THE CALLER IS SPEAKING. YOU MUST RESPOND UNMISTAKABLY IN THAT LANGUAGE,
FROM YOUR VERY FIRST REPLY.
- The greeting was in English only because the caller had not spoken yet; it doesn't decide the language.
  If the caller's first words are <L2>, your first reply is in <L2> from its first word: never an English
  apology or acknowledgement before it.
- Romanised <L2> is <L2>.
- The whole reply is in that language. A <L2> word at the start followed by English sentences is an English reply.
- A caller who answers with only the name of a language has chosen it.
- A few English words inside a <L2> sentence are not a change of language.
- The caller's first full sentence sets the call's language; keep it to the goodbye. Short English answers
  (a product name, a size, "No, same number", "okay", a number) in a <L2> call are still <L2>-call answers.
  Change only if the caller asks, or speaks whole sentences in another language two turns running.
- The English phrases quoted in these instructions are illustrations, not wording to read out.
- Read-backs, confirmations, the close, the reference number, thank-yous and the goodbye all follow the language.
- Never comment on the caller's language or announce a switch.
```

### 9.2 Evidence (measured on 3.8, Vertex)

| Change | Before | After |
|---|---|---|
| First-reply rule (English greeting, Hindi caller) | English first reply 4/14 | 0/8 |
| Language section moved straight after the role, openers labelled per language | fully-English replies to a Hindi caller 2/6 | 0/6 |
| "The whole call stays in the language" line | Hindi replies 5/6 | 6/6 |
| Short-English-answers rule | A Hindi call's close drifted into English after "No, same number" | held |
| Final simulations | | Hindi calls: 1/100 to 1/115 English-only replies |
| `language_code=en-IN` | English first reply to a Hindi caller 0/10 | **4/7: worse** |

### 9.3 Mechanics that support the prompt

- **`speech_config.language_code`:** off by default for mixed-language lines (`references/02-gemini-live-model-and-config.md` section 6).
- **Transcription hints:** set `input_audio_transcription.language_codes` to the expected caller languages.
- **System-driven prompts** (check-ins, closes) must say "in the language you have been speaking", never contain fixed English text (section 5).
- **Fixed wording per language.** Never let the model translate the one value a caller will hold you to: a follow-up window, a commitment, a number. A live translation once produced "twenty-four to eight hours". Give such lines **pre-translated and labelled per language**. A Hindi-only goodbye example leaked into English calls; giving it in both languages, each labelled, fixed it (6/6 and 6/6).
- **Gender and register:** in gendered languages (Hindi), the model defaulted to masculine verb forms for a female persona. A rule naming the persona's grammatical gender fixed it (0 errors after). Ask for everyday spoken register, not formal written Hindi.
- **Script:** Hinglish input is answered in Devanagari Hindi by default. Say so explicitly if you want romanised or English output.
- **Unlisted languages:** Kannada is not on the Vertex list but worked in practice; one Tamil word appeared in a Kannada reply. Get native-speaker QA for every language you advertise.
- **Don't infer the language** from the caller's name, number or accent.
- **LLM judges misread romanised Hindi as English.** Hand-check every language verdict in evaluations (`references/10-testing-and-evaluation.md`).

---

## 10. Voice, accent and tone

### 10.1 Accent comes from the prompt, not the voice

Live voices speak General American English unless told otherwise: the first real call was judged American 6/6 blind. A one-line instruction was drowned out by a long prompt. A **dedicated block placed straight after the persona** fixed it: Indian accent 5/5 in 3/3 sessions inside the full prompt.

```text
# Your voice and accent
You grew up in <city>, went to school in <country> and have never lived abroad.
- When you speak English, SPEAK <LOCALE> ENGLISH WITH A CLEAR, NATURAL <LOCALE> ACCENT, like an educated
  <nationality> professional on a work call: never American or British, with level, even intonation.
- When you speak <L2>, speak it as a native speaker, with native pronunciation and rhythm.
- Say local names, places and addresses the local way.
```

Use the same accent sentence in the **greeting render prompt** (section 2.2).

### 10.2 "Not dramatic": what drove theatrical prosody, and the fix

The line's owners said the voice "sounds dramatic". Findings:
- Their human agents' pitch spread (p10-p90) was **6-8 semitones**; the bot's was **8-11**.
- Humans repeat plain acknowledgements freely.
- An earlier accent-features list asked for "<locale> sentence melody". **Melody wording invited pitch swings.**
- "React freshly to each answer" rules and **emotional words in the prompt** ("I'm so sorry", "that's terrible") **drive the model's affective prosody**. Affective dialog is always on in 3.8: it mirrors and amplifies emotion.
- **Blind judge on emotional moments:** dramatic **4.6 to 2.6**/5, and professional 2.1 to 3.4, after the change below. Choosing a calmer voice helped further (`references/02-gemini-live-model-and-config.md` section 5.2).

```text
# How you sound
- Speak at an ordinary, steady conversational pace, never rushed and never drawn out, with no pauses for effect.
  Keep turns short rather than speaking slowly; slow down only for numbers, addresses and email IDs.
- Keep a calm, matter-of-fact, professional tone, the same for good news and bad: an even pitch with small
  natural movement, steady volume, crisp diction. Show care in your words, not your voice: no gasps, no breathy
  or whispered sympathy, no sing-song, no stressed or stretched words. Stay as calm when the caller is upset.
- Read identifiers slowly, one digit at a time, in short groups of three or four with a pause between groups.
- Never speak formatting, symbols or lists aloud.

# Sound like a person
- Short spoken sentences, one idea each; usually one or two sentences; never more than about eight seconds.
- Acknowledge a routine answer with one plain word, or the value said back flatly, then the next question.
  Repeating these plain words is natural. No exclamations.
- Say something of your own only when the caller tells you what went wrong: one short, matter-of-fact clause.
- One plain, brief apology. Strong feeling words make the voice theatrical; leave them out.
- Call-centre phrases make you sound like a recording ("I understand", "we regret the inconvenience", "kindly").
- Match the caller's pace, not their emotion. Use their name now and then, not every turn.
```

### 10.3 Pace and numbers

- **No rate knob:** `speaking_rate` and `pitch` are rejected. Default English pace is about 3.9-4 words/s (about 235 wpm), which is fast for a phone line.
- "Speak slowly" alone did little. An explicit, strong "about 25% slower" instruction gave about −19%. **Keep turns short instead.**
- Speech accelerates in turns longer than about 60 s (Vertex troubleshooting).
- **Numbers and identifiers:** one digit at a time, in groups of 3-4, slower. Read a reference number in groups of three and offer to repeat it.
- **Read back only the few values that matter, in the caller's own words.** Treat the phone number and address as their own check.
- **Challenge wrong-shaped values:** a 6-digit postcode, a 10-digit mobile, counting runs such as "1 2 3 4 5 6". Change method after two failures (spell it, send it by message, skip it).
- **Never offer a menu of values** ("<size A>, <size B> or <size C>"): the caller said one size three times and the agent repeated a different size from the menu.

### 10.4 Choosing the voice

Full catalogue, measurements and the judging method are in `references/02-gemini-live-model-and-config.md` section 5; the narrowband effects are in `references/08-audio-formats-and-conversion.md` section 11.
- **30 prebuilt voices, set once at connect** (`voice=` in LiveKit, `speech_config.voice_config` in a bridge). Changing the voice mid-call is an `update_options` reconnect (about 2.2 s of dead air). Render the cached greeting in the **same** voice and with the same language hint as the call.
- **Voices carry no accent or locale.** Accent and pace come from the prompt (section 10.1).
- **Choose by blind judging rendered audio,** not by the style label: same moments (greeting, upset caller, safety complaint, a non-English turn), several runs per voice, a contrast-anchored rubric, judged on the narrowband version callers hear (`references/10-testing-and-evaluation.md` section 8).
- **Measured:** Gacrux ("Mature") was the calmest and most professional with a strong local accent (dramatic 2.58-2.92/5); Sulafat ("Warm") was warmer but more dramatic. The tone prompt mattered more than the voice: Sulafat went from dramatic 4.58 to 3.75 with only the prompt changed.
- **Check pitch against the persona:** Gacrux and Pulcherrima measured in the male F0 range (about 135-152 Hz). Breathy or soft voices (Enceladus, Achernar) lose most on a 300-3400 Hz line.
- **Known limitation:** 3.8 occasionally speaks one turn in a different voice, then reverts. There is no fix; log it in QA.
- Custom (replicated) voices are allowlist-only and need consent audio (`references/02-gemini-live-model-and-config.md` section 5.3).

---

## 11. Background audio

### 11.1 LiveKit `BackgroundAudioPlayer` (1.7.1 API, checked in source)

```python
from livekit.agents import AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip

background = BackgroundAudioPlayer(
    ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.15),   # loops for the whole call
    thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.3, probability=0.6),
                    AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.25, probability=0.4)],
)
await background.start(room=ctx.room, agent_session=session)   # after session.start(); agent_session needed for thinking

handle = background.play(BuiltinAudioClip.HOLD_MUSIC, loop=True)   # on demand, e.g. during a transfer
...
handle.stop()                                                        # AudioConfig(fade_out=...) for a soft stop
```

- **Built-in clips (1.7.1):** `CITY_AMBIENCE`, `FOREST_AMBIENCE`, `OFFICE_AMBIENCE`, `CROWDED_ROOM`, `KEYBOARD_TYPING`, `KEYBOARD_TYPING2`, `HOLD_MUSIC`.
- **Sources:** a `BuiltinAudioClip`, a file path (loopable), or an `AsyncIterator[rtc.AudioFrame]`.
- **`AudioConfig` fields:** `source`, `volume` (0-1), `probability` (a list picks one; a total below 1 means a chance of silence), `fade_in`, `fade_out`.
- **The realtime caveat:** `thinking_sound` plays only while `agent_state == "thinking"`. On a Gemini realtime model that happens **only during tool execution**, not in the 1.2-1.6 s end-of-turn gap. It does not mask normal reply latency. Production used it only on the cascaded pipeline (`KEYBOARD_TYPING` 0.3 and `KEYBOARD_TYPING2` 0.25).
- **Background audio is its own published track.** The SIP participant hears it mixed in, but it is **not** in the RecorderIO recording (`audio.ogg`), whereas a room-composite egress would capture it.
- **Ambient sound on PSTN:** G.711 and carrier noise suppression can turn ambience into hiss, and it can confuse the caller's handset echo canceller. Keep it subtle (0.1-0.2), or skip it. Test on a real phone.
- It is not supported in console mode (`python agent.py console` warns).

### 11.2 Bridge equivalent

There is no player; mix into your outbound PCM before encoding:

```python
import numpy as np

class Mixer:
    """Mix a looping ambience bed (and an optional one-shot clip) under the agent's voice. All PCM16 at wire rate."""
    def __init__(self, bed: bytes, bed_gain=0.12):
        self.bed = np.frombuffer(bed, dtype=np.int16).astype(np.float32) * bed_gain
        self.pos = 0
    def mix(self, voice: bytes) -> bytes:
        v = np.frombuffer(voice, dtype=np.int16).astype(np.float32)
        idx = (np.arange(len(v)) + self.pos) % len(self.bed)
        self.pos = (self.pos + len(v)) % len(self.bed)
        return np.clip(v + self.bed[idx], -32768, 32767).astype(np.int16).tobytes()
```

- If you want ambience during silence too, the pacer must keep sending frames (`mix(silence)`) when no voice is queued.
- **Thinking sound:** on `tool_call`, enqueue a typing clip; stop it on the first `inline_data` of the reply, or on `interrupted` (flush).
- **Hold music** during a transfer: most providers can play it themselves (Twilio `<Enqueue>`/`<Play>`, the Exotel Connect applet `music_on_hold`), so prefer that.

---

## 12. Transfer to a human, and DTMF

Brief here; provider specifics are in `references/06-sip-trunking-providers.md` and `references/07-websocket-media-streams.md`.

### 12.1 LiveKit (SIP)

- **Cold transfer (SIP REFER):** `await ctx.transfer_sip_participant(participant, "tel:+91XXXXXXXXXX", play_dialtone=False)` (1.7.1 `JobContext` helper), or `ctx.api.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(room_name=..., participant_identity=..., transfer_to=..., play_dialtone=False, ringing_timeout=...))`.
  - The trunk must allow REFER. Plivo needs `sip:+E164@<id>.zt.plivo.com`.
  - On timeout the request errors and the caller stays in the room.
- **The tool pattern:**
  - Register `transfer_to_human(reason: str)` **only when a transfer number is configured**.
  - Before transferring, have Gemini say the hold line (`generate_reply`, since there's no `say()` without audio frames) and `wait_for_playout()`.
  - On failure, **return a string** telling the model the caller is still there and to promise an urgent callback. A string is right here: you want the model to speak.
  - Without a number, the prompt has an honest mode: never claim to connect or transfer; promise a callback, without a time.
- **Warm transfer:** `livekit.agents.beta.workflows.WarmTransferTask` (beta) puts the caller on hold with `HOLD_MUSIC`, dials and briefs the human in a consult room, then moves them in.
  - It requires `RoomOptions(delete_room_on_close=False)` and an outbound trunk.
  - It is untested end to end with Gemini Live (**VERIFY**).

### 12.2 Bridges

- **Twilio:** close the WS. Twilio POSTs to `<Connect action>`, which returns `<Dial>`. Or use REST `Twiml=<Response><Dial>...`.
- **Exotel:** write the decision to your DB, close the WS, then Passthru (200/302) to the Connect applet.
- **Plivo:** REST transfer with `legs=aleg&aleg_url=`.
- **In every case:** say the hold line first, and wait for its `mark` before acting.

### 12.3 DTMF

- **LiveKit:** `@ctx.room.on("sip_dtmf_received")` gives `rtc.SipDTMF` (`code`, `digit`, `participant`). Forward digits to the model with a short context note or `generate_reply(instructions=f"The caller pressed {digit} on the keypad.")`. Or collect a run (an account number ending with `#`) in code, then pass the whole value in.
  - `AgentSession(ivr_detection=True)` and `beta.workflows.GetDtmfTask` exist; their behaviour with a realtime model is untested (**VERIFY**).
  - Sending digits: `await ctx.room.local_participant.publish_dtmf(code=1, digit="1")`.
- **Bridges:** an inbound `dtmf` event arrives on Twilio, Exotel, Plivo, Telnyx and Vonage. Forward it as text (`send_client_content(user: "(The caller pressed 1 on the keypad.)", turn_complete=True)`). Sending DTMF over the socket is **not** supported on Twilio or Exotel; Plivo has `sendDTMF`.
- Prefer speech for data capture. Use DTMF for what speech gets wrong on a noisy line: long numeric ids, PINs, or "press 1 to confirm" in a compliance flow.

---

## 13. Other mechanics and configuration: caller number, consent line, prewarm, compression, resumption, failover, transcription

**Caller number (LiveKit SIP).** Read it from the participant attributes, **before** `session.start`, and put it in the prompt. Instructions can't be updated cheaply later.

```python
CALLER_NUMBER_ATTRS = ("sip.phoneNumber", "sip.from", "sip.fromUser")   # caller's number, preferred order
# never "sip.trunkPhoneNumber": that is the number the caller DIALLED

def caller_number_from(participant) -> str | None:
    attrs = getattr(participant, "attributes", None) or {}
    for key in CALLER_NUMBER_ATTRS:
        value = str(attrs.get(key) or "").strip()
        if value and any(ch.isdigit() for ch in value):
            return value
    return None

await ctx.connect()
participant = await asyncio.wait_for(ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP), 8)
caller_number = caller_number_from(participant)
ctx.proc.userdata[f"caller_number:{ctx.job.id}"] = caller_number      # for on_session_end (room is gone by then)
instructions = BASE_PROMPT + per_call_section(today=..., reference=..., caller_number=caller_number)  # LAST
```

- The dispatch rule must have `hidePhoneNumber: false`, or the number is absent. The trade-off is that the number then appears in the room name.
- Log which attribute keys arrived, with the values masked (`***1234`). One real call settles what the platform sends; simulations don't traverse SIP.
- Add the "confirm this number" instruction **only when a number exists**. Telling the model to confirm a number it wasn't given left the callback field empty. Ask "Is this the best number to reach you, or would you prefer another?" once, and never read the digits back.
- **Bridges:** Twilio `start.customParameters` / `From` on the webhook; Exotel `start.from` and the dynamic URL; Plivo `extra_headers`.

**Recording consent / AI disclosure line.** Put it in the cached greeting audio, not in a generated turn. The prompt must also never claim to be human: "if asked, say you are the automated assistant and offer a callback". India rules are in `references/12-india-telephony-compliance.md`.

**Room options used in production (LiveKit 1.7.1):**

```python
ROOM_OPTIONS = room_io.RoomOptions(
    audio_input=room_io.AudioInputOptions(
        sample_rate=16000, frame_size_ms=50,                     # one 48k->16k resample in Rust (default is 24 kHz)
        noise_cancellation=noise_cancellation.BVCTelephony()),
    audio_output=True,                                           # 24 kHz default = Gemini's native rate
    close_on_disconnect=True,                                    # caller BYE -> session closes
    delete_room_on_close=True,                                   # session close -> room deleted -> SIP BYE
)
```

`pre_connect_audio` applies to client-SDK tracks only and is a no-op for SIP.

**Max duration:** 900 s, enforced by the silence monitor (section 5). There is no native session-length cap: `user_turn_limit` limits a single turn.

**Prewarm:** section 2.3. Keep production warm (a LiveKit Cloud plan with cold-start prevention, or min instances >= 1 for a bridge), so the first caller after idle doesn't wait 10-20 s.

**Multi-region failover:** use the prewarm probe plus `RealtimeModelFallbackAdapter`. Share the hold object and any signals objects across models. See `references/02-gemini-live-model-and-config.md` section 2.4.

**Instructions are set once.** Resolve everything per-call before `session.start`. On Vertex, `update_instructions` mid-call injects the whole prompt as a model turn.

**Provider errors:** listen to `session.on("error")` and emit a `provider_error` event (with layer, recoverable, model). Per-turn latency only covers turns that produced a reply: a call with seven 502s looked healthy.

### 13.1 Session configuration that shapes the call

The fields themselves are in `references/02-gemini-live-model-and-config.md`; this is what each means for call behaviour.

| Setting | What to do | Why (call behaviour) |
|---|---|---|
| **Context compression** (02 section 9) | Always on: `ContextWindowCompressionConfig(sliding_window=SlidingWindow(target_tokens=64_000))` | Without it, audio-only sessions cap at about 15 min. Compression drops or summarises the oldest turns, so facts you must keep (date, reference number, caller number) belong in the system prompt, which is not compressed. It also caps per-turn re-billing. |
| **Session resumption and GoAway** (02 section 10) | LiveKit: the plugin requests resumption and reconnects on GoAway by itself; on plugin 1.7.1 apply the history-replay workaround (bug #6000, fixed in 1.8.1). Bridge: store `new_handle` from every resumable `session_resumption_update`; on `go_away` reconnect at the next `turn_complete` while playout is idle, buffering caller audio meanwhile | Connections last about 10 min (GoAway measured at about 542 s with `time_left=30s`, close at about 602 s with 1011). A resumed reconnect costs about 2.2-2.35 s and keeps context. Resumption stores session data up to 24 h: a zero-retention requirement means no resumption and a fresh session instead. Test one call longer than 9 minutes before launch. |
| **Region failover** (02 section 2.4) | Prewarm probe of the primary, then the fallback region; in-call `RealtimeModelFallbackAdapter([us, us-central1])` sharing the hold object (section 3.4); bridge: an ordered location list with backoff | 3.8 Live is served only from `us`, `eu` and `us-central1`. The plugin does not retry a failed **first** connect: without the adapter one failed connect gave half a greeting and a dead line. A resumption handle is not presumed valid across regions (**VERIFY**): re-seed context with `send_client_content(turn_complete=False)` on a region swap. |
| **Input transcription** (02 section 7.1, 7.3) | `input_audio_transcription=AudioTranscriptionConfig(language_codes=[<caller languages>], custom_vocabulary=[<multi-word phrases>])` | Arrives in one piece at the end of the turn (no interim text, no `finished` flag), and through LiveKit the final event fires after the agent's reply. `language_code` on transcripts is empty. Bare vocabulary words pull ordinary words towards them (a farewell heard as the brand name cancelled a hang-up). |
| **Output transcription** (02 section 7.2) | `output_audio_transcription=AudioTranscriptionConfig()` | Needed for history and logs, but about 1.7 s late and lossy (2/12 empty, 1-2/12 truncated). Never signal call control through it (section 6.1); strip `<no speech>` / `{pause}` before storing; build records by post-call extraction (section 7.8). |
| **Server VAD knobs** (02 section 8) | Leave `realtime_input_config` unset unless a measurement says otherwise | Defaults gave a median of about 1.6 s; tuning was weak and inconsistent, and a shorter silence window cuts off callers reading out numbers (section 8). |
| **Mid-session changes** (02 section 11) | Build tools, voice and the full prompt before connect | Tool or voice changes reconnect (about 2.2 s). On Vertex `update_instructions` injects the whole prompt as a model turn. |
| **Never-send settings** (02 section 4.2) | No `thinking_config`, `enable_affective_dialog`, `proactivity`, small `max_output_tokens`, `history_config`, TEXT modality, `speaking_rate`/`pitch` | Several are accepted at setup and then break the call silently (no audio, a turn that never completes, a greeting that gets no reply). Pin with a unit test plus a live probe. |

---

## 14. Pre-launch checklist

- [ ] Cached greeting rendered in the Live voice, verified (≥70% word coverage), and played via `say(text, audio=)` or the carrier; pushed into context only if not interrupted.
- [ ] All waits bounded: participant link 8 s, greeting playout 20 s, post-call jobs within their budget.
- [ ] Caller audio held during the greeting, with a 0.8 s local-VAD barge-in, 0.6 s replay lead and 0.4 s echo tail; `aec_warmup_duration=None`. Phantom-reply replay test: 0 of N.
- [ ] Bridge: `interrupted` triggers local flush, provider clear and pacer reset; outbound paced with a ≤200 ms lead.
- [ ] Silence ladder 20/40/60 s accepts `away`; instructions in the call's language, each saying it is not the close; max duration 900 s.
- [ ] `end_call` returns `None` (LiveKit); 4 s grace decided at commit time; close generations; speech suppressed after goodbye; `delete_room_on_close=True` (or the provider hang-up API after the goodbye mark).
- [ ] Tools registered before start, flat schemas, one-sentence descriptions, structured results, a retry cap in the prompt, idempotent side effects.
- [ ] Local VAD wired for `user_state`; latency measured from the local VAD end; provider errors emitted.
- [ ] Language rules at the top; accent block after the persona; tone block with no feeling words; per-language fixed lines.
- [ ] Test calls: every language, talking over the greeting, a silent caller, a caller who coughs after goodbye, a call longer than 9 minutes (GoAway), a transfer, and the caller hanging up first.
