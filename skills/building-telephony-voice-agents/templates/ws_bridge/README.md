# ws_bridge — telephony media streams ⇄ Gemini Live

One FastAPI process that accepts a provider's **WebSocket media stream** and bridges it to a **Gemini Live** session
(`google-genai`). It supports Twilio Media Streams, the Exotel Voicebot applet / Connect Voice AI, and Plivo Audio
Streams. Other carriers need one more adapter. The protocol details behind every adapter are in
`references/07-websocket-media-streams.md`, and the audio maths is in `references/08-audio-formats-and-conversion.md`.

| File | What it is |
|---|---|
| `bridge.py` | FastAPI app. Provider adapters (`TwilioAdapter`, `ExotelAdapter`, `PlivoAdapter`) behind one `ProviderAdapter` interface. Per-call `CallBridge`: Gemini session with resumption/GoAway, barge-in → provider `clear`, greeting with caller-audio hold, `end_call` → hang-up after the goodbye's mark, optional `transfer_to_human`, silence ladder, max duration, DTMF, provider hold events. |
| `telephony_audio.py` | Tested conversion module: bit-exact numpy G.711 µ-law/A-law, `LowLatencyResampler` (stateful polyphase FIR, the default), `StreamResampler` (soxr), framing, `Pacer`, per-provider `Codec` factories, `EnergyVAD`. |
| `requirements.txt` | Dependencies, with the versions tested. |

**Status:**
- `python -m py_compile` passes on Python 3.12 and 3.14.
- Google-genai API names were checked against the 2.21.0 source.
- An offline smoke test ran each adapter end to end through real uvicorn + websockets with a *fake*
  Gemini Live session. Checks: frame sizes (160 / 3200 / 640 B), barge-in → `clear`, a mark round trip, GoAway →
  reconnect with the resumption handle, `end_call` → post-tool speech suppressed → hang-up, cached greeting + barge-in replay, and the three-stage silence ladder.
- **Not yet run against a live phone call or a live Gemini session.** Do one real call per provider before relying
  on it.

## 1. Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Gemini: Vertex AI (ADC) ...
export GOOGLE_CLOUD_PROJECT=<PROJECT> GOOGLE_CLOUD_LOCATION=us          # gemini-3.8-live: us | eu | us-central1
gcloud auth application-default login
# ... or the Gemini API
# export GEMINI_API_KEY=...

export PUBLIC_HOST=bot.example.com            # the public host providers will reach (wss://PUBLIC_HOST/media/...)
export GEMINI_LIVE_MODEL=gemini-3.8-live GEMINI_VOICE=Gacrux
export GREETING_TEXT="Hello, this is Asha from Example Foods. How can I help you today?"
export SYSTEM_PROMPT_FILE=./prompt.md         # optional; a small demo prompt is built in
uvicorn bridge:app --host 0.0.0.0 --port 8080 --ws-ping-interval 20 --ws-ping-timeout 20
curl localhost:8080/healthz                   # {"ok":true,...,"greeting_cached":true} once the greeting is rendered
python ../../scripts/audio_selftest.py        # conversion self-test (output below)
```

### Environment variables
| Variable | Default | Meaning |
|---|---|---|
| `PUBLIC_HOST` | `bot.example.com` | Host in the wss/https URLs given to providers (also used for Twilio signature checks) |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | — / `us` | Vertex AI. Pre-refreshed ADC credentials, refreshed every 30 min |
| `GEMINI_API_KEY` | — | Gemini API (used when no project is set) |
| `GEMINI_LIVE_MODEL`, `GEMINI_VOICE` | `gemini-3.8-live`, `Gacrux` | Model and prebuilt voice |
| `GEMINI_LANGUAGE_CODE` | empty | Optional `speech_config.language_code`. It can pull other-language callers into that language, so measure first |
| `TRANSCRIPTION_LANGUAGE_CODES` | `en-IN,hi-IN` | Input transcription hints |
| `SYSTEM_PROMPT_FILE` | built-in demo | Prompt file (see `references/09-prompting-voice-agents.md`) |
| `GREETING_TEXT`, `PRERENDER_GREETING` | empty, `1` | Greeting rendered once in the Live voice at startup and verified (≥70% words, ≤3 extra). Played from cache at `start`, otherwise spoken live by Gemini |
| `TRANSFER_NUMBER` | empty | E.164. Enables the `transfer_to_human` tool |
| `OUT_LEAD_MS` | 150 | Max audio queued at the provider ahead of real time |
| `GREETING_BARGE_IN_S`, `GREETING_ECHO_TAIL_S`, `REPLAY_LEAD_S` | 0.8, 0.4, 0.6 | Greeting hold / barge-in / replay (values from a production deployment, where the VAD was Silero) |
| `VAD_THRESHOLD_DBFS`, `GREETING_VAD_BOOST_DB` | -38, 10 | Local **energy** VAD, used for timers, greeting barge-in and grace cancel only. Tune on real calls, or swap in Silero |
| `CLOSE_GRACE_S` | 4.0 | Wait after the goodbye finished playing before hanging up (caller speech cancels) |
| `SILENCE_CHECKIN_S`, `SILENCE_FINAL_S`, `SILENCE_CLOSE_S`, `MAX_CALL_S` | 15, 30, 45, 900 | Silence ladder and call cap |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VALIDATE` | —, —, `1` | REST hang-up/transfer and `X-Twilio-Signature` checks (webhook + WSS upgrade) |
| `EXOTEL_SAMPLE_RATE` | 16000 | Must match `?sample-rate=` on the applet URL (the actual rate is read from `start`) |
| `EXOTEL_BASIC_USER`, `EXOTEL_BASIC_PASS`, `EXOTEL_ALLOWED_IPS` | — | Applet auth: `wss://USER:PASS@host/media/exotel` → `Authorization: Basic`, and/or an IP allowlist |
| `EXOTEL_SEND_SEQ` | `0` | Add `chunk`/`timestamp`/`sequence_number` to outbound media (Exotel sample code says some Connect streams end early without them) |
| `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` | — | REST hang-up (`DELETE …/Call/{uuid}/`, never without a uuid) and transfer |
| `PLIVO_CONTENT_TYPE` | `audio/x-l16;rate=16000` | Must match the `<Stream contentType>` |
| `PLIVO_STREAM_SECRET` | — | Put into `extraHeaders` by `/plivo/answer` and checked at `start` |
| `TRUST_PROXY_IP_HEADER` | — | e.g. `x-forwarded-for`, only behind a trusted load balancer |

Add business tools: append `types.FunctionDeclaration`s to `EXTRA_TOOL_DECLARATIONS` and async implementations to
`TOOL_IMPLS`. Each must return `{"status", "retryable", "message"}`, never `{}`.

## 2. Expose it as `wss://` with a CA-signed certificate

Every provider needs `wss://`. Exotel "may" reject self-signed certificates, and Twilio accepts
`wss` only. Pick one of these:

- **Dev tunnel:** `ngrok http 8080` or `cloudflared tunnel --url http://localhost:8080`. Set `PUBLIC_HOST` to the
  tunnel host. These are fine for Twilio and Plivo tests. Exotel IP allowlisting won't work through a
  tunnel.
- **Caddy** (automatic Let's Encrypt; WebSockets work out of the box):
  ```
  bot.example.com {
      reverse_proxy 127.0.0.1:8080 {
          flush_interval -1
          transport http { read_timeout 2h }
      }
  }
  ```
- **nginx + certbot:**
  ```nginx
  server {
      listen 443 ssl http2;
      server_name bot.example.com;
      ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
      ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;
      ssl_protocols TLSv1.2 TLSv1.3;
      location / {
          proxy_pass http://127.0.0.1:8080;
          proxy_http_version 1.1;
          proxy_set_header Upgrade $http_upgrade;
          proxy_set_header Connection "upgrade";
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-For $remote_addr;
          proxy_read_timeout 3600s;      # longer than your longest call (Exotel 1006 = LB idle timeout)
          proxy_send_timeout 3600s;
          proxy_buffering off;
      }
  }
  ```
- **Cloud load balancer:** GCP HTTPS LB / Cloud Run, or AWS ALB with an ACM cert. Raise the backend/idle timeout
  (Cloud Run request timeout up to 60 min; ALB idle timeout up to 4000 s). Session affinity isn't needed, because one
  WS is one long-lived connection, but **drain** instances on deploy instead of killing live calls.
- **Direct uvicorn TLS:** `uvicorn bridge:app --host 0.0.0.0 --port 443 --ssl-keyfile key.pem --ssl-certfile fullchain.pem`.

**Region:** run the bridge where the provider's media is.
- Exotel, Plivo-India and other Indian carriers: Mumbai (`asia-south1` / `ap-south-1`).
- Twilio US1: `us-east`.

**Capacity:**
- Run at least two instances behind a health check. Some carriers never retry a failed handshake, and Exotel retries
  once.
- CPU is negligible (60 s of audio converts in about 0.03 s). The limit is the Gemini Live concurrent-session quota.

## 3. Configure each provider

**Twilio (Media Streams).** Twilio has no Indian DIDs; see `references/12-india-telephony-compliance.md`.
1. Phone Number → Voice Configuration → "A call comes in" → Webhook **POST** `https://PUBLIC_HOST/twilio/voice`.
   That endpoint validates `X-Twilio-Signature` and returns
   `<Connect><Stream url="wss://PUBLIC_HOST/media/twilio">` with `from`/`to` `<Parameter>`s.
2. Set `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`. Hang-up uses REST `Status=completed`. Transfer uses REST
   `Twiml=<Response><Dial>…</Dial></Response>`.
3. Outbound: Create Call with `Url=https://PUBLIC_HOST/twilio/voice` (or `Twiml=` with the same `<Connect><Stream>`).
   Use `MachineDetection=Enable&AsyncAmd=true` if you need AMD.
4. **VERIFY** on your first call that the WSS signature check passes. The code tries `wss://` and `https://`, with
   and without a trailing `/`. If it fails, set `TWILIO_VALIDATE=0` temporarily and log the header.

**Exotel (Voicebot applet).**
1. App Bazaar → new flow: **Call Start → (Greeting/compliance) → Voicebot → Passthru → Connect / Hangup**. Assign it
   to the ExoPhone.
2. Voicebot URL: either static `wss://USER:PASS@PUBLIC_HOST/media/exotel?sample-rate=16000`, or dynamic
   `https://PUBLIC_HOST/exotel/stream-url` (returns the wss URL with `call_sid`). Set `EXOTEL_BASIC_USER/PASS` to the
   same values, or ask hello@exotel.com for Exotel's IP ranges and set `EXOTEL_ALLOWED_IPS`.
3. Passthru URL: `https://PUBLIC_HOST/exotel/passthru`. It returns `302` when the bot asked for a transfer and `200`
   otherwise. Point the 302 branch at a **Connect** applet whose dynamic URL is `https://PUBLIC_HOST/exotel/connect`
   (returns `{"destination":{"numbers":[…]}}`), and the 200 branch at **Hangup**.
   - `ESCALATIONS` is in-memory. With more than one instance, use Redis or a DB keyed by `CallSid`.
4. Hang-up = the bridge closes the socket, and the flow moves to the next applet.
5. Outbound bot calls without a flow use the Connect Voice AI API (`StreamUrl=wss://…/media/exotel?sample-rate=16000`,
   `StreamType=bidirectional`; it must be enabled on the account). If those calls end after about 4 s, try
   `EXOTEL_SEND_SEQ=1` (**VERIFY**).

**Plivo (Audio Streams).** India: use an India data-region account.
1. Create an Application with Answer URL `https://PUBLIC_HOST/plivo/answer`, and attach the number. The endpoint
   returns `<Stream bidirectional="true" keepCallAlive="false" contentType="audio/x-l16;rate=16000">`.
2. Set `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` and `PLIVO_STREAM_SECRET`.
   - Hang-up is `DELETE …/Call/{call_uuid}/`. The code refuses to call it without a uuid, because without one Plivo
     disconnects *every* call on the account.
   - Transfer is `POST …/Call/{uuid}/` with `legs=aleg&aleg_url=https://PUBLIC_HOST/plivo/transfer-xml?to=…`.
3. `X-Plivo-Signature-V3` validation of the WS upgrade isn't implemented (**VERIFY** Plivo's algorithm for WSS). The
   shared secret in `extraHeaders` is checked instead.

**Other carriers (including protocols documented only under NDA).** Write one adapter class and one route (see
`references/07-websocket-media-streams.md` §5 and §9). Nothing in the core changes.

## 4. Audio self-test output

`scripts/audio_selftest.py`, run 2026-10-21 (Python 3.14.5, numpy 2.5.4, scipy 1.18.1, soxr 1.1.0, audioop-lts 0.2.2):

```text
python 3.14.5  numpy 2.5.4  audioop yes  soxr yes

== 1. G.711 codecs ==
mu-law encode numpy == audioop (all 65536 int16)                         True  ok
mu-law decode numpy == audioop (all 256 codes)                           True  ok
A-law encode numpy == audioop (all 65536 int16)                          True  ok
A-law decode numpy == audioop (all 256 codes)                            True  ok
SNR dB  mu-law 8k encode->decode (G.711 quantisation ceiling)            38.3  ok
SNR dB  A-law 8k encode->decode                                          37.4  ok

== 2. Streaming resamplers, 20 ms chunks, input has out-of-band tones when downsampling ==
SNR dB  LowLatency FIR   8000->16000                                     83.5  ok
SNR dB  LowLatency FIR  24000->8000                                      84.3  ok
SNR dB  LowLatency FIR  24000->16000                                     82.1  ok
SNR dB  LowLatency FIR  16000->24000                                     82.9  ok
SNR dB  soxr HQ          8000->16000                                     80.8
SNR dB  soxr HQ         24000->8000                                      81.4
SNR dB  soxr HQ         24000->16000                                     80.8
SNR dB  soxr HQ         16000->24000                                     81.2
SNR dB  audioop.ratecv   8000->16000                                     15.2
SNR dB  audioop.ratecv  24000->8000                                      4.5
SNR dB  audioop.ratecv  24000->16000                                     7.7
SNR dB  audioop.ratecv  16000->24000                                     27.3

== 3. True delay (noise cross-correlation), 20 ms chunks ==
delay ms LowLatency FIR   8000->16000 (filter  1.5 + held-back   0.0)    1.5  ok
delay ms LowLatency FIR  24000->8000  (filter  1.5 + held-back   0.0)    1.5  ok
delay ms LowLatency FIR  24000->16000 (filter  0.8 + held-back   0.0)    0.8  ok
delay ms soxr HQ          8000->16000 (filter  0.0 + held-back  28.8)    28.8
delay ms soxr HQ         24000->8000  (filter  0.0 + held-back  42.6)    42.6
delay ms soxr HQ         24000->16000 (filter  0.0 + held-back  12.1)    12.1
delay ms audioop.ratecv   8000->16000 (filter  0.0 + held-back   0.1)    0.1
delay ms audioop.ratecv  24000->8000  (filter  0.0 + held-back   0.0)    0.0
delay ms audioop.ratecv  24000->16000 (filter  0.0 + held-back   0.0)    0.0

== 4. Provider loopback: Gemini 24k -> wire frames -> 16k (as if echoed back) ==
E2E SNR dB Twilio mu-law 8k (160 B)       frame sizes [160]              38.8  ok
E2E SNR dB Telnyx PCMA 8k (160 B)         frame sizes [160]              37.9  ok
E2E SNR dB Exotel slin 8k (3200 B)        frame sizes [3200]             82.4  ok
E2E SNR dB Exotel slin 16k (3200 B)       frame sizes [3200]             85.1  ok
E2E SNR dB Exotel slin 24k (4800 B)       frame sizes [4800]             85.1  ok
E2E SNR dB Plivo L16 16k (640 B)          frame sizes [640]              85.1  ok
E2E SNR dB Plivo mu-law 8k (160 B)        frame sizes [160]              38.8  ok
E2E SNR dB Vonage L16 16k (640 B, binary) frame sizes [640]              85.1  ok
E2E SNR dB Vonage L16 24k (960 B, binary) frame sizes [960]              85.1  ok
Exotel 8k chunk bytes / ms (multiple of 320, >= 3200)                    (3200, 200.0)  ok
Exotel 16k chunk bytes / ms (multiple of 320, >= 3200)                   (3200, 100.0)  ok
Exotel 24k chunk bytes / ms (multiple of 320, >= 3200)                   (4800, 100.0)  ok

== 5. Pacing, base64 overhead, CPU ==
Pacer: wall time to send 2.0 s of 20 ms frames, 200 ms lead (expect ~1.8 s) 1.781  ok
Twilio 20 ms frame: payload bytes / JSON message bytes / kbit/s          (160, 305, 122.0)
CPU s: 60 s of Gemini audio -> 3000 Twilio frames                        (0.0256, 3000)  ok
CPU s: 60 s of caller mu-law -> Gemini 16k (3000 x 20 ms frames)         0.0368

RESULT: PASS
```

How to read it:
- G.711 caps µ-law/A-law providers at about 37–39 dB, which is normal telephony.
- The FIR resampler matches soxr quality at about 1 ms of delay instead of 12–43 ms.
- `audioop.ratecv` aliases (4.5–8 dB on 24k→8k; 4.5–15 dB across ratios) when Gemini's 24 kHz voice has energy above the output Nyquist. Don't use it.

## 5. Known limits / VERIFY before production
- Only offline smoke-tested. Make one real call per provider and check: audio both ways, barge-in, the mark echo,
  hang-up, transfer, and DTMF.
- The energy VAD is crude. Line echo louder than `VAD_THRESHOLD_DBFS + GREETING_VAD_BOOST_DB` can fake a greeting
  barge-in, and noise can delay silence check-ins. The production system used Silero VAD; swap it in via
  `telephony_audio.EnergyVAD`'s interface.
- Session resumption is not transparent-mode: no client-message replay. Caller audio is buffered during the reconnect
  (`mic_q`, about 10 s). Tool calls pending across a reconnect are undefined.
- Twilio WSS signature URL form, Exotel casing and sequence fields, and Plivo V3 WSS signature all carry VERIFY
  items; see
  `references/07-websocket-media-streams.md`.
- Gemini 3.8 "silent audio turns" (a transcript with near-zero PCM) are not detected here; see
  `references/13-troubleshooting-and-lessons.md`.
