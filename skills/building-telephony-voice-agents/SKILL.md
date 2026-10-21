---
name: building-telephony-voice-agents
description: Builds, connects and tunes phone-call voice agents on Google Gemini Live (gemini-3.8-live, native audio) using LiveKit (Cloud or self-hosted open source) or a direct WebSocket bridge, reached over SIP trunking or provider WebSocket media streams (Twilio, Exotel, Plivo, Telnyx, Vonage, or any carrier's stream). Covers greeting and pre-greet, barge-in and echo, silence and hang-up, tool calling, VAD and turn-taking, multilingual callers, voices and tone, background audio, audio formats and resampling, prompting, testing with simulated and replayed calls, deployment, CRM hand-off and India telephony rules. Use when designing, building, debugging or evaluating an AI agent that answers or places phone calls, wiring a phone number or carrier to an AI voice model, or choosing between SIP and WebSocket streaming. Not for general LiveKit concepts with no phone or Gemini angle (use LiveKit's own agent skills) or for text chatbots.
license: MIT
---

# Building telephony voice agents on Gemini Live

A field guide for putting a native-audio model on a phone line, distilled from a production deployment
(Gemini 3.8 Live + LiveKit Cloud + Indian SIP trunk, Hindi/English/Kannada callers) and from current docs.
Facts were verified on 2026-10-21 against: livekit-agents 1.7.1 (production) and 1.8.8 (docs), google-genai
2.21.0/2.32.0, `gemini-3.8-live`. APIs here move fast — anything marked **VERIFY** in a reference was not
confirmed; re-check versions before relying on exact names (see "Look things up" below).

## Step 1 — pick the path

```
How do calls reach you?
├─ Carrier offers a SIP trunk (Plivo Zentrunk, Twilio Elastic SIP, Exotel vSIP, Telnyx, Vonage, enterprise SIP)
│     → LiveKit SIP  ──┬─ want managed hosting, Krisp/BVC noise cancellation, phone numbers → LiveKit Cloud (04)
│                      └─ must self-host (data residency, cost at scale, air-gapped)        → LiveKit OSS + livekit/sip (05)
├─ Carrier offers only a WebSocket media stream (Exotel Voicebot applet, Plivo/Twilio/Telnyx/Vonage streams, carrier CPaaS such as Airtel IQ)
│     ├─ Twilio Media Streams + LiveKit Cloud   → LiveKit Twilio Connector (04)
│     ├─ one carrier, full control, lowest hops → direct Gemini Live WebSocket bridge (07, 08, templates/ws_bridge)
│     └─ many carriers, batteries included      → Pipecat with its serializers (01)
├─ Trunk only supports SIP REGISTER (common for Indian PBX-style trunks) → put Asterisk/FreeSWITCH in front of LiveKit (06)
└─ Browser/app callers (WebRTC) → LiveKit rooms directly (04/05)
```
Full decision guide with trade-offs, latency and cost: `references/01-architecture-and-decisions.md`.
India numbers: Twilio cannot provide domestic Indian DIDs — use Indian carriers (see `references/12-india-telephony-compliance.md`).

## Step 2 — build checklist (in order)

1. **Model config** — `gemini-3.8-live` on Vertex (`us`/`eu`/`us-central1` only) or the Gemini API; region failover; voice;
   transcription on. Read the never-send list first. → `references/02-gemini-live-model-and-config.md`
2. **Runtime** — LiveKit agent (`templates/livekit/agent_gemini_live.py`) or WS bridge (`templates/ws_bridge/`).
3. **Telephony** — SIP trunk + inbound trunk + dispatch rule (`templates/sip/`, `references/06-sip-trunking-providers.md`) or stream
   config per carrier (`references/07-websocket-media-streams.md`). Get audio formats right (`references/08-audio-formats-and-conversion.md`).
4. **Conversation mechanics** — pre-rendered greeting, caller-audio hold during greeting, barge-in, silence
   check-ins, guarded hang-up, tools (blocking vs non-blocking), VAD, background/thinking audio, transfer, DTMF.
   → `references/03-conversation-mechanics.md` (the most important file).
5. **Prompt** — persona → rules → guardrails; language-from-first-word; one question per reply; gated commitments;
   plain calm tone. → `references/09-prompting-voice-agents.md`
6. **Test** — unit invariants, moment probes, simulated callers, replay of real recordings, blind voice judging.
   → `references/10-testing-and-evaluation.md`, `templates/testing/`
7. **Ship & operate** — pinned deps, warm replicas, health probe, per-call events, recordings, post-call extraction
   to CRM. → `references/11-deployment-ops-and-integrations.md`
8. **When something breaks** → `references/13-troubleshooting-and-lessons.md` (symptom → cause → fix).

## Settings that silently break or hurt a Gemini Live call

| Do NOT | What happens |
|---|---|
| Disable thinking (`thinking_budget=0` / `thinking_level=MINIMAL`) | Accepted, then no audio at all (transcript only) |
| Send `enable_affective_dialog=True` or `proactivity` on 3.8 | Affective dialog: session rejected (1007). Proactivity: ignored on Vertex, error on the Gemini API. Both are always on |
| Set a small `max_output_tokens` | Accepted, then the turn never completes (audio tokens count against it) |
| Use a non-AUDIO response modality | Rejected (1007); no half-cascade on Vertex |
| Set `history_config(initial_history_in_client_content=True)` | The greeting trigger gets no reply |
| Use `livekit-plugins-google` 1.8.2–1.8.4 with `vertexai=True` + `gemini-3.8-live` | `ValueError` at startup; use 1.7.1 or ≥1.8.5 (or the `publishers/google/models/…` path form) |
| Return a string from an `end_call` tool (LiveKit) | Agent stuck "thinking", line never hangs up — return `None` |
| Call `session.shutdown()` without `RoomOptions(delete_room_on_close=True)` | Caller left on a silent open line |
| Stream caller audio to Gemini while your greeting plays on PSTN | Gemini answers the greeting's echo ("phantom caller") — hold and replay instead |
| Leave LiveKit's `aec_warmup_duration` at its 3 s inbound-SIP default with the hold design | Caller's first words replaced by silence — pass `None` |
| Rely on `allow_interruptions=False` with a server-VAD realtime model | Downgraded with a warning (raises on `Agent`); Gemini still barges in |
| Put fixed English sample sentences in the prompt | Copied verbatim, sometimes into other-language calls |
| Set `speech_config.language_code` to fix accent | Fixes English accent but pulled Hindi callers into English (4/7 vs 0/10) — measure |
| Big prompts | Whole context is re-billed every turn; also slows connect and first audio |
| Resample 24 kHz → 8 kHz with `audioop.ratecv` or naive decimation | Aliasing: ratecv 4.5–15 dB SNR vs 82–84 dB for a stateful FIR (`references/08-audio-formats-and-conversion.md`) |
| Send a display label to a CRM dropdown (e.g. Zendesk) | Silently dropped — send the option tag |

## Defaults that worked in production

- Model: `gemini-3.8-live` on Vertex `us`, fallback `us-central1`; context compression always on (64k target); server VAD knobs unset; `language_code` off.
- Latency: ~1.2–1.6 s caller-stop → first audio typical (recordings: median 1.6 s, p90 3.2 s); server VAD hangover ≈ 0.4–0.6 s of it.
- Greeting rendered once at worker prewarm (doubles as a health probe), verified, played from cache; Gemini speaks it only on cold start.
- During the greeting: hold caller audio; ≥0.8 s continuous local-VAD speech = real barge-in (echo bursts measured ≤0.70 s; real speech detected in 0.15 s) → stop greeting, replay held audio from 0.6 s before speech start; otherwise discard after a 0.4 s echo tail.
- Hang-up: goodbye → `end_call` (returns None) → wait for playout → 4 s grace (cancel if caller speaks, decided at commit time) → delete room / provider hangup; suppress anything the model says after the goodbye.
- Silence: check-in at 20 s, final warning at 40 s, close at 60 s; total call cap 900 s.
- Local Silero VAD (min speech 0.1 s, min silence 0.25 s) only for user-state/timers/greeting hold; Gemini's server VAD owns turns.
- Tone: ordinary pace, plain repeated acknowledgements, one brief apology, no exclamations; emotional wording makes native audio sound dramatic.

## Look things up instead of guessing

- LiveKit docs MCP: `https://docs.livekit.io/mcp`; telephony index `https://docs.livekit.io/telephony/llms.txt`.
- LiveKit's own skills (general agent building, testing, ops): `github.com/livekit/agent-skills` — use them for non-telephony LiveKit questions.
- Gemini Live: Vertex docs `docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/…`; Google skills `github.com/google/skills` (`cloud/gemini-live-api`), `github.com/google-gemini/gemini-skills` (`gemini-live-api-dev`).
- Twilio docs MCP `https://mcp.twilio.com/docs`; Pipecat skills `github.com/pipecat-ai/skills`.
- Installed source beats docs: check `site-packages/livekit/plugins/google/realtime/realtime_api.py` and `google/genai/live.py` for exact names.

## Reference map

| File | Read when |
|---|---|
| `references/01-architecture-and-decisions.md` | Choosing SIP vs stream, LiveKit Cloud vs OSS vs raw bridge vs Pipecat; regions, cost |
| `references/02-gemini-live-model-and-config.md` | Configuring the model: regions, voices, language, transcription, VAD knobs, resumption, pricing |
| `references/03-conversation-mechanics.md` | Greeting, echo, barge-in, silence, hang-up, tools, VAD, multilingual, voices/tone, background audio, transfer |
| `references/04-livekit-cloud.md` | Deploying agents and SIP on LiveKit Cloud; egress; Twilio Connector; API versions |
| `references/05-livekit-self-hosted.md` | Running livekit-server + livekit/sip + egress yourself |
| `references/06-sip-trunking-providers.md` | Pointing a carrier trunk at LiveKit; per-provider steps; PBX bridge; outbound; transfer; DTMF |
| `references/07-websocket-media-streams.md` | Twilio / Exotel / Plivo / Telnyx / Vonage stream protocols, mapping an NDA-only carrier protocol, and a generic adapter |
| `references/08-audio-formats-and-conversion.md` | μ-law/L16, 8/16/24 kHz, resampling, frame sizes, pacing (tested numbers) |
| `references/09-prompting-voice-agents.md` | Writing and debugging the system prompt |
| `references/10-testing-and-evaluation.md` | Probes, simulated callers, recording replay, voice judging, metrics |
| `references/11-deployment-ops-and-integrations.md` | Deploy/rollback, monitoring, recordings, post-call extraction, CRM |
| `references/12-india-telephony-compliance.md` | Indian numbers, media anchoring, TRAI/DoT A2P rules, consent |
| `references/13-troubleshooting-and-lessons.md` | Something is wrong on a call |

Templates: `templates/livekit/` (agent, Dockerfile, livekit.toml), `templates/sip/` (trunk/dispatch JSON, outbound call),
`templates/selfhost/` (docker-compose, configs), `templates/ws_bridge/` (FastAPI bridge + tested audio module),
`templates/testing/` (simulators, replay, voice A/B, gap measurement). Script: `scripts/audio_selftest.py` (run it).

## Working rules

- Measure before and after every change on real or replayed audio; small-n judges are noisy — compare against a baseline run.
- Change one thing at a time on the live line; keep the previous deploy version for instant rollback.
- Never put credentials in prompts, code or the skill; use the platform's secret store.
- Recording and AI-disclosure rules differ by country — check before go-live (`references/12-india-telephony-compliance.md` for India).
