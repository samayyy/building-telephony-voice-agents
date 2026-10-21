# 02. Gemini Live (gemini-3.8-live): model, endpoints and session configuration

This file covers the model and every setup field that matters for a phone agent: which model id to use, the Vertex AI and Gemini API endpoints and regions, the audio contract, which `LiveConnectConfig` fields to set and which to never send, voices, language hints, transcription, server VAD, context compression, session limits and resumption, pricing, quotas and known 3.8 bugs. It is written for both LiveKit Agents (`livekit-plugins-google`) and a raw google-genai or WebSocket bridge. Call behaviour built on top of this config (greeting, barge-in, silence, hang-up, tools) is in `references/03-conversation-mechanics.md`. Prompt writing is in `references/09-prompting-voice-agents.md`.

**Versions this was verified against:**
- `gemini-3.8-live` on Vertex AI, probed live from India (Sep to Oct 2026).
- google-genai 2.21.0: field names checked in the installed source. 2.32.0 is current on PyPI and adds `send_context_update`.
- livekit-agents and livekit-plugins-google 1.7.1 in production. 1.8.8 is current per the docs on 2026-10-21.

Measured numbers are from a production inbound consumer-care deployment (Vertex, agent in India, model in the US) unless a source is cited. Items marked **VERIFY** are not confirmed.

## Contents

1. [Model ids and variants](#1-model-ids-and-variants)
2. [Vertex AI vs Gemini API: auth, endpoints, regions, failover](#2-vertex-ai-vs-gemini-api-auth-endpoints-regions-failover)
3. [Audio contract](#3-audio-contract)
4. [LiveConnectConfig: what to set, what to never send](#4-liveconnectconfig-what-to-set-what-to-never-send)
5. [Voices: catalogue, choosing one, custom voices](#5-voices-catalogue-choosing-one-custom-voices)
6. [`speech_config.language_code`: a measured trade-off](#6-speech_configlanguage_code-a-measured-trade-off)
7. [Input and output transcription](#7-input-and-output-transcription)
8. [Server VAD: `realtime_input_config` knobs and their measured effect](#8-server-vad-realtime_input_config-knobs-and-their-measured-effect)
9. [Context window compression](#9-context-window-compression)
10. [Session limits, GoAway, resumption, reconnect](#10-session-limits-goaway-resumption-reconnect)
11. [Changing things mid-session](#11-changing-things-mid-session)
12. [Pricing and cost drivers](#12-pricing-and-cost-drivers)
13. [Quotas and capacity](#13-quotas-and-capacity)
14. [Known 3.8 issues and mitigations](#14-known-38-issues-and-mitigations)
15. [Ephemeral tokens (client-side apps only)](#15-ephemeral-tokens-client-side-apps-only)
16. [Config validation checklist](#16-config-validation-checklist)
17. [Sources](#17-sources)

---

## 1. Model ids and variants

| Model id | Where | Use for phone? | Notes |
|---|---|---|---|
| `gemini-3.8-live` | Vertex AI (GA 24 Sep 2026) and Gemini API (15 Sep 2026) | **Yes** | Native audio in and out. 128k context, 64k output. Knowledge cutoff Jan 2025. Function calling and Google Search grounding supported. No structured output, context caching, code execution or URL context. |
| `gemini-3.8-live-extended-thinking` | Gemini API only (not on the Vertex card) | No | Background reasoning; `turn_complete` is not "idle", so you must read `server_content.interaction_status` (`IN_PROGRESS`/`IDLE`). NON_BLOCKING tools only (`BLOCKING` is a hard error). Thinking levels `low`/`medium`/`high`. **Failed to connect in every Vertex location tried.** It adds latency, and a secondary report says it narrates its reasoning aloud. |
| `gemini-3.5-transcribe-live` | Vertex and Gemini API | ASR only | Streaming transcription with interim results, timestamps and code-switching. Use it if you need live captions, which 3.8 Live does not give (section 7). |
| `gemini-live-2.5-flash-native-audio` (Vertex) / `gemini-2.5-flash-native-audio-preview-12-2025` (API) | both | Legacy | This is still the **LiveKit plugin default** when `model` is omitted, so always pass `model="gemini-3.8-live"`. Measured: 3.8 answers Hindi about 0.6-0.8 s faster than 2.5 (2.5 took about 2.2 s). |

Model-card sources: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-live and https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live

> **Thinking.** The Vertex card says "Thinking: Not supported". Measured: the model **does** think by default, at 32-120 `thoughts_token_count` per short turn. Leave it alone (see section 4.2).

---

## 2. Vertex AI vs Gemini API: auth, endpoints, regions, failover

### 2.1 Decision table

| | Vertex AI (Gemini Enterprise Agent Platform) | Gemini API (AI Studio key) |
|---|---|---|
| Auth | Service account / ADC bearer token. "Gemini Live API only provides server to server authentication." | API key (`x-goog-api-key` or `?key=`), or an ephemeral token for browsers and apps |
| google-genai client | `genai.Client(vertexai=True, project=P, location=L)`. The SDK calls `vertexai` a "legacy flag"; the newer name is `enterprise=True` (**VERIFY** on your SDK version). | `genai.Client(api_key=...)` |
| Model name | `gemini-3.8-live`, or the full resource `projects/P/locations/L/publishers/google/models/gemini-3.8-live` | `gemini-3.8-live` (`models/gemini-3.8-live` on the wire) |
| WebSocket URL | `wss://{LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent`. Multi-region: `wss://aiplatform.us.rep.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent` (the SDK defaults to `v1beta1`) | `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent` |
| Regions | **Only `us`, `eu` (multi-regions) and `us-central1`** | Google-managed, no choice |
| Data controls | Data residency (US/EU), CMEK, VPC-SC, Provisioned Throughput | Free tier data is used to improve products |
| Response modality | AUDIO only (TEXT is rejected with 1007) | AUDIO only ("native audio models only support AUDIO") |
| `speech_config.language_code` | Accepted, as a hint (section 6) | "don't support explicitly setting the language code" |
| `tool_response_scheduling` | **Ignored** (the LiveKit plugin warns and drops it) | Supported |
| Behaviour parity | The two backends do **not** behave identically. Probe the one you deploy on. | |

**Pick Vertex** for anything that carries real caller audio: enterprise data terms, a region you choose, quotas you can raise, and Provisioned Throughput. Use the Gemini API for prototypes, or when the LiveKit Inference route (`llm="google/gemini-3.8-live"`, billed by LiveKit) suits you.

### 2.2 Regions on Vertex (measured)

- **Served:** `us-central1`, `us` and `eu`. Vertex lists ML processing in the `us`/`eu` multi-region.
- **Not served:** `global`, `asia-south1`, `europe-west4` and about 13 other regions return close code `1008 Publisher model ... was not found`. `asia-south2` returns HTTP 400.
- **The REST model catalogue is not proof a model is served there.** Only a live `connect()` proves it.
- **Endpoint mapping:** google-genai maps `location in {"us","eu"}` to `https://aiplatform.{us|eu}.rep.googleapis.com/` (`_api_client.py`). The LiveKit plugin passes `location` straight through, so `RealtimeModel(vertexai=True, location="us", ...)` works.
- **Connect time from India:**

  | Location | Session connect |
  |---|---|
  | `us` | 1.46-1.6 s, or 1.27 s with pre-refreshed credentials |
  | `eu` | 1.64-1.74 s |
  | `us-central1` | 2.1-2.3 s |

  **Per-turn latency is the same in all three.** Prefer `us`, with `us-central1` (or `eu`) as the fallback.
- **Data residency:** there is no Live region in India, Asia or most other countries. Caller audio is processed in the US or EU, so raise this with the data owner's privacy team early. See `references/12-india-telephony-compliance.md`.
- **Validate the location string in your config** (`{"us", "eu", "us-central1"}`), because Vertex accepts any string until connect time.

### 2.3 Credentials: avoid a token fetch on every connect

With no `credentials=`, the SDK runs `google.auth.default()` and refreshes a token on **every** `live.connect()`, which costs 123-711 ms. Pass pre-refreshed credentials and refresh them in the background. Both `genai.Client(credentials=...)` and the LiveKit plugin's `RealtimeModel(credentials=...)` accept them.

```python
import google.auth, google.auth.transport.requests
from google import genai

creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
creds.refresh(google.auth.transport.requests.Request())
client = genai.Client(vertexai=True, project="<PROJECT>", location="us", credentials=creds)
# refresh in the background before creds.expiry (tokens last ~1 h)
```

On LiveKit Cloud the service-account key arrives as a mounted secret file whose path the host chooses. Discover it (look for a JSON file containing `"type": "service_account"`) and set `GOOGLE_APPLICATION_CREDENTIALS`. Always pass `project=` explicitly, because ADC may not carry one. See `references/04-livekit-cloud.md`.

### 2.4 Region failover (two layers)

**Layer 1, at process start (prewarm):** open one tiny Live session per worker process to the primary region, then the fallback. Whichever succeeds becomes the process's location. The same session renders the cached greeting (see `references/03-conversation-mechanics.md` section 2). If both fail, route that process's calls to a fallback pipeline (STT, then a text LLM, then TTS). A greeting followed by silence is the worst outcome a phone line can have.

**Layer 2, in call (LiveKit):** the plugin **does not retry a failed first connect**; a connect failure is fatal immediately, and only errors inside a running session are retried. Wrap two regional models in `RealtimeModelFallbackAdapter` (it exists in livekit-agents 1.7.1 and 1.8.x):

```python
from livekit.agents import llm as llm_api
from livekit.plugins import google

def build_live(location: str) -> google.realtime.RealtimeModel:
    return google.realtime.RealtimeModel(
        model="gemini-3.8-live", vertexai=True, project="<PROJECT>", location=location,
        voice="Gacrux", **COMMON_LIVE_KWARGS)          # COMMON_LIVE_KWARGS from section 4.4

primary, secondary = build_live("us"), build_live("us-central1")
llm = llm_api.RealtimeModelFallbackAdapter([primary, secondary])  # cooldown=10.0, regenerate_on_swap=True
session = AgentSession(llm=llm, vad=silero_vad, aec_warmup_duration=None)
```

Verified: a primary that fails to connect is swapped, the error is re-emitted as recoverable, and the next reply has audio in about 0.7 s. Without the adapter, one failed connect (at call start or on any GoAway reconnect) closed the session without draining, and the caller heard half a greeting and then a dead line. If you subclass the model to observe signals, share one signals object across both models.

**Layer 2, in call (raw bridge):** keep an ordered list of locations. On a connect error or an unrecoverable close, reconnect to the next location, with backoff. A session-resumption handle is tied to a session and cannot be presumed valid across regions (**VERIFY**). On a region swap, start a new session and re-seed context with `send_client_content(turns=[...], turn_complete=False)`.

### 2.5 LiveKit plugin version traps

| Plugin version | `vertexai=True` with `model="gemini-3.8-live"` | Notes |
|---|---|---|
| 1.7.1 | Works (the model is not in its `LiveAPIModels` Literal, but any string is accepted) | Has the resume-replay bug #6000 (section 10.3), and `tool_call_cancellation` is only logged |
| 1.8.0, 1.8.1 | Works | Resume-replay bug fixed in 1.8.1 |
| 1.8.2 to 1.8.4 | **`ValueError`**: it lists 3.8 as a Gemini-API-only model | Workaround: `model="publishers/google/models/gemini-3.8-live"` or the full resource name. Fixed by livekit/agents PR #7587. From 1.8.3, `tool_behavior` defaults to NON_BLOCKING for 3.8 and the bad-tool-schema silence (#7371) is fixed |
| ≥1.8.5 (1.8.8 current) | Works (short name accepted again; mismatches only log a warning) | Rejects `thinking_level` on 3.8 for the Gemini API; NON_BLOCKING tool default kept |

Pin exact versions (`==`). If the Docker image installs from `pyproject.toml` rather than the lock file, a `~=` range silently pulls a newer minor version on rebuild. Add a test that asserts the pins. Sources: production pin history (wheel diff of the plugin, 2026-10-21; see `references/04-livekit-cloud.md` section 1.2); https://community.livekit.io/t/gemini-3-8-live-rejected-with-vertexai-true-in-livekit-plugins-google-although-vertex-ai-serves-it-eu-multi-region/2179

---

## 3. Audio contract

| Direction | Format | Notes |
|---|---|---|
| To Gemini | raw PCM16 little-endian mono **16 kHz**, `mime_type="audio/pcm;rate=16000"` | Send 20-40 ms per `send_realtime_input` (Vertex best practices). Never buffer about 1 s before sending. The Gemini API says it resamples other rates if the rate is in the MIME type; Vertex docs require 16 kHz, and other rates were not tested on 3.8. Resample yourself. |
| From Gemini | raw PCM16 LE mono **24 kHz** (`audio/pcm;rate=24000`) | Median chunk 7,680 B (160 ms), max about 280 ms; occasionally a 2-byte first chunk. Generated faster than real time, so the client paces playout. |
| Token rate | about 25 tokens/s of audio (measured 25.8/s of output speech) | 10 minutes of streamed silence cost about 120-145 tokens in total, so silence is effectively free |
| Stream continuity | Server VAD needs a **continuous** stream | If you stop sending frames after speech, `ACTIVITY_END` never comes. Keep sending (silence if the carrier pauses), or send `audio_stream_end=True` on a gap longer than about 1 s. |

Telephony is G.711 at 8 kHz, so every bridge resamples in both directions. A narrowband test (G.711 µ-law at 8 kHz, then Opus, LiveKit resamplers and pink noise) showed **no loss** of transcription or turn detection in English, Hindi or Kannada. Converters, frame sizes and pacing are in `references/08-audio-formats-and-conversion.md`.

---

## 4. LiveConnectConfig: what to set, what to never send

### 4.1 Field-by-field (google-genai 2.21.0 `LiveConnectConfig`)

| Field | Recommendation for a phone agent | Why |
|---|---|---|
| `response_modalities` | `[Modality.AUDIO]` | Vertex rejects `TEXT` and `AUDIO,TEXT` with `1007 Unsupported modality for Vertex Live API`. The Vertex session-page sample showing `["audio","text"]` is wrong. A "half-cascade" design, where Live understands and a separate TTS speaks, is not possible on Vertex 3.8. |
| `system_instruction` | Set it once, at connect | See `references/09-prompting-voice-agents.md`. Keep per-call values last. |
| `speech_config.voice_config.prebuilt_voice_config.voice_name` | Set it | Section 5 |
| `speech_config.language_code` | **Off by default**; test it before turning it on | Section 6 |
| `input_audio_transcription` | `AudioTranscriptionConfig(language_codes=[...], custom_vocabulary=[...])` | Section 7 |
| `output_audio_transcription` | `AudioTranscriptionConfig()` | Needed for logs and history. Lossy (section 7). |
| `realtime_input_config` | Leave it unset unless you have measured a gain | Section 8 |
| `context_window_compression` | **Always on** for calls that can run past about 9 minutes | Section 9 |
| `session_resumption` | `SessionResumptionConfig()` (LiveKit sets it for you) | Section 10. Stores session data for up to 24 h, so leave it off for zero data retention. |
| `tools` | A few flat function declarations, registered before connect | See `references/03-conversation-mechanics.md` section 7 |
| `temperature` | Default, or 0.6-0.8 | Not a lever that was tuned |
| `explicit_vad_signal` | Optional | `voice_activity` (ACTIVITY_START/END) events arrived on 3.8 even without it |
| `thinking_config` | **Never send** | Section 4.2 |
| `enable_affective_dialog` | **Never send** | Section 4.2 |
| `proactivity` (`ProactivityConfig(proactive_audio=...)`) | **Never send** | Section 4.2 |
| `max_output_tokens` | **Never set it small**; leave it unset | Section 4.2 |
| `history_config` | Don't set it unless you seed history before the first turn and understand the caveat | Section 4.2 |
| `media_resolution`, `avatar_config`, `translation_config` | Not used for phone | `avatar_config` is for video avatars, billed per second of video |

### 4.2 Settings that silently break 3.8 (pin these with a test)

Each was verified live on Vertex. Several are **accepted at setup and then break the call**, so you won't see an error.

| Setting | What happens | Source |
|---|---|---|
| `thinking_config.thinking_budget=0` or `thinking_level=MINIMAL` | Accepted, then **no audio at all**: the output transcript arrives, but zero `inline_data` chunks. Reproduced 3/3. On the Gemini API, `thinking_level` gets `1007 Thinking level is not supported for this model`. | LIVE; community LiveKit thread |
| `thinking_level=HIGH` / `LOW`, `include_thoughts=True` | Accepted on Vertex. `HIGH` added about 80 ms to time-to-first-audio. No benefit found. | LIVE |
| `enable_affective_dialog=True` | Session fails: `1007 Affective dialog is not yet supported on Beyond backend`. Affective dialog is always on in 3.8. `False` is accepted. | LIVE; Vertex guide: "Do not pass `enable_affective_dialog` or `proactivity`" |
| `proactivity` | Accepted and ignored on Vertex. On the Gemini API `proactive_audio: false` "returns an error". Proactive audio is always on. | LIVE; Gemini API 3.8 model page |
| `max_output_tokens` small (e.g. 20) | Accepted, then the turn **never completes**, because audio tokens (about 25/s) count against it | LIVE |
| `response_modalities` with TEXT | `1007 Unsupported modality` | LIVE |
| `speech_config.speaking_rate`, `pitch`, `generation_config.speaking_rate` | Rejected at setup (`Unknown name`). Pace is prompt-only. | LIVE |
| `input_audio_transcription.word_timestamp` / `diarization` | Rejected on 3.8, even though the SDK has the fields | LIVE |
| `history_config=HistoryConfig(initial_history_in_client_content=True)` | Accepted, but then a greeting triggered with `send_client_content`/`generate_reply` gets **no response**, because the first client content is treated as seeded history. Without it, seeding and mid-session appends work. | LIVE; Vertex migration guide recommends it for seeding |
| Unknown voice name, including Chirp-style `en-IN-Chirp3-HD-Kore` | `1007 No matching speaker voice found for name: ... and language: None` | LIVE |
| Prompt-level "thinking" scaffolds ("first, reason step by step...") | Google: purge them "to prevent dropped audio" | Vertex migration guide |

What "proactive audio, always on" means: the model "filters ambient noise and background speech, generating audio responses only when addressed". It also means **the model never speaks first, and never fills a silence**. It stayed silent for 12 s on a silent line. Your code must trigger the greeting and run the silence timer (see `references/03-conversation-mechanics.md`).

### 4.3 Recommended config (raw google-genai)

```python
from google.genai import types

TRANSCRIPTION_LANGUAGES = ["en-IN", "hi-IN"]          # the languages callers actually use
DOMAIN_PHRASES = ["<Brand> <product line>", "<product> <variant>"]   # phrases only, see 7.3

def build_config(system_prompt: str, tools: list[types.Tool], handle: str | None = None,
                 voice: str = "Gacrux", language_hint: str | None = None) -> types.LiveConnectConfig:
    # Deliberately NOT set on 3.8: thinking_config, enable_affective_dialog, proactivity,
    # max_output_tokens, history_config, non-AUDIO modalities, speaking_rate/pitch.
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)),
            language_code=language_hint,                # None by default (section 6)
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=TRANSCRIPTION_LANGUAGES, custom_vocabulary=DOMAIN_PHRASES),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(target_tokens=64_000)),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        tools=tools,
        # realtime_input_config: leave unset unless measured (section 8)
    )
```

The same setup as a raw WebSocket message (Vertex, camelCase as the SDK emits it; snake_case is also accepted):

```json
{"setup": {
  "model": "projects/<PROJECT>/locations/us/publishers/google/models/gemini-3.8-live",
  "generationConfig": {
    "responseModalities": ["AUDIO"],
    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Gacrux"}}}
  },
  "systemInstruction": {"parts": [{"text": "..."}]},
  "tools": [{"functionDeclarations": [{"name": "end_call", "description": "...",
             "parameters": {"type": "OBJECT", "properties": {}}}]}],
  "inputAudioTranscription": {"languageCodes": ["en-IN", "hi-IN"], "customVocabulary": ["<Brand> <product>"]},
  "outputAudioTranscription": {},
  "contextWindowCompression": {"slidingWindow": {"targetTokens": 64000}},
  "sessionResumption": {}
}}
```

Placement rules: `speechConfig`, `thinkingConfig`, `mediaResolution`, `temperature` and `maxOutputTokens` go **inside `generationConfig`**. `realtimeInputConfig`, the transcription configs, compression, resumption, `proactivity`, `historyConfig`, `explicitVadSignal` and `avatarConfig` go at the top level of `setup`. Wait for `setupComplete` before sending anything else. Server frames may arrive as binary WebSocket messages containing JSON, so decode them before `json.loads`. Google publishes an agent skill for raw-WebSocket Live clients: https://github.com/google/skills/blob/main/skills/cloud/gemini-live-api/SKILL.md

### 4.4 Recommended config (LiveKit `google.realtime.RealtimeModel`, plugin 1.7.1 names)

```python
from google.genai import types
from livekit.plugins import google

COMMON_LIVE_KWARGS = dict(
    input_audio_transcription=types.AudioTranscriptionConfig(
        language_codes=["en-IN", "hi-IN"], custom_vocabulary=DOMAIN_PHRASES),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        sliding_window=types.SlidingWindow(target_tokens=64_000)),
    # language="en-IN",                 # -> speech_config.language_code; off by default (section 6)
    # realtime_input_config=...         # unset unless measured (section 8)
    # credentials=prefreshed_creds,     # section 2.3
)
model = google.realtime.RealtimeModel(
    model="gemini-3.8-live", vertexai=True, project="<PROJECT>", location="us",
    voice="Gacrux", **COMMON_LIVE_KWARGS)
```

Plugin facts (1.7.1 source):
- `instructions` normally comes from your `Agent(instructions=...)`.
- The plugin always requests session resumption.
- It passes `AudioTranscriptionConfig` through as-is, so newer SDK fields work.
- `RealtimeCapabilities`: `mutable_tools=False`, `message_truncation=False`, `supports_say=False`, `auto_tool_reply_generation=True`.

Validate the voice name in your own config against the plugin's `Voice` Literal (`livekit/plugins/google/realtime/api_proto.py`).

---

## 5. Voices: catalogue, choosing one, custom voices

### 5.1 The 30 prebuilt voices

Set with `voice_name`. Names are case-insensitive. Style labels are Google's:

| Female (14) | Male (16) |
|---|---|
| Achernar (Soft), Aoede (Breezy), Autonoe (Bright), Callirrhoe (Easy-going), Despina (Smooth), Erinome (Clear), **Gacrux (Mature)**, Kore (Firm), Laomedeia (Upbeat), Leda (Youthful), Pulcherrima (Forward), **Sulafat (Warm)**, Vindemiatrix (Gentle), Zephyr (Bright) | Achird (Friendly), Algenib (Gravelly), Algieba (Smooth), Alnilam (Firm), Charon (Informative), Enceladus (Breathy), Fenrir (Excitable), Iapetus (Clear), Orus (Firm), Puck (Upbeat), Rasalgethi (Informative), Sadachbia (Lively), Sadaltager (Knowledgeable), Schedar (Even), Umbriel (Easy-going), Zubenelgenubi (Casual) |

Gender comes from the Chirp 3 HD table. Measured English F0: Leda about 226 Hz, Kore about 231 Hz, Sulafat about 197 Hz. **Gacrux (about 140-152 Hz) and Pulcherrima (about 135-140 Hz) measured in the male range**, so listen on a real narrowband line before choosing them for a "female agent" persona.

Voices have **no accent and no locale variant**. "Accent comes from the system instruction, not the voice": every voice speaks General American English unless told otherwise. On the first real call, blind judges rated the agent American 6/6. The fix is an accent block in the prompt (see `references/03-conversation-mechanics.md` section 10).

### 5.2 How we chose: blind LLM judging of rendered audio

**Method:**
1. Render the same moments (the greeting, an upset caller, a safety complaint, a Hindi complaint) per voice and per prompt variant, 3 runs each.
2. Save each render as a 24 kHz WAV.
3. Have a multimodal model rate the **audio** blind against a strict, contrast-anchored JSON rubric: dramatic 1-5 (5 = theatrical), professional 1-5, human_like 1-5, local accent 1-5.

The harness is in `references/10-testing-and-evaluation.md`. An earlier un-anchored rubric scored every voice 9-10/10 and separated nothing.

Results (n = 12 renders per cell, means; the "calm" prompt is the revised tone prompt, "deployed" was the earlier prompt):

| Voice | Prompt | dramatic ↓ | professional ↑ | human_like ↑ | Indian accent ↑ |
|---|---|---|---|---|---|
| **Gacrux** (Mature) | calm | **2.58-2.92** | **3.33-3.42** | 4.0-4.17 | 4.08-4.58 |
| Kore (Firm) | calm | 3.08-3.58 | 3.0-3.25 | 4.0-4.08 | 4.42-4.75 |
| Erinome (Clear) | calm | 3.08 | 3.17 | 3.58 | 4.08 |
| Vindemiatrix (Gentle) | calm | 3.5 | 2.67 | 3.83 | 3.92 |
| Sulafat (Warm) | calm | 3.75-3.92 | 2.58-2.83 | 3.92-4.08 | 3.75-4.25 |
| Sulafat (Warm) | deployed (old) | 4.08-4.58 | 2.08-2.42 | 3.92 | 4.17-4.5 |

An earlier Leda vs Sulafat test (English moments, n = 16 each) looked at the `language_code` hint, before the accent block existed:

| Voice | `language_code` | Indian accent | human_like | warmth | words/s |
|---|---|---|---|---|---|
| Leda | none | 2.94 | 3.81 | 4.12 | 4.17 |
| Leda | en-IN | 4.69 | 4.12 | 4.19 | 3.96 |
| Sulafat | none | 3.62 | 4.06 | 4.19 | 3.85 |
| Sulafat | en-IN | 4.88 | 4.12 | 4.31 | 3.79 |

In Hindi and Kannada moments, every voice scored 5/5 accent with or without the hint.

**Decision:** Gacrux ("Mature"). It was the calmest and most professional with a strong local accent. Sulafat was warmer but judged more dramatic. The tone prompt mattered more than the voice: the same Sulafat voice went from dramatic 4.58 to 3.75 when only the prompt changed. **The voice choice is a taste decision for whoever owns the line.** Run the blind test with the owner's own adjectives, then confirm with humans listening on a real phone line.

Known limitation: the model occasionally speaks one turn in a **different voice**, then reverts (Vertex troubleshooting page). There is a community report of Kore switching to a male-sounding speaker for a whole reply (https://discuss.ai.google.dev/t/gemini-3-8-live-prebuilt-voice-kore-intermittently-switches-to-a-different-male-sounding-speaker-for-a-whole-reply/187351). No fix exists. Log it if your QA listens for it.

### 5.3 Custom / replicated voice (allowlist only)

- `speech_config.voice_config.replicated_voice_config = types.ReplicatedVoiceConfig(voice_sample_audio=<bytes>, mime_type=..., consent_audio=<bytes>, voice_consent_signature=...)` (field names checked in SDK 2.21.0).
- **Allowlist only** ("select customers", through your Google account team). You must hold consent and rights for the voice.
- The sample is 10-20 s of clean speech, applied per session with no training step. `setup_complete.voice_consent_signature` is returned so later sessions can skip re-verifying the consent audio.
- **MIME conflict:** the SDK docstring says `audio/wav` at 24 kHz; the Vertex page says `audio/pcm;rate=24000`. Test both. **VERIFY**.
- Recording tips from Google: speak naturally in a quiet room, "smile slightly", and use one of Google's two scripts.
- Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/configure-language-voice

---

## 6. `speech_config.language_code`: a measured trade-off

- **Accepted, not validated, not enforced.** Any string is accepted, including `xx-XX`, so validate the shape yourself (`re.fullmatch(r"[a-z]{2}-[A-Z]{2}", code)`).
- It does **not** force the output language: `hi-IN` with an English question gives an English answer, and `en-IN` with a Hindi question gives a Hindi answer.
- **What the docs say:**
  - Vertex best practices recommend setting it "to maintain consistency", alongside a "RESPOND IN X" line.
  - The Vertex troubleshooting page calls it a hint that reduces unintended switches.
  - The Gemini API says native-audio models "don't support explicitly setting the language code".
- **Community report:** on 3.8, a non-US code made the voice drift into the target language and ignore accent instructions (https://discuss.ai.google.dev/t/bug-gemini-3-8-live-api-overrides-language-code-ignores-system-prompt-accent-formatting/185304).

Measured effect of `en-IN` (blind-judged renders and replayed real calls):

| Effect | Without hint | With `en-IN` |
|---|---|---|
| American-sounding in-call English replies | 5/12 | **0/12** (good) |
| Indian-accent rating, English (before the accent block) | 2.94-3.62 | 4.69-4.88 (good) |
| **English first reply to a Hindi caller** (real Hinglish recordings replayed) | **0/10** | **4/7** (bad) |
| English-only replies in simulated Hindi calls | 2-4% | 6-12% (bad) |

**Decision: off by default.** Language switching was the line owner's main complaint, and the accent was fixed in the prompt instead. Expose it as an environment flag, for example `GEMINI_LIVE_LANGUAGE=en-IN`, validated in config. Use the same hint for the prewarm greeting render as for the call, so the greeting and the call sound alike. If your callers are monolingual, the hint is probably a net gain: measure it on your own recordings.

Note: Pipecat's Gemini Live service sends `language="en-US"` by default as `speech_config.language_code`. Set it deliberately.

---

## 7. Input and output transcription

### 7.1 Input transcription (what the caller said)

- `input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[...], custom_vocabulary=[...])`.
  - `language_codes` takes BCP-47 hints, several allowed. Google says it "reduces the risk of incorrect language detection, especially for short prompts". It had no visible effect in our tests, but it is cheap; set it to the caller languages you expect. Community reports show short or accented utterances transcribed in the wrong language without it.
  - `custom_vocabulary` holds up to 1,000 terms.
  - `mode` is `VERBATIM` (default) or `SMART` (untested).
  - `adaptation_phrases`, `language_hints` and `language_auto` are deprecated. `word_timestamp` and `diarization` are rejected on 3.8.
- **It arrives in one piece at the end of the turn** (at `ACTIVITY_END`), not streamed. `interim_input_transcription` exists in the SDK but never arrived. You get no live captions and no early intent detection. 3.8 input transcripts have no `finished` flag, so don't wait for one.
- Through the LiveKit plugin, the final `user_input_transcribed` event fires at `turn_complete`, i.e. **after** the agent's reply.
- `Transcription.language_code` is **empty** on every input and output transcription. `UserInputTranscribedEvent.language` is not populated on the realtime path. Never drive logic from it.
- Short leading interjections ("Hi,", "Haan ji,") are sometimes missing. After a Hindi turn, English is sometimes transcribed in Devanagari transliteration, and romanised Hindi appears in about 10% of runs. Downstream extraction must tolerate mixed script.
- Transcription tokens are billed at the text-output rate.

### 7.2 Output transcription (what the agent said)

- **Late and lossy:** the first text arrives about 1.7 s after the first audio chunk. In tests, 2/12 transcripts were empty and 1-2/12 were cut off before the last words. Ordering relative to the audio is not guaranteed.
- The LiveKit plugin builds the assistant chat history and captions **from output transcription**, so stored agent turns can be missing or truncated.
- Consequences:
  1. Never signal call control through output text (for example an `<<END_CALL>>` marker); use a tool.
  2. Strip silence placeholders before storing: `re.compile(r"<no speech>|\{pause\}", re.IGNORECASE)`.
  3. For trusted QA, re-transcribe the recording after the call (`references/10-testing-and-evaluation.md`).
  4. Never build business records from it. Capture structured data from tool arguments or a post-call extraction over the full transcript.

### 7.3 `custom_vocabulary` pitfalls (measured)

`custom_vocabulary` biases the recogniser during recognition. Listing the same terms in the system prompt "doesn't produce the same result". It measurably fixes brand spellings and keeps brand words in Latin script inside Devanagari. But **every boosted term pulls nearby audio towards itself**:

| Boosted entry | What went wrong |
|---|---|
| A bare brand word | The caller's farewell word was heard as the brand name. That cancelled a hang-up 5 times. |
| A retailer name | "I bought <variant>" became "<retailer> se order liya tha" ("I ordered it from <retailer>") |
| "batch number" | "this is the best number" became "this is the batch number" |
| A bare variant word one phoneme away from the commonest complaint word | Complaints were transcribed as the variant |

Rules:
- List only words a recogniser would genuinely mangle (brand names, SKUs, coined words), and **as multi-word phrases**, not bare words.
- Never list ordinary words or words that sound like common replies.
- Keep one shared list module if you run several pipelines.
- Tell the model in the prompt to interpret answers against the question just asked: "best number" means "this number".
- Remove pronunciation hints from the prompt. A phonetic spelling of the brand name leaked into speech and transcripts, and Live pronounced the brand correctly without it.

---

## 8. Server VAD: `realtime_input_config` knobs and their measured effect

### 8.1 The knobs (SDK 2.21.0 names)

```python
types.RealtimeInputConfig(
    automatic_activity_detection=types.AutomaticActivityDetection(
        disabled=False,                                   # True = manual activity_start/activity_end
        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,   # or _HIGH
        end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,        # or _LOW
        prefix_padding_ms=100,     # speech needed before start-of-speech is committed (lower = more sensitive)
        silence_duration_ms=500,   # silence needed before end-of-speech
    ),
    activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,  # default (barge-in) | NO_INTERRUPTION
    turn_coverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,          # | TURN_INCLUDES_ALL_INPUT | ..._AND_ALL_VIDEO
)
```

**Defaults are not documented consistently, so treat them as unknown:**
- Docs samples show `LOW/LOW, prefix_padding_ms=20, silence_duration_ms=100` with the comment "disabled: False # default". These are sample values, not documented defaults.
- The Gemini API says "the server's internal default is approximately 800ms" of silence, and recommends 500-800 ms.
- Below 200 ms splits utterances; 2,000 ms or more adds latency.

### 8.2 Measured (speech end to first audio, India to US, Vertex)

| Config | Result |
|---|---|
| **Defaults (nothing set)** | **median about 1.6 s (1.47-1.91 s)** |
| `END_SENSITIVITY_HIGH` + `silence_duration_ms` 250-400 | 1.24-1.32 s in one probe set; **no gain** (1.42-1.93 s) in another. `ACTIVITY_END` moves earlier, but first audio often does not. |
| `END_SENSITIVITY_LOW` + `silence_duration_ms=1500` | 2.43 s |
| Manual activity (`disabled=True`, client sends `activity_end`) | 0.93-1.18 s raw. This shows server VAD costs about 0.4-0.6 s, but manual mode is **broken through the LiveKit plugin** (8.4). |
| Barge-in: caller voice onset to `interrupted=True` | about 0.5-0.58 s (`START_SENSITIVITY_LOW`: about 0.57 s); `turn_complete` follows about 0.12 s later |

**Conclusion:** end-of-speech detection plus the ocean crossing dominate the reply gap, not generation. Server VAD tuning is a **weak and inconsistent lever**. A shorter silence window also risks cutting off callers who pause while reading out codes, addresses or phone numbers. Production kept the defaults and exposed four environment knobs (`LIVE_END_SENSITIVITY`, `LIVE_SILENCE_MS`, `LIVE_START_SENSITIVITY`, `LIVE_PREFIX_PADDING_MS`), all unset. A typo in any of them falls back safely rather than refusing calls. Turn-taking design is in `references/03-conversation-mechanics.md` section 8.

### 8.3 Other options

- **`activity_handling=NO_INTERRUPTION`:** the model talks over the caller. The LiveKit plugin's `interrupt()` becomes a no-op. It can't be toggled mid-session (it needs a reconnect). It is a last resort for a critical disclosure only.
- **`start_of_speech_sensitivity=LOW` with `prefix_padding_ms` 100-200:** makes echo and short backchannels less likely to cut the model off, at the cost of slower barge-in. Measure it on your line.
- **Hybrid VAD (Gemini API docs):** keep server VAD on, run a client VAD, and send `send_realtime_input(audio_stream_end=True)` when it detects the end of speech. "The server treats the audio_stream_end signal as an immediate finalization prompt." It could recover about 0.4-0.6 s. **Not verified on Vertex 3.8.** `audio_stream_end` is valid only with automatic detection enabled.
- **`voice_activity` messages** (`ACTIVITY_START`/`ACTIVITY_END` with `audio_offset`) arrive on 3.8 without `explicit_vad_signal`. `ACTIVITY_END` often arrives **in the same message as the first audio**, so it can't be used alone to measure latency. `server_content.waiting_for_input` means the model expects the caller to continue.

### 8.4 Manual activity detection

- **Setup:** `AutomaticActivityDetection(disabled=True)`. Then call `send_realtime_input(activity_start=types.ActivityStart())`, send the audio, and call `send_realtime_input(activity_end=types.ActivityEnd())`. Each `send_realtime_input` call takes exactly **one** keyword, or the SDK raises `ValueError`. Audio sent with VAD disabled and no markers gets no response.
- **Broken through LiveKit on 3.8 (plugins 1.7.1 and 1.8.3):**
  - 1.7.1 sends `activity_end` plus a `"."` user turn. The model answers the dot first (`"<!-- Silence -->"` or a repeated greeting), the real utterance about 5.6 s later, and later turns go off by one.
  - 1.8.3 drops the dot but sends a bare `turn_complete=True`, with the same result.
  - Raw `activity_end` alone works (1.30 s).
- **Raw bridge:** Google warns that the server then adds no pre-roll and no silence tolerance. Use a client end-of-speech threshold of at least 500 ms and include pre-speech audio. The net gain is about 0-200 ms, plus you must build barge-in yourself. Not recommended.

---

## 9. Context window compression

- The context window is 128k tokens. **Without compression, audio-only sessions are capped at about 15 minutes** (audio plus video at 2 min). Overflow ends with close code 1000/1006, and the model may "hallucinate, slow down" before that.
- Real calls ran to 641 s, so compression is **always on**: `ContextWindowCompressionConfig(sliding_window=SlidingWindow(target_tokens=64_000))`.
  - Optional `trigger_tokens` (5,000-128,000). Google's default is fine; Pipecat triggers at 80% of the window.
  - A lower trigger also caps the per-turn cost (section 12).
- The oldest turns are dropped or summarised, so early-call facts can be lost. Facts you must keep belong in the system prompt, which is not compressed, or in tool state.
- Compression does **not** lift the 10-minute connection limit (section 10).

---

## 10. Session limits, GoAway, resumption, reconnect

### 10.1 Measured limits

| Limit | Value |
|---|---|
| Connection lifetime | about 10 min. **GoAway arrived at about 542 s** with `time_left="30s"` (docs say 60 s before the end). The socket actually closed at about 602 s with `APIError 1011 "The service is currently unavailable."` |
| Session without compression | 15 min of audio |
| Reconnect with a resumption handle | about 2.2-2.35 s; context restored (the model recalled a code word from before the cut) |
| First resumption handle | arrives only with the **second** client message |
| Handle validity | Vertex: "within 24 hours", but also "typically around 10 minutes". Gemini API: 2 h after the last termination. |
| Close code 1007 | Treated by the LiveKit plugin as "context exhausted": fatal, not retried |

### 10.2 Raw bridge pattern

1. Set `session_resumption=SessionResumptionConfig(handle=last_handle)`. On every `session_resumption_update` with `resumable=True`, store `new_handle`.
2. On `go_away`, parse `time_left` (a duration string such as `"30s"`). Reconnect at the next model `turn_complete` while your playout is idle, or with about 10 s to spare, whichever comes first.
3. Keep buffering caller audio during the swap and flush it to the new session.
4. On `APIError` 1000/1006/1011, reconnect with the handle, with backoff. Other codes are fatal.
5. **Transparent mode** (`transparent=True`):
   - Number client messages from 1 (0 is the setup message) and buffer them.
   - Prune the buffer using `last_consumed_client_message_index`.
   - On reconnect, resend the unacknowledged messages before anything else.
   - Google's skill `session_manager.md` has the reference logic.
6. Pending tool calls across a reconnect are undefined. Send their responses on the new session and tolerate errors.
7. The Vertex session-page sample uses the wrong key (`session_resumption_config`); the real key is `session_resumption`.

A complete bridge loop with GoAway handling is in `references/07-websocket-media-streams.md`.

### 10.3 LiveKit plugin behaviour and the 1.7.1 replay bug

The plugin always requests resumption and reconnects on GoAway. Its source has the note "TODO: this isn't a seamless reconnection just yet". Audio pushed during the roughly 2.2 s reconnect is queued and flushed afterwards.

**Bug in 1.7.1 (livekit/agents#6000, fixed in 1.8.1):** after resuming with a handle, the plugin also replays the whole local chat history, so the model sees every turn twice after about 9 minutes. If you are pinned to 1.7.1, use this workaround. It wraps `live.connect` and drops the first `turn_complete=False` client content on a resumed connection:

```python
class _ReplaySkippingConnect:
    def __init__(self, inner, resuming): self._inner, self._resuming = inner, resuming
    async def __aenter__(self):
        session = await self._inner.__aenter__()
        if self._resuming:
            original = session.send_client_content
            async def first_send(*args, **kwargs):
                session.send_client_content = original
                if kwargs.get("turn_complete") is False:   # the history replay is the first client content
                    return None
                return await original(*args, **kwargs)
            session.send_client_content = first_send
        return session
    async def __aexit__(self, *exc): return await self._inner.__aexit__(*exc)

def skip_replay_on_resume(live):            # live = genai_client.aio.live
    connect = live.connect
    def resume_aware_connect(*, model, config):
        r = config.session_resumption
        return _ReplaySkippingConnect(connect(model=model, config=config), bool(r is not None and r.handle))
    live.connect = resume_aware_connect
# call it in a RealtimeSession subclass __init__, before the first connect:
#   skip_replay_on_resume(self._client.aio.live)
```

Test the "call over 9 minutes" case explicitly before launch.

---

## 11. Changing things mid-session

| Change | What happens on 3.8 | Rule |
|---|---|---|
| **Tools** (`update_tools`) | LiveKit: `mutable_tools=False`, so it **reconnects** (about 2.2 s of dead air). Raw SDK 2.32: `send_context_update(tools=...)` (Vertex `BidiGenerateContentContextUpdate`) invalidates the prefix cache and is untested on 3.8. | Register every tool before `session.start` / connect |
| **Voice / temperature / tool behaviour** (`update_options`) | Reconnect | Set once |
| **Instructions** (`update_instructions`) | Vertex via the plugin: the **whole new instruction text is sent as a `role="model"` content turn** (no reconnect). An 88k-char prompt became about 21k tokens of "things the agent said". If the socket isn't connected yet, it forces a restart. | Build the final prompt, with every per-call value, **before** `session.start` |
| **Late facts** | A `role="model"` turn with `turn_complete=False` is honoured. A `role="system"` content turn is only partly honoured, and the LiveKit plugin drops it with a warning. | Short `update_chat_ctx` note or `send_client_content(turns=Content(role="model", ...), turn_complete=False)` |
| **History seeding** | Works without `HistoryConfig`: `send_client_content(turns=[...], turn_complete=False)`, then a final turn with `turn_complete=True`. `turn_complete=True` interrupts any current generation. | Used for greeting context and test harnesses |

The SDK docstring cautions that interleaving `send_client_content` with realtime audio "is not recommended". In practice it works for greeting kick-offs and context injection.

---

## 12. Pricing and cost drivers

Prices per 1M tokens on 21 Oct 2026. Vertex PayGo (non-global) and the Gemini API paid tier list the same prices.

| Item | Price | Approx. per minute |
|---|---|---|
| Input text | $0.75 | |
| Input audio | $3.00 | about $0.005/min |
| Output text (responses, reasoning, **transcription text**) | $4.50 | |
| Output audio | $12.00 | about $0.018/min |
| Input image/video | $1.00 | |
| Search grounding (Gemini API) | 5,000 free/month, then $14 per 1,000 | |

Sources: https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing and https://ai.google.dev/gemini-api/docs/pricing. Check them again before quoting a client.

**The big cost driver.** Google: "Users are charged for all tokens present in the Session Context Window during that turn ... Tokens from past turns are re-processed and billed in every new turn." Context caching is not supported on 3.8, and `cached_content_token_count` came back null.

- **Measured:** `input_tokens` per turn grew 61, 456, 719, 823, 1,256 over five turns. With an 88k-character prompt (about 21.2k tokens), about 21k text tokens were charged **every turn**.
- **So cost per call ≈ (prompt tokens × turns) + accumulated audio.** Example: a 20k-token prompt over 30 turns is 600k text-input tokens, about $0.45 per call for the prompt alone. A 9.5k-token prompt halves that.
- **Prompt size also costs latency.** Connect went from 2.2 s (tiny prompt) to 2.67-3.05 s (21k tokens). Time to first audio: 6.6k tokens 927 ms, 65.8k tokens 1,384 ms, 114.8k tokens 1,695 ms.
- **Levers, in order:**
  1. A lean prompt (aim under about 10-15k tokens; production went from 94k to about 38-41k characters).
  2. Compression with a modest trigger.
  3. Short agent turns.
  4. Keep silence free (it already is).
- **Billing behaviour:** input audio is billed while the model listens; output audio only when it speaks.
- **Reading usage in LiveKit:** read it from the typed `session_report.model_usage`, **not** `to_dict()`. `to_dict()` uses `exclude_defaults`, which strips the usage `type` discriminator, and every call then reported zero cost.

---

## 13. Quotas and capacity

- **Concurrent sessions:** the Vertex session page says **1,000 concurrent sessions per project on PayGo**. The older reference page says "5,000 concurrent sessions per project, 4M tokens per minute". **These conflict**: check the Quotas page of your project and request an increase before launch.
- **Provisioned Throughput:**
  - No mid-session spillover; spillover to PayGo happens between sessions.
  - The enforcement window is 45 s (1-9 GSU) or 5 s (10+ GSU).
  - Burndown multipliers: audio in ×4, audio out ×16, text out ×6. Accumulated session memory counts toward quota every turn.
- **Priority PayGo:** not supported for Live.
- **Bridge sizing:** one process per call is fine up to a few hundred concurrent calls. The project session quota is usually the bottleneck.

---

## 14. Known 3.8 issues and mitigations

| Issue | Mitigation | Evidence |
|---|---|---|
| **No audio** with `thinking_budget=0` / `MINIMAL` | Never send `thinking_config` | LIVE 3/3 |
| **Silent audio turns:** chunks are all-zero or empty while `output_transcription` has the full sentence, and `turn_complete` is often missing. Some users report 8-23 of 29-49 turns, on Vertex and the Gemini API. | Detect it: output transcript text but near-zero PCM energy for more than about 1.5 s. Recover with `send_client_content(turns=..., turn_complete=True)` to regenerate. Some users fell back to 3.1 or 2.5. Not seen in our production. | https://discuss.ai.google.dev/t/gemini-3-8-live-model-replies-with-transcription-but-no-audio/186220 |
| Speech accelerates in turns longer than about 60 s | Keep turns short (under about 100 words / 50 s) and hand the turn back | Vertex troubleshooting |
| Digits read back wrong | Prompt: read sequences one digit at a time, in groups | Vertex |
| Unexpected language switch | Prompt rules (`references/03-conversation-mechanics.md` section 9); `language_code` only after measuring | LIVE |
| Accent instructions followed less consistently than on 3.1 | A dedicated accent block; avoid using `language_code` purely for accent | community reports 184558 and 185304 |
| Voice changes for one turn | None (rare) | Vertex; community |
| Redundant tool "sweeps" (2-5 back-to-back calls) | Informative tool results plus a retry cap in the prompt | Vertex |
| Pronunciation degrades with large tool declarations or prompts (in one report "Juan" became "John" in 5-10 of 20 sessions) | One-sentence tool descriptions; few tools | https://github.com/googleapis/python-genai/issues/3120 |
| Model claims actions it didn't do ("I have registered your complaint" with no tool) | Gate commitments in the prompt; make every side effect a tool | LIVE |
| The model narrates notes aloud ("The user hasn't...") | Prompt guardrail; plain 3.8, not extended-thinking | LIVE |
| Output transcript late and lossy | Section 7.2 | LIVE |
| Echo of the greeting triggers a phantom reply on PSTN | Hold caller audio during the greeting (`references/03-conversation-mechanics.md` section 3) | LIVE 24/24 reproduced |
| Pipecat docs say Vertex Live doesn't support NON_BLOCKING tools; Vertex docs say non-blocking is the default | Don't make hang-up or transfer semantics depend on blocking. Test on your project. | conflict, **VERIFY** |

---

## 15. Ephemeral tokens (client-side apps only)

These are not used for phone bridges, where the server holds the credentials. They are for a browser or mobile app that connects to the Gemini API directly. Vertex Live is server-to-server only.

```python
import datetime as dt
now = dt.datetime.now(tz=dt.timezone.utc)
token = client.auth_tokens.create(config={
    "uses": 1,
    "expire_time": now + dt.timedelta(minutes=30),
    "new_session_expire_time": now + dt.timedelta(minutes=1),
    "live_connect_constraints": {"model": "gemini-3.8-live", "config": {"response_modalities": ["AUDIO"]}},
    "http_options": {"api_version": "v1alpha"},
})
# client side: use token.name as the API key; wire method ...BidiGenerateContentConstrained
```

- Defaults: new sessions are allowed for 60 s, messages for 30 min. Both must be under 20 h. `uses=0` means unlimited, and resumption doesn't count as a use.
- `live_connect_constraints` locks the setup, so the system prompt stays on the server.
- The SDK says ephemeral support is experimental and v1alpha-only, while the docs say v1beta. If one fails, try the other (**VERIFY**).
- Reconnects (every 10 min) must happen within `expire_time`, using the same token.
- Source: https://ai.google.dev/gemini-api/docs/live-api/ephemeral-tokens

---

## 16. Config validation checklist

Validate everything the provider accepts silently, and fail **safe** (log it, then use a fallback pipeline or the defaults) rather than refusing calls:

- [ ] `model` is exactly `gemini-3.8-live` (not `-extended-thinking`, not the plugin default).
- [ ] `location` is in `{us, eu, us-central1}`, and the fallback location differs from the primary.
- [ ] `voice` is in the 30-name list (compare case-insensitively).
- [ ] `language_code`, if set, matches `^[a-z]{2}-[A-Z]{2}$`. Off by default.
- [ ] VAD knobs: sensitivities in `{"", HIGH, LOW}`; ms values are non-negative integers. Collect parse errors rather than raising them in prewarm.
- [ ] No `thinking_config`, `enable_affective_dialog`, `proactivity`, `max_output_tokens`, `history_config`, TEXT modality, `speaking_rate` or `pitch`. Pin this with a unit test that builds the real config object.
- [ ] Compression on. All tools registered before connect, with flat schemas.
- [ ] LiveKit/plugin/SDK versions pinned with `==` and asserted by a test.
- [ ] **A live probe in the deploy gate:** connect, speak one line, and assert the audio has non-zero energy. Unit tests can't catch "accepted but silent" settings. A text-to-speech option once passed every offline test and made every call silent.

---

## 17. Sources

Vertex AI (Gemini Enterprise Agent Platform), fetched 21 Oct 2026:
- 3.8 Live model card: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-live
- Developer guide: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-8-live
- Migration from 2.5: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/migrate-from-gemini-2-5-to-gemini-3-8-live
- Sessions: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session
- Language and voice: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/configure-language-voice
- Capabilities: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/configure-gemini-capabilities
- Best practices: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/best-practices
- Troubleshooting: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/troubleshooting
- API reference: https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/live-api
- Provisioned Throughput: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput/live-api

Gemini API: https://ai.google.dev/gemini-api/docs/live-api (and `/capabilities`, `/session-management`, `/tools`, `/best-practices`, `/ephemeral-tokens`), https://ai.google.dev/api/live

LiveKit Gemini plugin: https://docs.livekit.io/agents/models/realtime/gemini.md

SDK source: google-genai 2.21.0 `google/genai/{live.py,types.py,_live_converters.py,_api_client.py}`; livekit-plugins-google 1.7.1 `livekit/plugins/google/realtime/{realtime_api.py,api_proto.py}`.
