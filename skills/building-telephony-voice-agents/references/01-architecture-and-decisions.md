# 01. Architecture and decisions

Read this first when you are deciding how phone calls should reach a Gemini Live voice agent. It covers how calls arrive (SIP trunk, WebSocket media stream, or WebRTC), which runtime to run the agent on (LiveKit Cloud, self-hosted LiveKit, a raw Gemini WebSocket bridge, or Pipecat), the data flow for each, realtime versus cascaded pipelines, region and latency planning (including India callers reaching Gemini in the US), cost drivers, and a production component checklist. Details live in sibling files: LiveKit Cloud in `04-livekit-cloud.md`, self-hosting in `05-livekit-self-hosted.md`, carriers in `06-sip-trunking-providers.md`, media-stream protocols in `07-websocket-media-streams.md`, and codecs and resampling in `08-audio-formats-and-conversion.md`.

Versions this file was checked against:
- Production deployment: livekit-agents 1.7.1, Gemini `gemini-3.8-live` on Vertex AI, LiveKit Cloud.
- Docs and installed source: livekit-agents 1.8.8 (current on 2026-10-21) and google-genai 2.21.0 / 2.32.0.

## Contents

1. [The three questions](#1-the-three-questions)
2. [Decision tree](#2-decision-tree)
3. [How calls arrive: SIP vs WebSocket stream vs WebRTC](#3-how-calls-arrive)
4. [Runtime choice](#4-runtime-choice)
5. [Data flows](#5-data-flows)
6. [Realtime (Gemini Live) vs cascaded STT-LLM-TTS](#6-realtime-vs-cascaded)
7. [Regions and latency](#7-regions-and-latency)
8. [Cost drivers](#8-cost-drivers)
9. [Production component checklist](#9-production-component-checklist)

---

## 1. The three questions

Answer these in order. Each answer narrows the next.

1. **What does the carrier offer, and where are the callers?** SIP trunking, a WebSocket media stream, or both. For Indian numbers, which providers can legally terminate and anchor media in India (see `12-india-telephony-compliance.md`).
2. **Which runtime owns the call?** LiveKit (Cloud or self-hosted) turns phone calls into room participants. A bridge or Pipecat handles the provider's WebSocket directly.
3. **Which model pipeline answers?** Gemini Live speech-to-speech (realtime), a cascaded STT, LLM and TTS chain, or realtime with an automatic cascaded fallback.

## 2. Decision tree

```text
Start
 |
 +-- Need browser/app callers as well as phone, multi-party rooms, warm transfer to humans,
 |   egress recording, or already on LiveKit?
 |     yes -> LiveKit runtime (go to "LiveKit: Cloud or self-hosted?")
 |     no  -> continue
 |
 +-- Does the carrier offer a SIP trunk (Elastic SIP, Zentrunk, ExoTrunk/vSIP, Telnyx SIP
 |   Connection, operator SIP via SBC)?
 |     yes -> SIP trunk -> LiveKit SIP is the default production path
 |     no (stream only: Twilio <Connect><Stream>, Plivo <Stream>, Exotel Voicebot, carrier CPaaS,
 |         Telnyx/Vonage streaming)
 |          -> Twilio and you want LiveKit?          -> LiveKit Cloud Twilio Connector (section 3.3)
 |          -> one provider, want minimal stack?     -> raw Gemini WebSocket bridge (07, templates/ws_bridge)
 |          -> several providers, want batteries?    -> Pipecat (serializers per carrier)
 |          -> must keep a LiveKit agent anyway?     -> custom WS -> LiveKit room bridge (07)
 |
 +-- Provider offers only SIP REGISTER (common with Indian PBX-style trunks)?
 |     -> put Asterisk/FreeSWITCH in between (registers upstream, peers with LiveKit by IP/digest);
 |        LiveKit SIP does not support REGISTER. See 06.
 |
LiveKit: Cloud or self-hosted?
 +-- Default: LiveKit Cloud (managed agent hosting, SIP, BVC noise cancellation, observability).
 +-- Self-host only for: data-residency or on-prem mandates, very high volume where per-minute
 |   platform fees dominate, or air-gapped networks. Budget for UDP/NAT/TURN/Redis/SIP ops (05).
 +-- Hybrid: self-hosted agent workers against LiveKit Cloud transport is common and keeps
     Cloud SIP + noise cancellation.

Model pipeline?
 +-- Default: Gemini Live realtime (lowest latency, native voice, server VAD).
 +-- Keep a cascaded fallback in the same image if the line must never go silent (section 6).
```

## 3. How calls arrive

### 3.1 Comparison

| Dimension | SIP trunk -> LiveKit SIP | Provider WebSocket stream -> bridge | WebRTC (browser/app) -> LiveKit |
|---|---|---|---|
| Who terminates RTP | LiveKit SIP (Cloud or `livekit/sip`) | The provider; you get JSON/binary frames over TCP | LiveKit SFU |
| Codec at your edge | G.711 PCMU/PCMA 8 kHz (G.722 if carrier supports it; Twilio does not) | Usually mu-law 8 kHz; some offer L16 16 kHz (Plivo, Telnyx, Vonage) or PCM 8/16/24 kHz (Exotel) | Opus 48 kHz |
| Who resamples | LiveKit (decode to a 48 kHz Opus track; RoomIO resamples for the model) | Your bridge (see `08-audio-formats-and-conversion.md`) | LiveKit |
| Barge-in | Server VAD interrupts; LiveKit stops playout | You must flush local queue **and** send the provider's `clear` | Same as SIP |
| Transfer | SIP REFER (`TransferSIPParticipant`), warm transfer task | Provider REST call update or close the socket into the next flow step | n/a |
| DTMF | RFC 4733 in and out | Usually inbound only (`dtmf` events) | n/a |
| Recording | Egress or agent-side recorder; carrier trunk recording | Provider recording or record in the bridge | Egress |
| Outbound | `CreateSIPParticipant` via outbound trunk | Provider "make call" API whose answer URL starts the stream | n/a |
| Ops | Trunks, auth, region pinning, dispatch rules, SIP debugging | Public `wss://` with valid TLS, sticky long-lived connections, pacing, reconnects | Tokens, client SDK |
| Best for | Production contact centre, transfers, multi-agent rooms | Fast prototypes, stream-only providers, minimal hop count | Web/app voice |

Sources: https://docs.livekit.io/telephony.md, provider docs cited in `06` and `07`.

### 3.2 Rules of thumb

- **SIP is the default for production.** LiveKit's own guidance for Twilio is "Elastic SIP Trunking is the recommended approach" (https://docs.livekit.io/telephony/connectors/twilio.md).
- **Use a media stream when SIP is unavailable or slow to provision**, or when you want no LiveKit bill. Expect to own pacing, `clear`, resampling and reconnection.
- **Twilio ConversationRelay is the wrong product for Gemini Live.** It is text in and text out with Twilio-side STT/TTS. It discards Gemini's native voice, server VAD and audio understanding. Use Media Streams (`<Connect><Stream>`) or Elastic SIP (see `07`).
- **Twilio is not a domestic India option.** It sells only +91 800 international toll-free numbers that "must be answered outside of India", and outbound calls to India use non-Indian CLIs. Use Exotel, Plivo (India region) or a licensed operator for Indian DIDs (see `12`).

### 3.3 Twilio Connector (LiveKit Cloud only)

LiveKit Cloud can ingest Twilio Media Streams without SIP. Call `ConnectTwilioCall`, get back a `connect_url`, and return `<Connect><Stream url="<connect_url>"/></Connect>` from your TwiML webhook. The call joins a room as a `CONNECTOR` participant, and agent dispatch works as usual. Plivo, Exotel and others have no such connector. Full details are in `04-livekit-cloud.md` section 11.

## 4. Runtime choice

| Runtime | Choose when | You give up / you own |
|---|---|---|
| **LiveKit Cloud** (managed SFU, SIP, agent hosting) | Default for SIP telephony; fast to production; BVC telephony noise cancellation; observability, recordings and transcripts for 30 days; `lk agent deploy` | ~$0.01/min agent + ~$0.003-0.004/min SIP (plus models). The Gemini realtime plugin takes turn control away from LiveKit (turn detector, endpointing and interruption settings are inert) |
| **LiveKit self-hosted** (`livekit-server` + `livekit/sip` + Redis + optional egress) | Residency/on-prem mandates, very high volume, or private networks | Redis HA, TLS/TURN, UDP firewalls and NAT, SIP public IP, agent autoscaling and draining. No BVC/Krisp without your own license, no LiveKit Inference, no managed agent hosting or phone numbers. See `05` |
| **Raw Gemini WebSocket bridge** (FastAPI + google-genai) | One stream provider, minimal dependencies, full control of GoAway/resumption, echo gating, barge-in and pacing; easiest place to debug 3.8 quirks | About 400 lines you own: audio conversion, pacing, `clear`, reconnects, hang-up and transfer through provider REST. No SIP. See `07` and `templates/ws_bridge/` |
| **Pipecat** (`pipecat-ai[google,websocket,runner]`) | Same WebSocket path with batteries: serializers for Twilio, Plivo, Exotel, Telnyx, Vonage; auto hang-up; easy to add STT/TTS stages | Framework defaults to override for 3.8 (language `en-US` sent by default, no GoAway handling found in 1.13.0 source, NON_BLOCKING tool caveats on Vertex). Server VAD emits no user-speaking frames unless you add a local VAD |
| **Google ADK** (`run_live`) | Multi-agent orchestration and MCP tools on Live | Little telephony focus; pair with LiveKit or a bridge |

Source for the Pipecat and ADK rows: pipecat-ai 1.13.0 source and the Vertex Live docs (https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api).

## 5. Data flows

### 5.1 SIP trunk -> LiveKit Cloud -> agent -> Gemini Live (production-proven)

```text
Caller (PSTN/mobile)
  -> carrier -> provider DID (G.711 A-law/u-law, 8 kHz)
  -> provider SIP trunk, origination URI sip:<subdomain>.<region>.sip.livekit.cloud;transport=tcp
  -> LiveKit SIP: decode G.711 -> 48 kHz Opus room track; caller becomes participant kind=SIP
  -> inbound trunk (matches the dialled number) -> dispatch rule (one room per call,
     room_config.agents=[{agent_name}])
  -> agent worker (LiveKit Cloud, same region as SIP media)
       prewarm: Silero VAD, greeting rendered in the Live voice, Live reachability probe
       entrypoint: wait for SIP participant -> caller number -> build prompt -> AgentSession
       RoomIO input 16 kHz/50 ms + BVCTelephony -> RealtimeModel.push_audio
  -> Gemini Live (Vertex, wss to aiplatform.us.rep.googleapis.com), server VAD owns turns
  <- 24 kHz PCM16 -> room track -> LiveKit SIP -> G.711 8 kHz -> caller
Hang-up: session.shutdown(drain=True) + RoomOptions(delete_room_on_close=True) -> room deleted -> SIP BYE
Post-call (on_session_end, caller already gone): transcript, extraction, CRM, recording upload
```

Template: `templates/livekit/agent_gemini_live.py`. Trunk and dispatch JSON: `templates/sip/`.

### 5.2 SIP trunk -> self-hosted LiveKit

```text
Carrier --SIP 5060 udp/tcp, RTP 10000-20000/udp--> livekit/sip (host network, public IP)
            |  psrpc over Redis
            v
       livekit-server (7880 WS behind TLS LB, 7881/tcp, 50000-60000/udp, TURN)
            ^
            |  outbound wss (no inbound ports)
       agent workers (python agent.py start) --wss--> Gemini Live
       egress (optional, Chrome/GStreamer) --> GCS/S3
```

See `05-livekit-self-hosted.md` and `templates/selfhost/`.

### 5.3 Provider media stream -> raw bridge -> Gemini Live

```text
Caller -> provider (Twilio/Plivo/Exotel/Telnyx/Vonage/carrier CPaaS)
       -> WSS JSON frames (mu-law 8k base64, or L16/PCM) -> your FastAPI bridge
            inbound: decode -> resample to 16 kHz PCM16 -> send_realtime_input(audio)
            outbound: 24 kHz PCM16 -> resample to provider rate -> encode -> paced 20 ms frames
            interrupted=True -> flush local queue + provider "clear"
       -> WSS -> Gemini Live (Vertex or Gemini API)
Hang-up/transfer: provider REST (Twilio, Plivo, Telnyx, Vonage), socket close into the next flow
step (Exotel), or an in-band message (some carriers). See 07.
```

### 5.4 Twilio Media Streams -> LiveKit Twilio Connector -> agent

```text
Twilio number -> your TwiML webhook -> ConnectTwilioCall(room, agents=[...]) -> connect_url
  -> <Connect><Stream url="connect_url"/> -> LiveKit Cloud (participant kind=CONNECTOR)
  -> same agent as 5.1 (Gemini realtime plugin)
```

### 5.5 Provider stream -> custom bridge -> LiveKit room

```text
Provider WSS -> bridge joins the room with livekit.rtc (AudioSource publishes caller audio,
AudioStream subscribes to agent audio) -> same LiveKit agent
```

This is custom code with no LiveKit documentation behind it. Prefer SIP or the Twilio Connector.

### 5.6 WebRTC client -> LiveKit -> agent

Use this for web and app testing of the same agent. It is also how `lk agent console` and LiveKit's playground exercise the agent without a phone. Phone-specific behaviour (8 kHz narrowband, line echo, SIP attributes) is not reproduced, so do at least one real call before release (see `10-testing-and-evaluation.md`).

## 6. Realtime vs cascaded

### 6.1 Comparison (measured in a production deployment, India callers, Gemini in US)

| | Gemini Live realtime (`gemini-3.8-live`) | Cascaded STT -> Gemini text -> TTS |
|---|---|---|
| End of speech -> first audio | **1.2-1.6 s typical**, occasional 2-3.5 s | p50 **3.17 s**, p90 3.98 s over 196 real turns (STT finalisation ~1.38 s, LLM TTFT ~1.22 s, TTS TTFB ~0.16 s) |
| Turn-taking | Gemini server VAD owns turns. LiveKit turn detector, endpointing, adaptive interruption, `min_words`, `false_interruption_timeout` are inert | Full LiveKit control (turn detector, endpointing, backchannel handling) |
| Voice | 30 prebuilt Live voices; accent comes from the prompt; prosody native | Any TTS voice (e.g. Chirp 3 HD); fixed, predictable pronunciation |
| `session.say(text)` | Not supported without a TTS (`supports_say=False`); `say(text, audio=frames)` works | Works |
| Transcripts | Input transcript arrives once at end of turn; output transcript is lossy and ~1.7 s late | STT transcripts are first-class and streamed |
| Control signals | Use tools (e.g. `end_call`); never text markers | Text markers possible but tools still better |
| Half-cascade (Live understands, separate TTS speaks) | Impossible on Vertex 3.8: `response_modalities` TEXT is rejected with 1007 | n/a |
| Cost shape | Per-turn re-billing of the whole context (text and audio tokens) | STT per minute + LLM tokens + TTS characters |
| Typical failures | Silent audio turns, language drift, echo phantom replies, reconnect gaps | Slow turns, backchannels cutting agent off, STT language mistakes |

Sources: production measurements (see `13-troubleshooting-and-lessons.md`); Vertex model card https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-live.

### 6.2 When to pick which

- **Realtime by default** for conversational phone agents. The latency gap is roughly 2x.
- **Cascaded** when you need strict scripted wording (regulatory disclosures), a specific TTS voice or pronunciation lexicon, a model that Live does not offer, deterministic per-word transcripts, or `say()` of fixed text.
- **Realtime with cascaded fallback** when a greeting followed by silence is unacceptable. Production design that held up:
  1. One image, both pipelines, selected by a flag (`AGENT_PIPELINE=realtime|cascaded`).
  2. Per-process prewarm probe: open a Live session in the primary region, then the fallback region. Whichever renders the greeting becomes the process's Live location. If both fail, that process answers on cascaded.
  3. In-call `RealtimeModelFallbackAdapter([live@us, live@us-central1])` so a failed connect or reconnect swaps region instead of dropping the call (the plugin does not retry a failed first connect).
  4. Config typos in realtime tuning fall back to cascaded instead of refusing calls. Only missing hard requirements refuse a call.
  5. Everything after the call (transcript, extraction, CRM, QA, recording) is pipeline-agnostic, because both produce the same session report `chat_history`.
  6. Flipping the pipeline secret is a ~30 s rollback lever that needs no rebuild.

Model configuration details are in `02-gemini-live-model-and-config.md`. Turn-taking and greeting mechanics are in `03-conversation-mechanics.md`.

## 7. Regions and latency

### 7.1 Where Gemini Live runs

- `gemini-3.8-live` on Vertex is served from multi-regions **`us`** and **`eu`** and region **`us-central1`** only. `global`, `asia-south1` and others return close code `1008 Publisher model ... was not found` (verified live; https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-live).
- From India, session connect takes ~1.46-1.6 s to `us`, ~1.64-1.74 s to `eu`, and ~2.1-2.3 s to `us-central1`. **Per-turn latency is the same** in all three, so use `us` with `us-central1` as fallback.
- **Data residency:** no India region exists for Live, so caller audio is processed in the US or EU. Raise this with the customer explicitly (DPDP and sector rules).

### 7.2 Where everything else runs

Keep the **agent next to the SIP media, not next to Gemini.** For Indian callers:

| Hop | Place it | Why |
|---|---|---|
| Carrier and SIP termination | India (`<subdomain>.india.sip.livekit.cloud`, outbound `destination_country: "in"`) | TRAI media anchoring; LiveKit's Plivo guide says India numbers must use the pinned endpoint |
| Agent worker | LiveKit Cloud `ap-south` (Mumbai) or a Mumbai VM | Keeps real-time UDP media short; the ocean crossing moves onto a TCP WebSocket to Gemini |
| Gemini Live | Vertex `us` (fallback `us-central1`) | Only regions where 3.8 is served |

A US-hosted agent saves no per-turn latency, moves the long hop onto lossy UDP media, and conflicts with India region pinning.

### 7.3 Latency budget (mouth to ear, measured and estimated)

| Segment | Typical |
|---|---|
| PSTN + carrier, each way | 60-150 ms |
| Carrier -> LiveKit SIP | 20-50 ms |
| SFU -> agent | 30-70 ms |
| Agent buffering | 0-50 ms |
| Agent (India) -> Gemini US, one way | ~115-140 ms |
| Gemini endpointing + first audio (server VAD) | ~1.4-1.75 s |
| **Total, caller stops -> hears reply** | **~1.6-2.2 s** |

Notes:
- Server VAD end-of-speech plus the India-US hop dominate, not generation. Manual `activity_end` measured 0.93-1.18 s on the raw API, but manual activity is broken through the LiveKit plugin on 3.8 (see `03`).
- Prompt size adds latency: time to first audio grew from 927 ms (6.6k-token prompt) to 1,384 ms (65.8k) and 1,695 ms (114.8k), and connect from ~2.2 s to ~3 s with a ~21k-token prompt (see `02-gemini-live-model-and-config.md` section 12).
- Cold start on LiveKit Cloud's free plan adds 10-20 s before the agent joins (callers hear ringing). See `04` section 6.
- Mid-call reconnects (GoAway at ~9 min, or any tool change) cost ~2.2 s.

## 8. Cost drivers

Prices as fetched on 2026-10-21. Re-check before quoting.

| Driver | Rate | Notes |
|---|---|---|
| Gemini Live input audio | $3.00 / 1M tokens (~$0.005/min) | Vertex and Gemini API paid tier |
| Gemini Live output audio | $12.00 / 1M tokens (~$0.018/min of agent speech) | Speech output is ~25 tokens/s |
| Gemini Live input text | $0.75 / 1M tokens | **The system prompt is re-billed every turn**; context caching is not supported on 3.8 |
| Context re-processing | all tokens in the session window, every turn | Measured `input_tokens` per turn grew 61 -> 456 -> 719 -> 823 -> 1,256 over five turns. Compression (`sliding_window`) caps growth |
| LiveKit Cloud agent minutes | $0.01/min after included (Ship 5,000; Scale 50,000) | https://livekit.com/pricing |
| LiveKit third-party SIP minutes | $0.004/min (Ship), $0.003/min (Scale) after included | Carrier minutes are separate |
| Noise cancellation (voice isolation) | $0.0012/min after included minutes | Check the current line items |
| Egress (audio-only transcode) | $0.004/min after included | Agent-side recording avoids it |
| Carrier | per-minute trunk or programmable-voice rates, DID rental | Programmable-voice streams usually cost more than raw trunk minutes |

Worked example (inferred, not measured): a 20k-token prompt over 30 turns is 600k text input tokens, about $0.45 per call for the prompt alone. Keep the prompt lean (the production prompt was cut from 94k to ~40k characters) and measure real usage from the session report's typed `model_usage` (not `to_dict()`, which strips the usage type; see `11-deployment-ops-and-integrations.md`).

## 9. Production component checklist

Telephony
- [ ] Carrier and DID chosen for the callers' country; KYC done; trial limits lifted (a lapsed trial makes callers hear "busy" before the call reaches LiveKit).
- [ ] SIP trunk (or stream) configured with the region-pinned endpoint, TCP/TLS transport, and auth (digest, IP allow-list or number match).
- [ ] Inbound trunk `numbers` match exactly what the carrier sends (E.164 with `+`).
- [ ] Dispatch rule: one room per call, `room_config.agents[].agent_name` equals the worker's `agent_name`, `hide_phone_number` chosen deliberately.
- [ ] Outbound trunk, caller ID, AMD and retry policy, if outbound is in scope.
- [ ] Transfer path tested (REFER enabled at the carrier, or a REST transfer).

Agent
- [ ] Explicit dispatch (`agent_name` set), versions pinned exactly (the image installs from `pyproject.toml` or `requirements.txt`, not the lock file).
- [ ] Model, region and fallback region validated in config; settings that break 3.8 are never sent (see `02`).
- [ ] Prewarm under the hard 10 s `initialize_process_timeout`; network warm-up on a background thread.
- [ ] Greeting that plays while Gemini connects; caller audio held (not dropped) during the greeting, with barge-in.
- [ ] `end_call` tool returning `None`, grace close that re-checks caller speech at commit time, `delete_room_on_close=True`.
- [ ] Silence monitor and maximum call duration.
- [ ] Context compression and session resumption on any call that may exceed ~9 minutes.
- [ ] Caller number read from SIP attributes and stashed per job for post-call use.

Audio
- [ ] Noise cancellation (BVCTelephony on Cloud), one method only.
- [ ] RoomIO input 16 kHz / 50 ms frames for Gemini; `aec_warmup_duration=None` with the hold design.

Operations
- [ ] Minimum warm capacity (a paid LiveKit plan or always-on workers) so the first caller after idle does not wait 10-20 s.
- [ ] Recording governance decided (consent, retention, redaction, residency) before turning recording on.
- [ ] Post-call pipeline in `on_session_end` with explicit timeouts and retries sized to the 300 s budget.
- [ ] Provider errors emitted as their own metric (per-turn latency alone is survivor-biased).
- [ ] Readiness check in the deploy gate (config errors, live provider probe, region-pinned URLs).
- [ ] Acceptance call: provider logs, LiveKit session, room and SIP participant, agent audio, clean hang-up, recording and transcript uploaded.
- [ ] Regulatory checklist for the market (India: see `12`).
