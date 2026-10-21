# 07 — WebSocket media streams (provider ⇄ your bridge ⇄ Gemini Live)

Purpose: the exact wire protocol for every common "media stream" product: Twilio Media Streams, Exotel
Voicebot/Stream applets and the Connect Voice AI API, Plivo Audio Streams, Telnyx and
Vonage, plus how to map a carrier protocol that is only documented under NDA. For each provider it covers how to turn the stream on, how the handshake is authenticated, every message in
both directions with exact JSON, audio encoding and frame size, barge-in (`clear`), marks, DTMF, hang-up, transfer and
fallback, custom parameters, and the quirks that break real calls. It ends with a side-by-side table and a
provider-adapter interface, so one bridge (`templates/ws_bridge/bridge.py`) can serve every provider.
Audio conversion maths and pacing are in `references/08-audio-formats-and-conversion.md`. SIP alternatives are in
`references/06-sip-trunking-providers.md`. Greeting, barge-in and hang-up behaviour are in
`references/03-conversation-mechanics.md`. Gemini session config is in
`references/02-gemini-live-model-and-config.md`.

Verified 2026-10-21 against the provider docs (served as raw `.md`) linked inline, Exotel's own sample repo
`github.com/exotel/Agent-Stream`, and pipecat-ai 1.13.0 serializers (`pipecat/serializers/*`, a third-party reference
implementation). **VERIFY** marks items that are unconfirmed or where sources conflict.

## Contents
1. [When to use streams vs SIP](#1-when-to-use-streams-vs-sip)
2. [Twilio Media Streams (+ LiveKit Twilio Connector)](#2-twilio-media-streams)
3. [Exotel Voicebot / Stream applets and Connect Voice AI API](#3-exotel-agentstream)
4. [Plivo Audio Streams](#4-plivo-audio-streams)
5. [Carrier stream products documented only under NDA](#5-carrier-stream-products-documented-only-under-nda-eg-airtel-iq)
6. [Telnyx media streaming](#6-telnyx-media-streaming)
7. [Vonage Voice API WebSockets](#7-vonage-voice-api-websockets)
8. [Side-by-side comparison](#8-side-by-side-comparison)
9. [Generic adapter interface (one bridge, all providers)](#9-generic-adapter-interface)
10. [Hosting the endpoint: wss, TLS, scaling](#10-hosting-the-wss-endpoint)

---

## 1. When to use streams vs SIP

| Choose a WebSocket stream when | Choose SIP (`references/06-sip-trunking-providers.md`) when |
|---|---|
| The provider has no usable SIP trunk for you (carrier CPaaS stream products, Exotel flows, Twilio in markets you can't trunk) | You want REFER transfers, RFC 4733 DTMF, PBX integration, many concurrent calls, or you already run LiveKit |
| You want the fewest hops: provider → bridge → Gemini, with no LiveKit bill | You want LiveKit features (BVC noise cancellation, recording/egress, multi-party rooms, Agents framework) |
| A prototype or a single-provider deployment where you own ~500 lines of bridge code | You don't want to own transcoding, pacing, reconnects and barge-in plumbing |
| The provider's flow builder must run before or after the bot (IVR, compliance prompt, agent hand-off applets) | Low-latency barge-in matters and you'd rather not depend on a provider `clear` buffer |

**Costs of the stream path:**
- TCP head-of-line blocking.
- The provider buffers your audio (Twilio "buffered and played in order"; Plivo and Vonage about 60 s), so barge-in
  needs an explicit `clear`.
- You must pace output in real time (`references/08-audio-formats-and-conversion.md` §7).
- You must host a public `wss://` with a CA-signed certificate.

**Benefits:**
- Fewer telecom concepts.
- Wider audio on some providers (Exotel 16/24 kHz, Plivo/Vonage/Telnyx L16 16 kHz). This doesn't change what the
  caller hears (the PSTN is narrowband), but it removes conversions.

**Bridging a stream into LiveKit** (to keep a LiveKit agent):
- Twilio: use the native **Twilio Connector** (§2.8).
- Other providers: write a bridge that joins the room as a participant through `livekit.rtc` (`AudioSource` /
  `AudioStream`). There is no native connector for Plivo, Exotel or other carriers. See `references/01-architecture-and-decisions.md`.

The protocols share a common shape:
1. The provider opens a WS to you.
2. It sends `connected`, then `start` (metadata and IDs).
3. It streams caller audio as base64 frames inside JSON (Vonage uses binary).
4. You send audio back the same way, `mark` to learn when playback finished, and `clear` to flush on barge-in.
5. The provider sends `stop` (or closes) when the call ends.

---

## 2. Twilio Media Streams

Sources: <https://www.twilio.com/docs/voice/twiml/stream.md>,
<https://www.twilio.com/docs/voice/media-streams/websocket-messages.md> (dateModified 2026-10-08),
<https://www.twilio.com/docs/voice/media-streams.md>, <https://www.twilio.com/docs/voice/api/call-resource.md>.
**Region note:** Media Streams are processed in **US1 (default), IE1 and AU1** only. Host the bridge next to them
(us-east-1 for US1).
**India:** no domestic DIDs, and ITFS numbers "must be answered outside of India" (see
`references/12-india-telephony-compliance.md`).

### 2.1 Configure
The number's Voice webhook ("A call comes in" → Webhook, POST) returns TwiML:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start><Recording track="both" channels="dual" recordingStatusCallback="https://bot.example.com/twilio/rec"/></Start>
  <Connect action="https://bot.example.com/twilio/after-bot" method="POST">
    <Stream url="wss://bot.example.com/media/twilio" statusCallback="https://bot.example.com/twilio/stream-status">
      <Parameter name="tenant" value="acme"/>
      <Parameter name="lang" value="en-IN"/>
    </Stream>
  </Connect>
</Response>
```
- **`<Connect><Stream>` is bidirectional**, and it is what an AI agent needs. It blocks the TwiML that follows until
  the WS closes. `<Start><Stream>` is a one-way fork (listen-only), and "DTMF isn't supported with unidirectional
  streams".
- `url`: "`wss` is the only supported protocol", and it "*does not* support query string parameters". Pass data with
  `<Parameter>` (each name+value under 500 chars), which arrives in `start.customParameters`.
- A bidirectional stream receives **only the inbound track**. Only one bidirectional stream per call.
- Limits: 4 h call duration, 64 KB TwiML, 60 s RTP timeout, outbound 1 CPS by default.
- `statusCallback` receives `StreamEvent` = `stream-started|stream-stopped|stream-error`, plus `StreamError`.
- Start recording **before** `<Connect>`, because nothing after `<Connect>` runs until the WS closes. Alternatives are
  `Record=true` on Create Call, or the Recordings REST API.

**ConversationRelay is not for Gemini Live.** It is text in / text out with Twilio's own STT and TTS, which throws away
native audio (voice, prosody, server VAD, affective audio). Use Media Streams.

### 2.2 Handshake and auth
- Twilio signs the WSS upgrade with **`X-Twilio-Signature`**: HMAC-SHA1 keyed with your Auth Token over the URL (plus
  sorted POST params, none for WS), base64-encoded. "You must also configure your application to validate the
  `X-Twilio-Signature` header."
- Twilio connects from "any public IP address", so you **can't IP-allowlist**.
- Twilio's WSS note says: "try appending a trailing `/` character to the URL". Which scheme string is signed is
  **VERIFY**, so validate against all four candidates:
  ```python
  from twilio.request_validator import RequestValidator
  def twilio_ws_ok(auth_token: str, host: str, path: str, sig: str) -> bool:
      v = RequestValidator(auth_token)
      cands = [f"{s}://{host}{p}" for s in ("wss", "https") for p in (path, path.rstrip("/") + "/")]
      return any(v.validate(u, {}, sig) for u in cands)
  ```
  The bridge template implements this without the `twilio` package.
- On a shared bridge, also check `start.accountSid`. Validate the HTTP voice webhook too (same header).

### 2.3 Twilio → you
`sequenceNumber`, `chunk` and `timestamp` are **strings**.
```json
{"event":"connected","protocol":"Call","version":"1.0.0"}
{"event":"start","sequenceNumber":"1","streamSid":"MZ…",
 "start":{"accountSid":"AC…","streamSid":"MZ…","callSid":"CA…","tracks":["inbound"],
          "mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1},
          "customParameters":{"tenant":"acme","lang":"en-IN"}}}
{"event":"media","sequenceNumber":"4","streamSid":"MZ…",
 "media":{"track":"inbound","chunk":"2","timestamp":"5","payload":"no+JhoaJjpzS..."}}
{"event":"dtmf","streamSid":"MZ…","sequenceNumber":"5","dtmf":{"track":"inbound","digit":"1"}}
{"event":"mark","sequenceNumber":"6","streamSid":"MZ…","mark":{"name":"turn-3"}}
{"event":"stop","sequenceNumber":"7","streamSid":"MZ…","stop":{"accountSid":"AC…","callSid":"CA…"}}
```

### 2.4 You → Twilio
```json
{"event":"media","streamSid":"MZ…","media":{"payload":"<base64 mu-law 8 kHz, no header>"}}
{"event":"mark","streamSid":"MZ…","mark":{"name":"turn-3"}}
{"event":"clear","streamSid":"MZ…"}
```
- **Audio:** `audio/x-mulaw`, 8000 Hz, mono, base64, "should not contain audio file type header bytes". The format is
  fixed (no L16 or 16 kHz option).
- **Frame size:** Twilio says "the audio can be of any size". Inbound frames are 20 ms / 160 B in practice (observed,
  not documented). Send 160 B (20 ms) frames paced in real time with a 100–200 ms lead.
- **`mark`:** Twilio echoes it "when the audio ends (or if there is no audio buffered)".
- **`clear`:** "empties all buffered audio and causes any `mark` messages to be sent back". Marks echoed right after a
  clear tell you which turns never played.
- **DTMF out is not supported** on the stream ("Sending DTMF outbound … is not supported"). Use REST `<Play digits>`
  instead.

### 2.5 Hang-up, transfer, fallback
| Goal | Preferred | Alternative |
|---|---|---|
| Hang up | `POST /2010-04-01/Accounts/{AC}/Calls/{CA}.json` with `Status=completed` | Close the WS with no `<Connect action>` and no TwiML after `</Connect>` |
| Goodbye then hang up | Send goodbye audio + `mark`; on the mark echo, REST `Status=completed` | REST `Twiml=<Response><Say>…</Say><Hangup/></Response>` (max 4000 chars) |
| Transfer to a human | Close the WS. Twilio POSTs the `<Connect action>` URL, which returns `<Response><Dial callerId="+1…">+1…</Dial></Response>` (or `<Dial><Sip>`, `<Enqueue>`) | REST `Twiml=<Response><Dial>…</Dial></Response>`, which replaces the TwiML and kills the stream |

"The only way you can stop a bidirectional Stream … is to end the call" (or update the call with new TwiML).

### 2.6 Outbound calls
```bash
curl -X POST "https://api.twilio.com/2010-04-01/Accounts/$AC/Calls.json" -u "$AC:$TOKEN" \
  --data-urlencode "To=+14155550100" --data-urlencode "From=+14155550199" \
  --data-urlencode 'Twiml=<Response><Connect><Stream url="wss://bot.example.com/media/twilio"><Parameter name="campaign" value="c42"/></Stream></Connect></Response>' \
  -d "MachineDetection=Enable" -d "AsyncAmd=true" --data-urlencode "AsyncAmdStatusCallback=https://bot.example.com/twilio/amd" \
  --data-urlencode "StatusCallback=https://bot.example.com/twilio/status" -d "StatusCallbackEvent=answered" -d "StatusCallbackEvent=completed"
```
- `From` must be a Twilio number or a Verified Caller ID.
- `Timeout` default is 60 (max 600).
- With `MachineDetection` and **no** `AsyncAmd`, TwiML is held until detection finishes. Use `AsyncAmd=true`.

### 2.7 Quirks
- No query strings in `url`. Everything goes through `<Parameter>`.
- There's no India or Singapore media region, so an India caller → US1 → bridge → Gemini adds an ocean crossing.
- Only one bidirectional stream per call. Up to 4 unidirectional tracks, shared with SIPREC, Real-Time Transcription
  and AMD; `both_tracks` counts as 2.
- India specifics: "Voice OTP/2FA is not permitted"; "RFC 2833 DTMF: Yes (Not guaranteed)".

### 2.8 LiveKit Twilio Connector (Media Streams straight into a LiveKit room)
Source: <https://docs.livekit.io/telephony/connectors/twilio.md>.
- Call `ConnectTwilioCall` to get a `connect_url`, then return `<Connect><Stream url="<connect_url>"/></Connect>`.
  LiveKit bridges the stream into a room as a `CONNECTOR` participant and dispatches your agent.
  ```python
  resp = await lkapi.connector.connect_twilio_call(api.ConnectTwilioCallRequest(
      twilio_call_direction=api.ConnectTwilioCallRequest.TWILIO_CALL_DIRECTION_INBOUND,  # nested enum (livekit-api 1.2.2)
      room_name="call-<CallSid>", agents=[api.RoomAgentDispatch(agent_name="my-agent")], destination_country="US"))
  ```
- For outbound, create the Twilio call with `Url` = `connect_url` with `wss` replaced by `https`.
- LiveKit Cloud only. Audio is still 8 kHz µ-law at Twilio. Full details (fields, participant kind `CONNECTOR`): `references/04-livekit-cloud.md` section 11.
- LiveKit says "Elastic SIP Trunking is the recommended approach". DTMF, transfer and recording through the connector
  are undocumented (**VERIFY**).

---

## 3. Exotel AgentStream

Sources: <https://developer.exotel.com/docs/agentstream/websocket-protocol.md>,
<https://docs.exotel.com/exotel-agentstream/voicebot-applet.md>,
<https://docs.exotel.com/exotel-agentstream/connect-voice-ai-api.md>,
<https://docs.exotel.com/exotel-agentstream/agentstream-what-to-use-when.md>,
<https://docs.exotel.com/exotel-agentstream/agentstream-wss-errors-and-handling.md>. Sample code:
<https://github.com/exotel/Agent-Stream>.
Media and signalling run in **AWS Mumbai/Hyderabad**, so host the bridge in **ap-south-1**.

### 3.1 Four ways to start a bot session
| Front door | Use for | How |
|---|---|---|
| **Voicebot applet** in an App Bazaar flow (inbound) | Inbound ExoPhone with IVR/compliance before and agent hand-off after | Flow: Call Start → (Greeting) → **Voicebot** → **Passthru** → Connect / Hangup; assign the flow to the ExoPhone |
| **Connect Voice AI API** (outbound, bot only) | Outbound bot calls without a flow | `POST https://api.in.exotel.com/v1/Accounts/{sid}/Calls/connect` with `From`, `CallerId`, `StreamUrl=wss://…`, `StreamType=bidirectional` |
| **Connect-to-Flow** (outbound + flow) | Outbound with menus / disclosure / hand-off | Same endpoint with `Url=http://my.exotel.com/{sid}/exoml/start_voice/{flow_app_id}`; "Do not pass StreamUrl or StreamType" here |
| **ExoML Legs** (code-owned) | Full programmatic control | `POST https://cpaas-api.in.exotel.com/v2/accounts/{sid}/legs` + gRPC leg events + `POST …/legs/{leg_sid}/actions/start_stream {"direction":"bidirectional","url":"wss://…","content_type":"audio/x-mulaw;rate=8000"}` (**VERIFY** that µ-law is selectable here) |

**Connect Voice AI API details:**
- Optional parameters: `Record`, `RecordingChannels` (`single|dual`), `TimeLimit` (≤14400), `CustomField` (≤128
  chars), `StatusCallback`, `StatusCallbackEvents[]` (`answered|terminal|ringing`), `StreamName` (≤32).
- "The full StreamUrl (including any query string) must be under 600 characters."
- "This feature must be enabled on your account before use."
- "When your bot wants to end the call, it closes the WebSocket connection."
- "Not for: Menus, disclosure before bot, bot→human".
- Base URLs: Mumbai `https://api.in.exotel.com`, Singapore `https://api.exotel.com`. HTTP Basic with API key : API
  token.

### 3.2 Configure the Voicebot applet
1. **URL:** a static `wss://bot.example.com/media/exotel?sample-rate=16000`, or an `https://` **dynamic URL**.
   - Exotel GETs the dynamic URL with `CallSid, CallFrom, CallTo, Direction, Created, DialWhomNumber, From, To,
     CurrentTime`, and you reply `200`, `Content-Type: application/json`, `{"url":"wss://…"}`.
   - The dynamic URL arrives before the WS, so use it to pre-warm the Gemini session.
2. **Auth:** either **IP allowlisting** (get Exotel's ranges from hello@exotel.com), or **Basic auth in the URL**:
   `wss://<API_KEY>:<API_TOKEN>@bot.example.com/media/exotel`, which Exotel sends as
   `Authorization: Basic base64(API_KEY:API_TOKEN)`.
3. **Sample rate:** `?sample-rate=8000|16000|24000`, with 8000 the default. "Best Practice: Use 16 kHz". One rate
   applies to both directions. **Use 16000 for Gemini:** inbound passes straight through, outbound needs one 24→16
   resample, and the 3200 B minimum chunk is exactly 100 ms.
4. **Custom parameters:** "Maximum number of custom params that are allowed is 3", with the total query ≤256 chars.
   They arrive in `start.custom_parameters`. Pipecat warns that query params can be stripped on dial-out, so keep the
   critical ones in the dynamic URL or your DB keyed by `call_sid`.
5. **Record:** single or dual channel; MP3, MP3 HQ, or RAW (HQ and RAW need a flag). The URL arrives in the next
   Passthru as `Stream[RecordingUrl]`.
6. **Next applet:** "the stream is automatically closed before executing the next Applet".

**Stream applet (unidirectional)** sends `connected/start/media/stop` only. The flow continues immediately. "If used
with a Connect applet that rings multiple agents, audio from all ringing legs is sent." Use the **Voicebot** applet for
an agent.

### 3.3 Exotel → you
From the developer protocol page. The docs.exotel.com copy uses numbers instead of strings for `sequence_number` and
`chunk`, plus `encoding:"raw"`, `bit_rate:"128kbps"`. Parse both and **never branch on `encoding`**.
```json
{"event":"connected"}
{"event":"start","sequence_number":"1","stream_sid":"MZ…",
 "start":{"stream_sid":"MZ…","call_sid":"CA…","account_sid":"AC…",
          "from":"+91XXXXXXXXXX","to":"+91XXXXXXXXXX",
          "custom_parameters":{"key1":"value1"},
          "media_format":{"encoding":"audio/x-raw","sample_rate":"16000","bit_rate":"16"}}}
{"event":"media","sequence_number":"3","stream_sid":"MZ…",
 "media":{"chunk":"2","timestamp":"200","payload":"<base64 PCM16LE>"}}
{"event":"dtmf","sequence_number":"7","stream_sid":"MZ…","dtmf":{"digit":"5","duration":"100"}}
{"event":"mark","sequence_number":"15","stream_sid":"MZ…","mark":{"name":"turn-3"}}
{"event":"stop","sequence_number":"20","stream_sid":"MZ…",
 "stop":{"call_sid":"CA…","account_sid":"AC…","reason":"callended"}}
```
- `stop.reason`: `stopped` (the applet ended) or `callended` (the caller hung up).
- `dtmf` and `mark` are Voicebot only.
- Inbound cadence is "every ~100 ms": 1600 B at 8k, 3200 B at 16k, 4800 B at 24k.

### 3.4 You → Exotel (Voicebot only)
```json
{"event":"media","stream_sid":"MZ…","media":{"payload":"<base64 PCM16LE, n×320 B, ≥3200 B>"}}
{"event":"mark","stream_sid":"MZ…","mark":{"name":"turn-3"}}
{"event":"clear","stream_sid":"MZ…"}
```
- **Audio:** "Raw PCM (linear16)", "16-bit signed, little-endian", mono, base64.
- **Chunk rules (exact):**
  - "Minimum chunk size: 3.2k [100ms data]". At 8 kHz that's 200 ms; the docs and Exotel's own samples disagree,
    and **3200 B is safe under both readings**.
  - "Maximum chunk size: 100k".
  - "Chunk size should always be in multiple of 320 bytes". Otherwise "the last packet will be of lesser size than
    320 Bytes, & platform will wait for 20ms before sending next chunk … audio gaps".
  - Use 3200 B at 8k/16k, or 4800 B (100 ms) at 24k.
- **`clear`:** "the Clear event will only apply to the second media message". The chunk already playing finishes, so
  keep chunks at the minimum and the lead short.
- **No `stop` from the bot.** "There is no explicit Stop event sent by the bot to Exotel". Closing the socket is the
  stop.

### 3.5 Hang-up, transfer, errors
- **Hang up:** close the WS. In a flow, Exotel then emits `stop` (`stopped`) and runs the next applet (Hangup). With
  Connect Voice AI, closing ends the call.
- **Transfer to a human:**
  1. Store the decision in your DB keyed by `call_sid`.
  2. Close the WS.
  3. The **Passthru** (sync) GETs your URL with `CallSid`, `CustomField`, `Stream[StreamSID]`, `Stream[Status]`,
     `Stream[Duration]`, `Stream[RecordingUrl]`, `Stream[DisconnectedBy]`, `Stream[Error]` and
     `Stream[DetailedStatus]`.
  4. Return **`200 OK` for branch A or `302 Found` for branch B**. Passthru only makes a binary decision; the JSON body
     doesn't route.
  5. Branch B → a **Connect applet** with a dynamic URL that returns e.g.
     `{"destination":{"numbers":["+91XXXXXXXXXX"]},"record":true,"max_ringing_duration":30}` or
     `{"destination":{"trunk":"trunk-xxxx"}}` (SIP to a contact centre).
  6. Make the Passthru idempotent: "retries, dedupe on CallSid".
- **DTMF:** inbound only. There is no send-DTMF.
- **Timeouts:**
  - "If the bot server does not respond within 10 seconds, the session will fail."
  - "Exotel will attempt one automatic retry if the WebSocket handshake fails."
  - Sessions run up to 60 min.
  - "Self-signed certs may be rejected."
- **Errors:** WS close 1006 is the common one ("network drop · TLS failure · server crash", or an LB idle timeout).
  Passthru fields `Stream[Error]` and `Stream[DetailedStatus]` (e.g. `Streaming_call_throttled`).
- **Concurrency:** `GET /v1/Accounts/{sid}/ActiveStreams` → XML `ActiveStreamCount` and `ThrottleLimit`.

### 3.6 Exotel quirks (read before coding)
- **snake_case vs camelCase.** The docs and Pipecat use `stream_sid`, `sequence_number`, `custom_parameters`.
  **Exotel's own sample helper sends camelCase** `streamSid` with `media.{payload,chunk,timestamp,sequenceNumber}`,
  and reads both casings. Its comment says: "Exotel examples include timestamp + sequenceNumber; omit them and some
  Connect streams drop / end early."
  - **Do this:** accept both casings inbound and send the documented snake_case.
  - If Connect Voice AI calls end after ~4 s, add `chunk`, `timestamp` and `sequence_number` to outbound media and pace
    in real time (the sample notes "bursting frames with sleep(0) correlates with short ~4s Connect hangups").
  - **VERIFY** with a live capture.
- **Doc contradictions:** the "Updated Extension Guide" lists **Clear** as an Exotel→bot event that "Resets session
  context", and says "Ensure bot sends Stop". The protocol page has neither. Ignore an inbound `clear`, and never send
  `stop`.
- "Low or 'slow' voice almost always means sample-rate mismatch". Read the actual rate from
  `start.media_format.sample_rate`, not your config.
- The Connect Voice AI page says "Exotel sends audio as raw PCM or mulaw at 8 kHz by default", while the applet docs
  say slin only. **VERIFY** if you see µ-law.
- A dynamic-URL GET can come back with mangled param names ("paramName:value="). Parse defensively.

---

## 4. Plivo Audio Streams

Sources: <https://www.plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-guide.md>,
<https://www.plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-reference.md>,
<https://plivo.com/docs/voice/api/calls.md>. India: use an **India data-region** account (media anchoring; see
`references/12-india-telephony-compliance.md`).

### 4.1 Configure
The number's Application Answer URL returns:
```xml
<Response>
  <Stream bidirectional="true" keepCallAlive="true"
          contentType="audio/x-l16;rate=16000"
          statusCallbackUrl="https://bot.example.com/plivo/stream-status" statusCallbackMethod="POST"
          extraHeaders="tenant=acme;lang=hi">wss://bot.example.com/media/plivo</Stream>
  <!-- runs after the stream ends because keepCallAlive="true": hand-off target -->
  <Redirect>https://bot.example.com/plivo/after-bot</Redirect>
</Response>
```
- `contentType`: `audio/x-mulaw;rate=8000` (default, and Plivo's recommendation), `audio/x-l16;rate=8000`, or
  `audio/x-l16;rate=16000`. **Use L16 16k for Gemini**: input needs no conversion, and output is one 24→16 resample.
- `keepCallAlive="false"` (the default) ends the call when the stream ends. Set `true` if XML must run after the bot.
- Status callbacks are `started`, `stopped` and `failed`.
- REST alternative: `POST https://api.plivo.com/v1/Account/{auth_id}/Call/{call_uuid}/Stream/` starts or stops a
  stream on a live call.
- **Limits:** 1 stream per call; 64 KB max WS message; about 60 s playback buffer; outbound chunks ≤16 KB base64
  recommended.
- **`<Redirect>` after the stream** (**VERIFY**): `<Redirect>` is a standard Plivo XML verb, but chaining it after
  `<Stream keepCallAlive="true">` isn't shown on the streaming pages. Test it, or use the REST transfer in §4.5.

### 4.2 Handshake and auth
- Plivo signs requests with **`X-Plivo-Signature-V3`** + **`X-Plivo-Signature-V3-Nonce`** (HMAC-SHA256 with the Auth
  Token). Invalid signatures should be rejected; Pipecat closes with WS code 1008.
- Use Plivo's SDK validator (`plivo.utils.validate_v3_signature(method, uri, nonce, auth_token, v3_signature, params)`)
  — **VERIFY** the exact signature in your SDK version and the URL form for WS upgrades.

### 4.3 Plivo → you
```json
{"event":"start","sequenceNumber":1,
 "start":{"callId":"<uuid>","streamId":"<uuid>","accountId":"MA…","tracks":["inbound"],
          "mediaFormat":{"encoding":"audio/x-l16","sampleRate":16000}},
 "extra_headers":"tenant=acme;lang=hi"}
{"event":"media","sequenceNumber":42,"streamId":"<uuid>",
 "media":{"track":"inbound","timestamp":"1705312200000","chunk":41,"payload":"<base64>"},"extra_headers":"…"}
{"event":"dtmf","sequenceNumber":50,"streamId":"<uuid>","dtmf":{"track":"inbound","digit":"5","timestamp":"1705312250000"},"extra_headers":"…"}
{"event":"playedStream","sequenceNumber":75,"streamId":"<uuid>","name":"turn-3"}
{"event":"clearedAudio","sequenceNumber":80,"streamId":"<uuid>"}
```
- Media arrives in ~20 ms chunks: 160 B µ-law at 8k, 640 B L16 at 16k.
- `extra_headers` is a `k=v;k2=v2` **string** on every event. Parse it once from `start`.

### 4.4 You → Plivo
```json
{"event":"playAudio","media":{"contentType":"audio/x-l16","sampleRate":16000,"payload":"<base64>"}}
{"event":"checkpoint","streamId":"<uuid>","name":"turn-3"}
{"event":"clearAudio","streamId":"<uuid>"}
{"event":"sendDTMF","dtmf":"1234#"}
```
- **`playAudio`'s `contentType` and `sampleRate` must match the `<Stream>` attributes.**
- `checkpoint` is Plivo's mark, answered by `playedStream`. `clearAudio` is the barge-in flush, answered by
  `clearedAudio`.
- Plivo is the only provider here with **DTMF out over the socket** (`sendDTMF`).

### 4.5 Hang-up and transfer (REST)
- **Hang up one call:** `DELETE https://api.plivo.com/v1/Account/{auth_id}/Call/{call_uuid}/`.
  **Danger:** "If `call_uuid` is not specified, all ongoing calls in the account will be disconnected." Guard against
  an empty `call_uuid` in code (the bridge template refuses to call DELETE without one).
- **Transfer:** `POST https://api.plivo.com/v1/Account/{auth_id}/Call/{call_uuid}/` with
  `legs=aleg&aleg_url=<https URL returning <Response><Dial><Number>+91…</Number></Dial></Response>>`.
- Or set `keepCallAlive="true"`, close the WS, and let the next XML verb run.
- Closing the WS with `keepCallAlive="false"` also ends the call.

### 4.6 Quirks
- The default is µ-law 8k even though L16 16k exists. Set `contentType` explicitly.
- IDs are `streamId` and `callId` (camelCase), not `streamSid`.
- A 60 s buffer means an unpaced bridge can queue a whole reply. Always `clearAudio` on barge-in and pace output.

---

## 5. Carrier stream products documented only under NDA (e.g. Airtel IQ)

Some carriers, especially Indian CPaaS products such as Airtel IQ's voice-bot streaming, publish their WebSocket
protocol only in a customer guide shared during onboarding. This skill deliberately does not reproduce those guides.
When you get one, map it onto the generic adapter (§9). Work through it in this order:

1. **Handshake:** who opens the socket (usually the carrier, to a `wss://` URL registered in its portal or call
   flow), how it is authenticated (bearer token, mTLS, signed query, IP allowlist), and whether a failed handshake is
   retried. If it isn't, run at least two instances behind a health check.
2. **Inbound events:** map them to `Event` kinds (`audio`, `dtmf`, `mark`, `hold`/`unhold`, `stop`), and map the
   start event to `CallInfo` (stream id to echo, call id, caller/called numbers, custom parameters).
3. **Audio:** encoding, rate and outbound frame size. µ-law 8 kHz in 20 ms (160 B) frames is the common case:
   reuse `telephony_audio.twilio_codec()` or build a `Codec`.
4. **Control:** the barge-in message (`clear` or similar), the playback acknowledgement (mark/checkpoint), and how
   hang-up and transfer work: REST, socket close, or an in-band message.
5. **Parse defensively:** carrier guides often disagree between their field tables and their example JSON (key
   spellings, numbers as strings, JSON-encoded strings inside JSON). Accept every variant and echo ids exactly.
6. **Certification:** ask for the carrier's go-live checklist early. It usually covers frame size, DTMF, hold,
   mark/clear, hang-up/fallback, load and failover.

---

## 6. Telnyx media streaming

Sources: <https://developers.telnyx.com/docs/voice/programmable-voice/media-streaming.md>,
<https://developers.telnyx.com/docs/voice/programmable-voice/texml-verbs/stream.md>,
<https://developers.telnyx.com/api-reference/call-commands/streaming-start.md>.

### 6.1 Configure
- **Call Control:** on dial (`POST /v2/calls`), answer (`POST /v2/calls/{call_control_id}/actions/answer`) or
  `…/actions/streaming_start`:
  ```json
  {"stream_url":"wss://bot.example.com/media/telnyx","stream_track":"inbound_track",
   "stream_bidirectional_mode":"rtp","stream_bidirectional_codec":"L16","stream_bidirectional_sampling_rate":16000}
  ```
  - `stream_bidirectional_codec`: `PCMU` (default), `PCMA`, `G722`, `OPUS`, `AMR-WB`, `L16`.
  - Sampling rate: 8000, 16000, 22050, 24000 or 48000.
  - `custom_parameters` (name/value); optional `stream_auth_token`.
- **TeXML:** `<Stream url="wss://…" bidirectionalMode="rtp" bidirectionalCodec="PCMU" bidirectionalSamplingRate="8000"/>`
  (`bidirectionalCodec` doesn't list L16), plus `<Parameter>`.
- **Auth:** `telnyx-signature-ed25519` + `telnyx-timestamp` over `{timestamp}|{stream_url}` (verify with your Telnyx
  public key).

### 6.2 Messages
Telnyx → you. Order isn't guaranteed, so use `chunk` and `occurred_at`:
```json
{"event":"connected","version":"1.0.0"}
{"event":"start","sequence_number":"1","stream_id":"<uuid>",
 "start":{"call_control_id":"…","from":"+1…","to":"+1…","media_format":{"encoding":"PCMU","sample_rate":8000,"channels":1}}}
{"event":"media","sequence_number":"4","stream_id":"<uuid>","media":{"track":"inbound","chunk":"2","timestamp":"5","payload":"<base64>"}}
{"event":"dtmf","stream_id":"<uuid>","occurred_at":"…","sequence_number":"7","dtmf":{"digit":"1"}}
{"event":"mark","stream_id":"<uuid>","sequence_number":"8","mark":{"name":"turn-3"}}
{"event":"stop","sequence_number":"9","stream_id":"<uuid>","stop":{"call_control_id":"…"}}
{"event":"error","stream_id":"<uuid>","payload":{"code":100003,"title":"malformed_frame","detail":"…"}}
```
You → Telnyx:
```json
{"event":"media","media":{"payload":"<base64 in the bidirectional codec>"}}
{"event":"mark","mark":{"name":"turn-3"}}
{"event":"clear"}
```
- RTP mode chunks must be **20 ms to 30 s**; one bidirectional stream per call. mp3 mode allows at most 1 message per
  second.
- `clear` "immediately stop[s] the media playing … and clear[s] the media queue"; queued marks are returned.
- **L16 byte order: VERIFY.** RTP L16 is network **big-endian** per RFC 3551, and Telnyx doesn't say. Capture one frame:
  if speech sounds like loud static, byteswap (`np.frombuffer(b, ">i2").astype("<i2")`).
- Hang up: `POST https://api.telnyx.com/v2/calls/{call_control_id}/actions/hangup`. Transfer:
  `POST …/actions/transfer {"to":"+1…"|"sip:…"}` (`to` required). DTMF out: Call Control `send_dtmf`.

---

## 7. Vonage Voice API WebSockets

Sources: <https://developer.vonage.com/en/voice/voice-api/concepts/websockets>,
<https://developer.vonage.com/en/api/voice>.
- **Configure:** the answer-URL NCCO:
  ```json
  [{"action":"connect","endpoint":[{"type":"websocket","uri":"wss://bot.example.com/media/vonage",
    "content-type":"audio/l16;rate=16000","headers":{"tenant":"acme"}}]}]
  ```
  `content-type` is `audio/l16;rate=8000|16000|24000`, one rate for both directions.
- **Auth:** `Authorization: Bearer <JWT>` (`vonage` mode), or a custom value you set.
- **Protocol:**
  - The first text frame is `{"event":"websocket:connected","content-type":"audio/l16;rate=16000", …custom headers}`.
  - Caller audio arrives as **binary frames** of raw 16-bit LE PCM, ~20 ms (640 B at 16k). There's no base64.
  - Send audio back as **binary frames** in the same format, 20 ms each.
  - The buffer is "3072 packets … around 60 seconds".
- **Control** (text frames):
  - `{"action":"clear"}` → `{"event":"websocket:cleared"}`
  - `{"action":"notify","payload":{"name":"turn-3"}}` → `{"event":"websocket:notify","payload":{…}}` (the mark)
  - DTMF in: `{"event":"websocket:dtmf","digit":"5","duration":260}`
- **Hang up:** `PUT /v1/calls/{uuid}` `{"action":"hangup"}`. **VERIFY**: the API reference page showed only
  `transfer`. Closing the WS only fires a `disconnected` webhook.
- **Transfer:** `PUT /v1/calls/{uuid}` `{"action":"transfer","destination":{"type":"ncco","ncco":[…]}}`.
- Best Gemini setting: `audio/l16;rate=16000` (input passthrough), or `24000` (output passthrough).

---

## 8. Side-by-side comparison

| | Twilio | Exotel Voicebot | Plivo | Telnyx | Vonage |
|---|---|---|---|---|---|
| Start | TwiML `<Connect><Stream>` | Voicebot applet; Connect Voice AI API (`StreamUrl`, `StreamType=bidirectional`); Legs `start_stream` | XML `<Stream bidirectional="true">`; REST `/Call/{uuid}/Stream/` | Call Control `stream_url` + `stream_bidirectional_mode:"rtp"`; TeXML `<Stream>` | NCCO `connect` → `websocket` |
| Frames | JSON text | JSON text | JSON text | JSON text | **binary audio** + JSON control |
| Encoding | µ-law b64 (fixed) | PCM16 LE b64 | µ-law (default) or L16 b64 | PCMU/PCMA/G722/OPUS/AMR-WB/**L16** b64 | L16 LE raw |
| Rates | 8000 | 8000 / 16000 / 24000 (`?sample-rate=`) | 8000 µ-law; L16 8000/16000 | 8000–48000 (L16) | 8000 / 16000 / 24000 |
| Inbound frame | ~20 ms / 160 B (observed) | ~100 ms | ~20 ms | numbered chunks | ~20 ms |
| Outbound frame rule | "any size" (send 20 ms) | n×320 B, ≥3200 B, ≤100 KB | ≤64 KB msg; ~16 KB b64 rec. | RTP 20 ms–30 s | 20 ms |
| Stream id key | `streamSid` | `stream_sid` (samples: `streamSid`) | `streamId` | `stream_id` | none (call `uuid` via webhook) |
| Call id | `start.callSid` | `start.call_sid` | `start.callId` | `start.call_control_id` | webhook `uuid` |
| Send audio | `media` | `media` | `playAudio{media:{contentType,sampleRate,payload}}` | `media` | binary frame |
| Interrupt | `clear` (pending marks echoed) | `clear` (playing chunk finishes) | `clearAudio` → `clearedAudio` | `clear` (marks returned) | `{"action":"clear"}` → `websocket:cleared` |
| Mark | `mark` | `mark` | `checkpoint` → `playedStream` | `mark` | `notify` → `websocket:notify` |
| DTMF in / out | in / no (REST `<Play digits>`) | in / no | in / **`sendDTMF`** | in / Call Control | in / API |
| Hang up | REST `Status=completed` | close WS (→ Hangup applet / call ends) | REST `DELETE …/Call/{uuid}/` (**never without uuid**) or close with `keepCallAlive=false` | `…/actions/hangup` | `PUT /v1/calls/{uuid}` hangup (VERIFY) |
| Transfer | close WS → `<Connect action>` returns `<Dial>`; or REST `Twiml` | close WS → Passthru (200/302) → Connect applet | REST `legs=aleg&aleg_url=`; or `keepCallAlive=true` + next XML | `…/actions/transfer {"to"}` | `PUT` transfer NCCO |
| Custom params | `<Parameter>` → `start.customParameters` (<500 chars each) | ≤3 query params (≤256) → `start.custom_parameters` | `extraHeaders="k=v;…"` → `extra_headers` string | `custom_parameters` / `<Parameter>` | NCCO `headers{}` → `websocket:connected` |
| WS auth | `X-Twilio-Signature` HMAC-SHA1; no IP list | Basic auth in URL or IP allowlist | `X-Plivo-Signature-V3` + nonce (HMAC-SHA256) | `telnyx-signature-ed25519` + `telnyx-timestamp` | JWT Bearer or custom |
| Handshake retry | — | 1 retry; 10 s to respond | — | — | — |
| Max session | 4 h call | 60 min | — | — | — |
| Media region | US1/IE1/AU1 | AWS Mumbai/Hyderabad | account region (India region available) | per Telnyx region | per Vonage region |
| Best Gemini setting | µ-law 8k (forced) | `sample-rate=16000` | `audio/x-l16;rate=16000` | L16 16k (VERIFY byte order) | `audio/l16;rate=16000` |

Measured end-to-end conversion quality per provider format (Gemini 24k → wire → 16k loopback, stateful FIR): µ-law
providers ~38 dB (38.8 dB; the G.711 ceiling); Exotel 16k/24k, Plivo L16 16k and Vonage 16k ~85 dB (85.1 dB). See
`references/08-audio-formats-and-conversion.md` §5.

---

## 9. Generic adapter interface

One bridge, many providers: everything provider-specific goes behind a small adapter. The bridge core (Gemini
session, resampling, pacing, barge-in, timers, tools) never sees provider JSON. This is how
`templates/ws_bridge/bridge.py` is built.

```python
@dataclass
class CallInfo:
    stream_id: str                     # echo this in every outbound message
    call_id: str | None = None         # REST call id for hang-up/transfer
    caller: str | None = None
    called: str | None = None
    custom: dict = field(default_factory=dict)
    wire_rate: int = 8000              # actual rate negotiated (Exotel: read from start.media_format)

class Event(NamedTuple):               # normalised inbound event
    kind: str                          # "audio" | "dtmf" | "mark" | "hold" | "unhold" | "stop" | "ignore"
    value: Any = None                  # wire bytes for audio, digit, mark name

class ProviderAdapter(Protocol):
    name: str
    binary_audio: bool                 # Vonage-style binary frames
    def authorize(self, ws: WebSocket) -> bool: ...          # signature / basic / bearer / IP check BEFORE accept
    def parse_start(self, msg: dict) -> CallInfo | None: ... # CallInfo on the start event (raise PermissionError to reject)
    def make_codec(self, info: CallInfo) -> ta.Codec: ...    # wire format, rate and outbound frame size
    def parse(self, msg: dict) -> Event: ...
    def media(self, frame: bytes) -> str | bytes: ...        # one outbound frame
    def mark(self, name: str) -> str: ...
    def clear(self) -> str: ...
    async def hangup(self, call: CallBridge) -> None: ...    # REST, close-WS, or in-band terminate
    async def transfer(self, call: CallBridge, target: str) -> bool: ...
```

**Design rules:**
1. **Normalise IDs at `start`.** Every provider names the stream and call IDs differently. After `parse_start`, the
   core only uses `CallInfo`.
2. **Accept both casings** (Exotel) and every key spelling a guide uses. Parse numbers from strings.
3. **Audio stays in wire format inside the adapter boundary.** The codec (`telephony_audio.Codec`) converts wire ↔
   Gemini PCM. The adapter only does base64 and JSON framing.
4. **Frame size is the adapter's call** (via `make_codec`): Twilio 160 B; Exotel 3200 B (or 4800 B at 24k); Plivo and Vonage
   640 B at 16k.
5. **Hang-up and transfer are capabilities, not events.** Some providers need REST (Twilio, Plivo, Telnyx, Vonage),
   some need a socket close (Exotel), and some carriers use an in-band message. The core calls
   `await adapter.hangup(call)` after the goodbye's `mark` comes back and a short grace window passes.
6. **Barge-in** is: core drops its local queue → `adapter.clear()` → `codec.barge_in()` → `pacer.reset()`. Keep the
   resamplers; they're stateful filters and stay valid.
7. **Authorize before `accept()`.** Reject with close code 1008 (policy violation) on a bad signature.
8. **Unknown events are ignored, never fatal** (Exotel inbound `clear`, Telnyx `error`, Vonage notifications).
9. Add a provider by writing one adapter class and one route. Nothing in the core changes.

---

## 10. Hosting the wss endpoint

- **TLS:** every provider needs `wss://` with a **CA-signed certificate**. Self-signed certs "may be rejected" by Exotel and are
  ruled out by Twilio's `wss` requirement; assume every carrier rejects them.
  - Options: a cloud load balancer with a managed cert (GCP HTTPS LB, AWS ALB), Caddy (automatic Let's Encrypt), nginx
    + certbot, or uvicorn `--ssl-keyfile/--ssl-certfile`. For dev: `ngrok http 8080` or
    `cloudflared tunnel --url http://localhost:8080`.
  - Setup steps: `templates/ws_bridge/README.md`.
- **Proxies:** enable WebSocket upgrade, raise idle timeouts above the longest call (1006 on Exotel is often an LB idle
  timeout), and disable response buffering. nginx needs `proxy_http_version 1.1; proxy_set_header Upgrade
  $http_upgrade; proxy_set_header Connection "upgrade"; proxy_read_timeout 3600s; proxy_buffering off;`.
- **Placement:** run the bridge in the provider's media region (Exotel/Plivo-India/Indian carriers → Mumbai `asia-south1` /
  `ap-south-1`; Twilio US1 → us-east). The single TCP crossing to Gemini (`us`/`eu`) happens once per turn wherever the
  bridge is.
- **Scaling:**
  - One process handles many calls (asyncio). The CPU cost is tiny (60 s of audio converts in ~0.02 s).
  - The real limit is the Gemini Live concurrent-session quota (1,000 per project on Vertex PayGo per the session page;
    another page says 5,000; **VERIFY** in Quotas).
  - Each WS must stay on one instance (no mid-call rebalancing), so drain instances on deploy instead of killing
    them.
- **Availability:** Some carriers don't retry a failed handshake, and Exotel retries once. Run at least two instances behind
  a health check, and keep `/healthz` cheap.
