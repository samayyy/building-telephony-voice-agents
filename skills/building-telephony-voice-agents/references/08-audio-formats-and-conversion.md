# 08 — Audio formats and conversion (telephony ⇄ Gemini Live)

Purpose: the audio engineering a phone bridge has to get right, with measured numbers. It covers telephony formats
(µ-law and A-law at 8 kHz, linear L16/slin at 8/16/24 kHz, codecs on SIP legs), the Gemini Live contract (16 kHz in,
24 kHz out), the conversion pipeline per provider, and which resampler to use and why. Test results come from
`scripts/audio_selftest.py`. It also covers frame packing, real-time pacing and jitter, base64 overhead, playback
buffers and barge-in timing, echo, levels, and how the narrowband phone line should shape the choice of voice.
The code is in `templates/ws_bridge/telephony_audio.py`. Provider wire protocols are in
`references/07-websocket-media-streams.md`. Greeting, echo-hold and barge-in behaviour are in
`references/03-conversation-mechanics.md`.

Measurements were taken 2026-10-21 with Python 3.14.5, numpy 2.5.4, scipy 1.18.1, soxr 1.1.0 and audioop-lts 0.2.2,
using synthetic multi-tone and noise signals. Live-call numbers are labelled "measured in a production deployment"
(Gemini 3.8 Live + LiveKit + Indian SIP trunk).

## Contents
1. [Formats you will meet](#1-formats-you-will-meet)
2. [The Gemini Live audio contract](#2-the-gemini-live-audio-contract)
3. [Conversion pipeline per provider](#3-conversion-pipeline-per-provider)
4. [Resampling: what to use and why](#4-resampling-what-to-use-and-why)
5. [Measured results](#5-measured-results)
6. [Frame packing cheat sheet](#6-frame-packing-cheat-sheet)
7. [Pacing and jitter](#7-pacing-and-jitter)
8. [base64 and JSON overhead](#8-base64-and-json-overhead)
9. [Playback buffers and barge-in timing](#9-playback-buffers-and-barge-in-timing)
10. [Echo, levels, normalisation](#10-echo-levels-normalisation)
11. [Narrowband effects on voice choice](#11-narrowband-effects-on-voice-choice)

---

## 1. Formats you will meet

| Format | Bytes/sample | Rate | Where | Notes |
|---|---|---|---|---|
| **G.711 µ-law** (`audio/x-mulaw`, `ulaw`, PCMU, PT 0) | 1 | 8 kHz | Twilio (fixed), Plivo default, Telnyx PCMU, SIP everywhere | 14-bit logarithmic companding. Round-trip SNR is about 37–39 dB on speech-level tones. Silence code `0xFF`. |
| **G.711 A-law** (PCMA, PT 8) | 1 | 8 kHz | Indian/European SIP trunks (Exotel prefers PCMA), Telnyx PCMA | 13-bit companding. Silence code `0xD5`. |
| **L16 / slin / "raw PCM"** | 2 (LE) | 8, 16, 24 kHz | Exotel (8/16/24k, base64), Plivo `audio/x-l16` (8/16k), Vonage (8/16/24k, binary), Telnyx L16 | Little-endian on these WS APIs. **RTP L16 is big-endian** per RFC 3551, so check Telnyx (VERIFY). |
| **G.722** | ~1 (64 kbps) | 16 kHz audio (SDP clock 8000) | LiveKit SIP default, Vonage SIP | Wideband, but most PSTN routes and Twilio/Plivo trunks don't carry it. |
| **AMR-WB** | variable | 16 kHz | Mobile networks; LiveKit opt-in; Telnyx streams | |
| **Opus** | variable | 48 kHz (8–48k) | Inside LiveKit (WebRTC room tracks); Telnyx streams | **Opus over SIP to LiveKit is not in LiveKit's codec list (VERIFY)**. On the SIP path, LiveKit transcodes G.711 ↔ Opus 48 kHz itself. |
| **Gemini Live in** | 2 (LE) | **16 kHz** | `audio/pcm;rate=16000` | The Gemini API says other rates are resampled server-side if declared in the MIME type. That's untested on 3.8, so send 16k. |
| **Gemini Live out** | 2 (LE) | **24 kHz** | `server_content.model_turn.parts[].inline_data.data` | Always 24k mono. |

**What the caller hears:** whatever format you send, a PSTN or mobile leg is effectively narrowband (about
300–3400 Hz) and G.711 somewhere along the route. A wideband wire (Exotel 16k, Plivo L16 16k) **doesn't make the
caller hear wideband audio**. It removes conversions in your bridge (CPU, latency, one resampler) and keeps the
caller's audio cleaner for Gemini's recogniser.

---

## 2. The Gemini Live audio contract

- **Input:** raw PCM16 LE mono 16 kHz, `types.Blob(data=…, mime_type="audio/pcm;rate=16000")` via
  `session.send_realtime_input(audio=…)`, one keyword per call.
  - Send **20–40 ms chunks** (the guide allows 20–100 ms; best practice is 20–40 ms "to minimize latency"). Don't
    buffer a second before sending.
  - The template sends 40 ms (1280 B).
- **Keep the stream continuous.** Server VAD needs audio after speech to detect end of speech: "if you stop sending
  frames after speech, ACTIVITY_END never comes". While caller audio is held or muted, send **digital silence of the
  same length** (as the bridge does). Or send `send_realtime_input(audio_stream_end=True)` when the stream pauses for
  more than about 1 s.
- **Output:** 24 kHz PCM16 LE mono.
  - Server chunks measured in a production deployment: median 7,680 B (160 ms), max 13,440 B (280 ms), occasionally a
    2-byte first chunk. Your framer must handle odd sizes, which `FrameBuffer` does.
  - "Content is generated as quickly as possible, and not in realtime". In practice it arrives at roughly real-time
    pace, sometimes in bursts.
- **Silence isn't tokenised:** a 10-minute silent stream used about 120–145 tokens. Streaming silence while holding
  costs almost nothing.

---

## 3. Conversion pipeline per provider

Recommended wire settings are shown first; each pipeline needs at most one resampler per direction. Per-call state:
**one `Codec` (two resamplers + a framer) per call, created at `start`**.

| Provider (setting) | Caller → Gemini | Gemini → caller | Outbound frame |
|---|---|---|---|
| Twilio (fixed µ-law 8k) | µ-law → PCM16 8k → **FIR ×2** → 16k | 24k → **FIR ÷3** (anti-alias) → 8k → µ-law | 160 B / 20 ms |
| Exotel `?sample-rate=16000` | passthrough | 24k → **FIR ×2/3** → 16k | 3200 B / 100 ms (n×320, ≥3200) |
| Exotel `?sample-rate=24000` | 24k → FIR ×2/3 → 16k | passthrough | 4800 B / 100 ms |
| Exotel default 8000 (avoid) | PCM16 8k → ×2 | 24k → ÷3 | 3200 B = **200 ms** minimum |
| Plivo `audio/x-l16;rate=16000` | passthrough | 24k → ×2/3 → 16k | 640 B / 20 ms (`playAudio`) |
| Plivo default µ-law 8k | as Twilio | as Twilio | 160 B |
| Vonage `audio/l16;rate=16000` | passthrough (binary) | 24k → ×2/3 | 640 B binary |
| Vonage `audio/l16;rate=24000` | 24k → ×2/3 | passthrough | 960 B binary |
| Telnyx L16 16k / PCMU / PCMA | passthrough / as Twilio / A-law variant | ×2/3 / as Twilio / A-law | 20 ms |
| LiveKit SIP (no bridge code) | G.711 8k → LiveKit SIP → 48k Opus room track → AudioStream resample → **16k** (ask RoomIO for `sample_rate=16000`, else 48→24→16) | plugin hard-codes 24k → `AudioSource(24000)` → 48k Opus → SIP → 8k G.711 | handled by LiveKit |

```python
import telephony_audio as ta
codec = ta.exotel_codec(16000)          # or twilio_codec(), plivo_codec("audio/x-l16;rate=16000"),
                                        #    vonage_codec(16000), telnyx_codec("PCMA")
pcm16k = codec.to_gemini(wire_bytes)    # -> session.send_realtime_input(audio=Blob(pcm16k, "audio/pcm;rate=16000"))
for frame in codec.from_gemini(pcm24k): # exact wire frames (partial frame kept for the next chunk)
    send(adapter.media(frame))
for frame in codec.end_of_turn():       # at turn_complete: resampler tail + padded last frame
    send(adapter.media(frame))
codec.barge_in()                        # on interrupted: drop the partial frame (keep the resamplers)
```

Choosing a wire rate when the provider lets you:
- Prefer the rate that makes **caller → Gemini** a passthrough (16k). That path decides recognition and VAD latency.
- Exotel 16k also makes the 3200 B minimum chunk exactly 100 ms.
- Never pick 8k L16 when 16k is offered: it costs two resamples and, on Exotel, a 200 ms minimum chunk.

---

## 4. Resampling: what to use and why

**Rules:**
1. **Stateful, per call, per direction.** Resampling each 20 ms chunk independently (stateless `resample_poly`) drops
   SNR to **31.9 dB**, with a click at every frame boundary. Create the resamplers at `start`. **Don't recreate them
   on barge-in**: they're filter delay lines, and the few ms of history are harmless.
2. **Downsampling Gemini's 24 kHz voice needs a real anti-alias low-pass.** The voice has energy above 4 kHz
   (fricatives, breath). `audioop.ratecv` and naive `x[::3]` fold it back into the band:
   - 24k→8k with tones above the output Nyquist: **audioop.ratecv 4.5–8.0 dB, naive decimation 7.4 dB**, against
     the stateful FIR at 82–84 dB and soxr HQ at 80–81 dB. Audible as harsh, hissy "s" sounds.
   - `ratecv` upsampling 8k→16k is linear interpolation: 15.2 dB on a 2.5 kHz tone (42.5 dB on 440 Hz alone).
   - **Don't use `audioop.ratecv` in production.** In-band-only test signals hide the problem (ratecv scored 87.7 dB
     on 24k→8k with no out-of-band content).
3. **Streaming soxr adds latency.** `soxr.ResampleStream` HQ holds back **12–43 ms** (noise cross-correlation, 20 ms
   chunks), and up to 66 ms in a steady-state tone test. It emits nothing for the first 2–6 chunks. MQ is similar
   (30–52 ms), and VHQ (Pipecat's default) is 37–53 ms. Every direction pays it on every turn.
4. **Default: the stateful polyphase FIR** (`LowLatencyResampler`: scipy `firwin` Kaiser β=8, 24 taps per phase × max(L, M), `lfilter` with
   carried `zi`). It gives **82–84 dB SNR with ~0.5–1.5 ms delay** for every ratio used here, including out-of-band
   content (85 dB end to end on the L16 provider loopbacks in §5).
   - CPU: 60 s of Gemini audio → 3,000 Twilio frames in about 0.025 s, and 60 s of caller µ-law → 16k in about
     0.04 s. That's over 1,500× real time.
   - Filter length scales with max(L, M). An earlier version sized 24k→16k (L=2, M=3) at only 49 taps and leaked
     9–12 kHz content (55 dB). Fixed in the template: 73 taps → 82 dB.
5. **Exact integer ratios:**
   - 8k→16k ×2 (L=2, M=1)
   - 24k→8k ÷3 (L=1, M=3)
   - 24k→16k ×2/3 (L=2, M=3)
   - 16k→24k ×3/2
   - 16k→8k ÷2
6. **G.711:** the numpy table codecs in `telephony_audio.py` are **bit-exact with `audioop`** for all 65,536 int16
   inputs and all 256 codes (µ-law and A-law). `audioop` was removed from the stdlib in Python 3.13; `audioop-lts`
   restores it, but you don't need it.

---

## 5. Measured results

From `scripts/audio_selftest.py` (full output in `templates/ws_bridge/README.md`). Streaming in 20 ms chunks. For
downsampling, the input carries two tones above the output Nyquist that must be filtered out.

| Path | LowLatency FIR | soxr HQ (stream) | audioop.ratecv |
|---|---|---|---|
| 8k → 16k SNR | 83.5 dB | 80.8 dB | 15.2 dB |
| 24k → 8k SNR (with out-of-band) | 84.3 dB | 81.4 dB | **4.5 dB** |
| 24k → 16k SNR (with out-of-band) | 82.1 dB | 80.8 dB | **7.7 dB** |
| 16k → 24k SNR | 82.9 dB | 81.2 dB | 27.3 dB |
| delay 8k → 16k | **1.5 ms** | 28.8 ms | 0.1 ms |
| delay 24k → 8k | **1.5 ms** | 42.6 ms | 0.0 ms |
| delay 24k → 16k | **0.8 ms** | 12.1 ms | 0.0 ms |

End-to-end loopback (Gemini 24k → exact wire frames → base64 → back to 16k), LowLatency FIR:

| Provider format | Frame | SNR |
|---|---|---|
| Twilio / Plivo µ-law 8k | 160 B | 38.8 dB (the G.711 ceiling) |
| Telnyx PCMA 8k | 160 B | 37.9 dB |
| Exotel slin 8k | 3200 B (200 ms) | 82.4 dB |
| Exotel slin 16k / 24k | 3200 B / 4800 B (100 ms) | 85.1 dB |
| Plivo L16 16k | 640 B | 85.1 dB |
| Vonage L16 16k / 24k (binary) | 640 B / 960 B | 85.1 dB |

The SNR shows conversion fidelity on synthetic tones, not perceived quality. The phone network's own G.711 hop
(~37–39 dB) dominates whatever you do above it.

---

## 6. Frame packing cheat sheet

| Format | 20 ms | 40 ms | 100 ms | 1 s |
|---|---|---|---|---|
| µ-law / A-law 8k | **160 B** | 320 B | 800 B | 8,000 B |
| PCM16 8k | **320 B** | 640 B | 1,600 B | 16,000 B |
| PCM16 16k | **640 B** | 1,280 B (Gemini input chunk) | **3,200 B** | 32,000 B |
| PCM16 24k | 960 B | 1,920 B | 4,800 B | 48,000 B |

- **Twilio:** "any size", so send 160 B.
- **Plivo:** about 20 ms (≤64 KB per message). **Vonage:** 20 ms binary frames. **Telnyx RTP mode:** 20 ms to 30 s.
- **Exotel:** a **multiple of 320 B**, at least **3,200 B**, at most 100 KB.
  - 320 B is 20 ms at 8 kHz. Exotel plays internally in 20 ms packets, and a short last packet makes it "wait for
    20ms … audio gaps".
  - At 16k, 3,200 B is 100 ms; at 24k use 4,800 B; at 8k, 3,200 B is 200 ms.
- **End of turn:** pad the final partial frame with codec silence (`0xFF` µ-law, `0xD5` A-law, `0x0000` PCM). Never
  send a short frame to Exotel.
- **Inbound:** forward each provider chunk straight away. The bridge re-chunks to 40 ms for Gemini, and Exotel's
  ~100 ms inbound chunks get split.

---

## 7. Pacing and jitter

**Send in real time with a small lead. Don't burst.**
- Gemini often delivers audio faster than real time. If you forward it immediately, the provider's buffer fills
  (Twilio "buffered and played in the order received"; Plivo and Vonage about 60 s).
- Then barge-in depends entirely on `clear`, and your idea of "what the caller has heard" is wrong.
- Exotel's own sample code links back-to-back sends to early (~4 s) hang-ups on Connect calls (VERIFY).

`telephony_audio.Pacer(frame_ms, lead_ms)` keeps at most `lead_ms` queued at the provider. Measured: 2.0 s of 20 ms
frames with a 200 ms lead goes out in 1.78 s.
```python
pacer = ta.Pacer(frame_ms=codec.frame_seconds * 1000, lead_ms=150)
async def sender():
    while True:
        epoch, frame = await out_q.get()
        await pacer.wait_turn()
        if epoch == current_epoch:      # dropped if a barge-in happened while we waited
            await ws.send_text(adapter.media(frame))
# on barge-in: current_epoch += 1; drain out_q; codec.barge_in(); pacer.reset(); send adapter.clear()
```
- **Lead of 100–200 ms.**
  - Lower risks underruns (audible gaps) when the event loop or network stalls.
  - Higher makes barge-in slower and `clear` more important.
  - Exotel's 100 ms chunks need a lead of at least 100 ms.
  - The template defaults to 150 ms.
- **Reset the pacer after idle.** The tested pacer restarts its clock when it falls behind real time (an idle gap
  between turns). Otherwise the first reply after a pause would be burst out as "catch-up".
- **Run the sender as its own task** so the receive loop never blocks. Caller audio must keep flowing to Gemini
  during playback, or barge-in can't be detected.
- **Uplink jitter:** providers send a steady 20 ms (Twilio, Plivo, Vonage) or ~100 ms (Exotel). No jitter
  buffer is needed: forward immediately. Over TCP, network jitter shows up as bunching, not loss, and Gemini handles
  bunched input. Don't aggregate beyond 40–100 ms, because it delays VAD.
- **Marks are the ground truth for playback.** Send a `mark`/`checkpoint`/`notify` after each turn's last frame and
  act on the echo: hang up after the goodbye's mark, release the greeting hold, start silence timers. Fall back to
  the local pacer clock with a timeout.
- Turn off Nagle (most WS servers already set `TCP_NODELAY`), and avoid proxies that buffer frames
  (`proxy_buffering off`).

---

## 8. base64 and JSON overhead

- base64 is 4/3 the size: 160 B µ-law becomes 216 chars.
- A Twilio media message for one 20 ms frame is about **305 B** of JSON, which is **~122 kbit/s per direction** against
  64 kbit/s of raw G.711. Plivo L16 16k: 640 B → about 860 chars → ~370 kbit/s per direction.
- That's trivial for one call. At 1,000 concurrent calls it's ~250–750 Mbit/s of WS traffic, so size NICs and LB
  costs accordingly. Vonage's binary frames avoid the overhead.
- CPU for base64 + JSON is negligible next to the network.
  - Don't `json.dumps` large dicts per frame; build the string once per frame, as the adapters do.
  - Never log payloads.

---

## 9. Playback buffers and barge-in timing

What the caller experiences when they start talking over the bot:

```
caller starts speaking
  └─ ~0.5–0.58 s  Gemini server VAD -> server_content.interrupted   (measured on 3.8; START_SENSITIVITY_LOW ~0.57 s)
       └─ bridge: drop local queue + send clear                     (~0 ms local; one network trip to the provider)
            └─ provider drops its buffer                            (Twilio/Telnyx/Vonage/Plivo: immediately;       
                                                                      Exotel: the chunk already playing finishes,
                                                                      up to 100 ms at 16k / 200 ms at 8k)
bot audio heard after onset ≈ 0.5–0.6 s + network RTT/2 + (Exotel residual chunk)
```
- Without `clear`, the bot keeps talking for the whole provider buffer: the rest of the reply, up to 60 s on Plivo or
  Vonage. **Always send the provider's clear on `interrupted`**, even when your lead is small.
- After `clear`, Twilio and Telnyx return the pending marks. Treat those turns as "not heard" if you track what the
  caller heard. The template treats cleared marks as finished, so waiters don't hang.
- `turn_complete` follows `interrupted` about 0.12 s later. Drop any late audio for the interrupted turn (the epoch
  check does this).
- **False barge-ins** come from echo of the bot's own voice, backchannels ("haan", "ok") and background voices.
  - Mitigations: `START_SENSITIVITY_LOW` with `prefix_padding_ms` 100–200; 3.8's proactive audio ignores speech that
    isn't addressed to it; hold caller audio during the greeting (§10).
  - On LiveKit SIP, BVC telephony noise cancellation also suppresses background voices.

---

## 10. Echo, levels, normalisation

**Echo.**
- PSTN hybrids and handsets return part of the bot's voice on the caller channel. Carrier G.168 echo cancellers and
  handset AEC remove most of it, but not all. **The bridge has no AEC, and LiveKit applies none to SIP participants.**
- Residual echo reaches Gemini's server VAD and can trigger a **phantom reply** (the bot answering its own greeting).
  - In a production deployment, replayed real recordings reproduced this **24/24** times. Holding caller audio during
    the greeting fixed it (**0/57**).
- **Hold, don't drop.**
  - Buffer caller audio during the greeting and send silence to Gemini.
  - If ≥0.8 s of continuous caller speech is detected (in production, Silero VAD measured echo and chatter bursts of
    ≤0.70 s, and real speech was caught within 0.15 s), stop the greeting and replay the held audio from 0.6 s before
    onset.
  - Otherwise discard it after a 0.4 s echo tail.
  - The template implements this with an energy VAD; swap in Silero for production. Details:
    `references/03-conversation-mechanics.md`.
- Don't add outbound gain, and don't run a noise gate on the caller path: gates clip soft onsets ("haan", digits).

**Levels.**
- Gemini's output is speech-normalised, so send it as is. Don't add gain: µ-law clips hard above full scale, and
  louder output means louder echo.
- Caller levels on PSTN vary by 20+ dB. Gemini's recogniser coped with narrowband, noisy and quiet input in testing,
  so **don't normalise or AGC the caller path**. A slow AGC also boosts echo and noise between words.
- `telephony_audio.rms_dbfs()` gives per-frame levels for logs and VAD thresholds. Typical speech on a G.711 line is
  about −35 to −20 dBFS RMS. Digital silence from a held line is −inf (all `0xFF`).
- **Noise suppression:** on LiveKit, use `BVCTelephony` (one NC method only; not both BVC and trunk Krisp). It can
  clip soft first syllables, so A/B it on real calls. On a WS bridge there's no built-in NC, and Gemini's
  proactive-audio filtering plus `START_SENSITIVITY_LOW` is usually enough.

**Narrowband robustness.** In a production deployment, synthetic callers through a faithful chain (G.711 µ-law 8 kHz →
48k → Opus 32 kbps → 16k, plus pink noise) showed **no loss in transcription or turn detection** in English, Hindi and
Kannada, with replies in the caller's language. Real callers (accents, packet loss, babble) are harder; test with
recordings (`references/10-testing-and-evaluation.md`).

---

## 11. Narrowband effects on voice choice

- The caller hears Gemini's 24 kHz voice **band-limited to about 300–3400 Hz** and µ-law/A-law quantised. Qualities
  that live above 3.4 kHz don't survive: breathiness, "air", sibilant sparkle.
- Voices described as *Breathy* (Enceladus) or *Soft* (Achernar) lose intelligibility first. *Clear*, *Firm* and
  *Even* voices (Erinome, Iapetus, Kore, Orus, Schedar) usually carry better. **[inference: listen to confirm]**
- Very low-pitched voices lose their fundamental below 300 Hz. Pitch is still perceived (the missing-fundamental
  effect), but the voice sounds thinner. Gacrux and Pulcherrima measured male-range F0 (~135–150 Hz).
- A production deployment chose its voice by **blind judging of rendered audio** (`references/10-testing-and-evaluation.md`). Its final
  voice (Gacrux, "Mature") beat the warm default (Sulafat) on "not dramatic" and "professional".
- **Judge voices the way callers hear them.** Render each candidate, pass it through the telephone chain, then
  listen or blind-judge:
  ```python
  import numpy as np, telephony_audio as ta
  def as_heard_on_phone(pcm24k: bytes) -> bytes:              # 24 kHz PCM16 -> 8 kHz mu-law -> back to 16 kHz
      down, up = ta.LowLatencyResampler(24000, 8000), ta.LowLatencyResampler(8000, 16000)
      return up.process(ta.ulaw_to_pcm16(ta.pcm16_to_ulaw(down.process(pcm24k))))
  ```
  This simulates the G.711 hop and band limit. It doesn't model packet loss, the 300 Hz high-pass, or mobile
  codecs (AMR-NB); add those if your callers are mostly on mobile.
- **Accent and pace come from the prompt, not the voice.** Voices default to General American English unless told
  otherwise. There's no `speaking_rate` on 3.8, and fast speech is harder to follow on narrowband lines, so ask for
  a relaxed telephone pace (`references/09-prompting-voice-agents.md`).
