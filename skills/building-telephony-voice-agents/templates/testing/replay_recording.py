#!/usr/bin/env python3
"""Replay a REAL caller recording into Gemini Live with production timing, and log what the model hears and says.

Why
---
Text simulations never exercise hearing, echo, noise or turn-taking. Real phone recordings do. Streaming the
caller channel of a stored call into a fresh Live session at real-time pace reproduces bugs such as:
  * phantom replies over the greeting (PSTN echo of the greeting reaches Gemini's server VAD),
  * first reply in the wrong language,
  * mis-heard brand words (custom_vocabulary on/off),
  * barge-in during the greeting being lost or answered.
In one production deployment this reproduced the greeting-echo phantom reply 24/24 and proved the fix 0/57.

Input
-----
A recording with the caller on its own channel (LiveKit RecorderIO stores stereo: caller ch0, agent ch1; carrier
dual-channel recordings vary - check), any format ffmpeg can read, or a mono caller WAV. Decoded to 16 kHz s16le
mono, streamed in 100 ms chunks at real-time pace, followed by --tail-seconds of silence (Gemini's VAD needs a
continuous stream or ACTIVITY_END never arrives).

Greeting modes (the caller channel during the first --greeting-seconds normally contains greeting ECHO)
  open : stream caller audio from t=0 (no protection; reproduces echo phantom replies).
  gate : send silence until the greeting ends (echo-safe, but a caller who talks over the greeting is unheard).
  hold : hold caller audio during the greeting. If a barge-in is detected (or given with --barge-in-at), release
         at speech_start + --barge-in-threshold and replay the held audio from speech_start - --replay-lead;
         otherwise wait --echo-tail after the greeting and DISCARD the held audio. This mirrors the hold-not-drop
         design in references/03-conversation-mechanics.md.
At the greeting end (when not interrupted) the greeting text is pushed as a model turn (turn_complete=False), as the
production agent does after playing a cached greeting.

Barge-in test: --mix other_call.wav --mix-from 11.4 --mix-to 16.2 --mix-at 3.0 mixes a real utterance into the echo
window at 3.0 s. Use --barge-in-at with the time your local VAD fires (run Silero over the mixed audio for an exact
value; --barge-in-at auto uses a crude energy detector).

Examples
--------
  python replay_recording.py call.ogg --prompt system.txt --greeting-text "Hello, ..." --greeting-seconds 8.5 \
      --seconds 45 --greeting-mode open --runs 5
  python replay_recording.py call.ogg --prompt system.txt --greeting-text "..." --greeting-seconds 8.5 \
      --greeting-mode hold --mix other.ogg --mix-from 11.4 --mix-to 16.2 --mix-at 3.0 --barge-in-at 3.15

Output: one JSON line per run with merged events [t, kind, text] (HEARD = input transcription, SAID = output
transcription, FIRST_AUDIO, INTERRUPTED, TURN_DONE, ACTIVITY_START/END, TOOL_CALL) and flags
(phantom_reply_during_greeting, first_reply_at). Then a SUMMARY line per variant.

Requirements: google-genai (verified against 2.21.0), numpy, ffmpeg on PATH.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types

RATE = 16000
CHUNK_S = 0.1
CHUNK_BYTES = int(RATE * CHUNK_S) * 2


def decode(path: str, channel: int | None, start: float = 0.0, seconds: float | None = None) -> np.ndarray:
    """Decode any audio file to 16 kHz mono int16 using ffmpeg; pick one channel if given."""
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-i", path]
    if channel is not None:
        cmd += ["-af", f"pan=mono|c0=c{channel}"]
    cmd += ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).copy()


def energy_barge_in(x: np.ndarray, until_s: float, min_speech_s: float) -> float | None:
    """Crude barge-in detector: first run of >= min_speech_s with 20 ms frames above noise floor + 15 dB.
    Echo is usually quieter than direct speech but not always; prefer a real VAD time via --barge-in-at."""
    win = int(RATE * 0.02)
    n = min(len(x), int(RATE * until_s)) // win
    if n == 0:
        return None
    frames = x[: n * win].astype(np.float32).reshape(n, win) / 32768
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    thr = max(np.percentile(db, 10) + 15, -45)
    need, run = int(min_speech_s / 0.02), 0
    for i, active in enumerate(db > thr):
        run = run + 1 if active else 0
        if run >= need:
            return round((i - run + 1) * 0.02, 2)
    return None


def make_client(args) -> genai.Client:
    if args.backend == "gemini-api":
        return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("pass --project or set GOOGLE_CLOUD_PROJECT")
    return genai.Client(vertexai=True, project=project, location=args.location)


def live_config(args, system: str) -> types.LiveConnectConfig:
    tr = {}
    if args.language_codes:
        tr["language_codes"] = args.language_codes.split(",")
    if args.custom_vocab:
        tr["custom_vocabulary"] = [ln.strip() for ln in Path(args.custom_vocab).read_text().splitlines() if ln.strip()]
    speech = {"voice_config": types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=args.voice))}
    if args.language_code:
        speech["language_code"] = args.language_code
    detection = {}
    if args.start_sensitivity:
        detection["start_of_speech_sensitivity"] = getattr(types.StartSensitivity,
                                                           f"START_SENSITIVITY_{args.start_sensitivity}")
    if args.end_sensitivity:
        detection["end_of_speech_sensitivity"] = getattr(types.EndSensitivity, f"END_SENSITIVITY_{args.end_sensitivity}")
    if args.silence_ms:
        detection["silence_duration_ms"] = args.silence_ms
    kwargs = dict(response_modalities=[types.Modality.AUDIO], system_instruction=system,
                  speech_config=types.SpeechConfig(**speech),
                  input_audio_transcription=types.AudioTranscriptionConfig(**tr),
                  output_audio_transcription=types.AudioTranscriptionConfig())
    if detection:
        kwargs["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(**detection))
    return types.LiveConnectConfig(**kwargs)


async def run_once(client, args, system: str, audio: np.ndarray, barge_in_at: float | None, n: int) -> dict:
    pcm = audio.tobytes() + bytes(int(RATE * args.tail_seconds) * 2)
    g_end = args.greeting_seconds
    events: list[list] = []
    state = {"turn_audio": False}

    if args.greeting_mode == "hold" and barge_in_at is not None and barge_in_at < g_end:
        release_at = barge_in_at + args.barge_in_threshold
        replay_from = max(0.0, barge_in_at - args.replay_lead)
        interrupted_greeting = True
    else:
        release_at = g_end + (args.echo_tail if args.greeting_mode == "hold" else 0.0)
        replay_from = None
        interrupted_greeting = False

    async with client.aio.live.connect(model=args.model, config=live_config(args, system)) as s:
        t0 = time.monotonic()

        def now() -> float:
            return round(time.monotonic() - t0, 2)

        async def send_audio(data: bytes) -> None:
            await s.send_realtime_input(audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))

        async def sender() -> None:
            pushed = False
            released = args.greeting_mode != "hold"
            for k, i in enumerate(range(0, len(pcm), CHUNK_BYTES)):
                el = k * CHUNK_S
                await asyncio.sleep(max(0.0, el - (time.monotonic() - t0)))      # real-time pacing
                if args.greeting_text and not pushed and not interrupted_greeting and el >= release_at:
                    # greeting finished playing uninterrupted: tell Gemini it greeted (production does this)
                    await s.send_client_content(turns=[types.Content(
                        role="model", parts=[types.Part(text=args.greeting_text)])], turn_complete=False)
                    pushed = True
                    events.append([now(), "GREETING_PUSHED", ""])
                chunk = pcm[i:i + CHUNK_BYTES]
                if args.greeting_mode == "gate" and el < g_end:
                    await send_audio(bytes(len(chunk)))                           # silence keeps VAD alive
                elif args.greeting_mode == "hold" and not released:
                    if el >= release_at:
                        released = True
                        if replay_from is not None:                               # caller barged in: replay
                            events.append([now(), "RELEASE_REPLAY", f"from {replay_from:.2f}s"])
                            for j in range(int(replay_from / CHUNK_S) * CHUNK_BYTES, i, CHUNK_BYTES):
                                await send_audio(pcm[j:j + CHUNK_BYTES])
                        else:
                            events.append([now(), "RELEASE_DISCARD", ""])
                        await send_audio(chunk)
                    # else: held (not sent)
                else:
                    await send_audio(chunk)

        async def receiver() -> None:
            while True:
                async for m in s.receive():
                    if m.voice_activity and m.voice_activity.voice_activity_type:
                        events.append([now(), str(m.voice_activity.voice_activity_type.value), ""])
                    if m.tool_call:
                        for fc in m.tool_call.function_calls or []:
                            events.append([now(), "TOOL_CALL", fc.name])
                    sc = m.server_content
                    if not sc:
                        continue
                    if sc.input_transcription and sc.input_transcription.text:
                        events.append([now(), "HEARD", sc.input_transcription.text])
                    if sc.model_turn and not state["turn_audio"]:
                        if any(p.inline_data and p.inline_data.data for p in sc.model_turn.parts or []):
                            state["turn_audio"] = True
                            events.append([now(), "FIRST_AUDIO", ""])
                    if sc.output_transcription and sc.output_transcription.text:
                        events.append([now(), "SAID", sc.output_transcription.text])
                    if sc.interrupted:
                        events.append([now(), "INTERRUPTED", ""])
                    if sc.turn_complete:
                        state["turn_audio"] = False
                        events.append([now(), "TURN_DONE", ""])

        rx = asyncio.create_task(receiver())
        try:
            await sender()
        finally:
            rx.cancel()

    merged: list[list] = []
    for t, kind, text in events:
        if merged and merged[-1][1] == kind and kind in ("HEARD", "SAID"):
            merged[-1][2] += text
        else:
            merged.append([t, kind, text])
    first_audio = [t for t, k, _ in merged if k == "FIRST_AUDIO"]
    return {
        "variant": args.label, "run": n, "greeting_mode": args.greeting_mode, "barge_in_at": barge_in_at,
        "phantom_reply_during_greeting": bool(first_audio and first_audio[0] < g_end and not interrupted_greeting),
        "first_reply_at": first_audio[0] if first_audio else None,
        "heard": " ".join(x for _, k, x in merged if k == "HEARD"),
        "events": merged,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording")
    ap.add_argument("--caller-channel", type=int, default=0, help="-1 = file is already mono")
    ap.add_argument("--start", type=float, default=0.0, help="offset into the recording (s)")
    ap.add_argument("--seconds", type=float, default=45.0, help="how much caller audio to stream")
    ap.add_argument("--prompt", required=True, help="system prompt file")
    ap.add_argument("--greeting-text", default="")
    ap.add_argument("--greeting-seconds", type=float, default=0.0, help="greeting playout length in the recording")
    ap.add_argument("--greeting-mode", choices=["open", "gate", "hold"], default="open")
    ap.add_argument("--barge-in-at", default="", help="seconds when local VAD saw caller speech, or 'auto'")
    ap.add_argument("--barge-in-threshold", type=float, default=0.8, help="continuous speech before release")
    ap.add_argument("--replay-lead", type=float, default=0.6)
    ap.add_argument("--echo-tail", type=float, default=0.4)
    ap.add_argument("--mix", default="", help="file with a caller utterance to mix in (barge-in test)")
    ap.add_argument("--mix-channel", type=int, default=0)
    ap.add_argument("--mix-from", type=float, default=0.0)
    ap.add_argument("--mix-to", type=float, default=0.0)
    ap.add_argument("--mix-at", type=float, default=3.0)
    ap.add_argument("--custom-vocab", default="", help="file, one phrase per line")
    ap.add_argument("--language-codes", default="", help="input transcription hints, e.g. en-IN,hi-IN")
    ap.add_argument("--language-code", default="", help="speech_config.language_code")
    ap.add_argument("--start-sensitivity", choices=["", "HIGH", "LOW"], default="")
    ap.add_argument("--end-sensitivity", choices=["", "HIGH", "LOW"], default="")
    ap.add_argument("--silence-ms", type=int, default=0)
    ap.add_argument("--voice", default="Kore")
    ap.add_argument("--model", default="gemini-3.8-live")
    ap.add_argument("--backend", choices=["vertex", "gemini-api"], default="vertex")
    ap.add_argument("--project", default="")
    ap.add_argument("--location", default="us")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--tail-seconds", type=float, default=6.0)
    ap.add_argument("--label", default="variant")
    ap.add_argument("--out", default="", help="append JSON lines here as well as stdout")
    args = ap.parse_args()

    system = Path(args.prompt).read_text()
    channel = None if args.caller_channel < 0 else args.caller_channel
    audio = decode(args.recording, channel, args.start, args.seconds).astype(np.int32)
    if args.mix:
        utt = decode(args.mix, None if args.mix_channel < 0 else args.mix_channel, args.mix_from,
                     (args.mix_to - args.mix_from) or None).astype(np.int32)
        o = int(RATE * args.mix_at)
        end = o + len(utt)
        if end > len(audio):
            audio = np.concatenate([audio, np.zeros(end - len(audio), dtype=np.int32)])
        audio[o:end] += utt
    audio = np.clip(audio, -32768, 32767).astype(np.int16)

    barge_in_at = None
    if args.barge_in_at == "auto":
        barge_in_at = energy_barge_in(audio, args.greeting_seconds, args.barge_in_threshold)
        print(f"auto barge-in detection: {barge_in_at}", file=sys.stderr)
    elif args.barge_in_at:
        barge_in_at = float(args.barge_in_at)

    client = make_client(args)
    sem = asyncio.Semaphore(args.concurrency)

    async def guarded(n: int):
        async with sem:
            for attempt in range(3):
                try:
                    return await run_once(client, args, system, audio, barge_in_at, n)
                except Exception as exc:  # noqa: BLE001 - 1011/1006 drops, quota
                    print(f"retry run {n}: {exc}", file=sys.stderr)
                    await asyncio.sleep(5 * (attempt + 1))
            return None

    results = [r for r in await asyncio.gather(*(guarded(n) for n in range(args.runs))) if r]
    for r in results:
        line = json.dumps(r, ensure_ascii=False)
        print(line, flush=True)
        if args.out:
            with open(args.out, "a") as fh:
                fh.write(line + "\n")
    phantom = sum(r["phantom_reply_during_greeting"] for r in results)
    print(f"SUMMARY {args.label}: phantom replies during greeting {phantom}/{len(results)}; "
          f"first replies at {[r['first_reply_at'] for r in results]}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
