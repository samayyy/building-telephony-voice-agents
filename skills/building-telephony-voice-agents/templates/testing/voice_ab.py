#!/usr/bin/env python3
"""Render the same moments across voices x prompts x language hints, then judge the AUDIO blind.

Why
---
Voice, accent and tone ("sounds dramatic", "sounds American", "robotic") are properties of the audio, not the
transcript. A multimodal model can rate rendered audio against a strict, contrast-anchored rubric. In one
production deployment this picked the final voice and proved the calm-tone prompt block
(dramatic 4.6 -> 2.6 / 5, professional 2.1 -> 3.4 on emotional moments).

Blindness: each WAV is saved under a random id; the judge sees only the audio, the moment's language and the
rubric, never the voice name or prompt label. The id -> config key is in key.json.

moments.json
------------
{
  "greeting":  {"language": "English", "system": "You are the voice of a customer-care line ... say exactly the line
                you are given in a calm, steady tone, and nothing else.",
                "turns": [["user", "Say exactly this line: Hello, you have reached ..."]]},
  "upset_hi":  {"language": "Hindi",
                "turns": [["model", "<the fixed greeting>"],
                          ["user", "Maine kal aapka product liya tha, usme keede the, main bahut pareshan hoon."]]},
  "question":  {"language": "English", "turns": [["model", "<greeting>"], ["user", "Is this product safe for kids?"]]}
}
A moment without "system" uses each --prompt file (that is what makes a prompt A/B). Pick moments where tone
matters: the greeting, an upset caller, a safety complaint, a plain question, a read-back of digits.

Usage
-----
  python voice_ab.py --moments moments.json --voices Kore,Gacrux,Sulafat --prompt current=cur.txt \
      --prompt calm=calm.txt --language-codes none,en-IN --runs 3 --out voice_ab/ --narrowband
  python voice_ab.py --out voice_ab/ --judge-only          # re-judge existing audio (e.g. with a new rubric)

--narrowband passes each render through 300-3400 Hz + 8 kHz mu-law (ffmpeg) before judging, i.e. what a caller
on a PSTN line hears. Listen yourself to the top two configs before deciding: judges are a filter, not a verdict.

Requirements: google-genai (verified against 2.21.0); ffmpeg on PATH for --narrowband.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path

from google import genai
from google.genai import types

DEFAULT_RUBRIC = (
    "This is one reply from an automated voice agent on a {context}, speaking {language}. The owner wants a calm, "
    "natural, professional conversational tone, like an experienced call-centre executive, with a {accent} accent. "
    "Listen carefully and rate STRICTLY; most replies should not get 5. Use the anchors.\n"
    "Return JSON: {{"
    "\"dramatic\": 1-5 (1 = level, matter-of-fact; 5 = theatrical: exclamations, pitch spikes, breathy or whispered "
    "sympathy, sing-song, stretched words), "
    "\"professional\": 1-5 (5 = calm, steady, composed executive), "
    "\"human_like\": 1-5 (5 = indistinguishable from a real agent on a phone line; 1 = obviously synthetic), "
    "\"accent_match\": 1-5 (5 = clearly the requested accent; for a non-English language, a native speaker), "
    "\"pace\": \"too slow\" | \"natural\" | \"too fast\", "
    "\"words_per_second\": number, "
    "\"language_spoken\": string, "
    "\"robotic_cues\": \"what exactly sounds synthetic, or none\", "
    "\"notes\": \"one short sentence\"}}"
)
NUMERIC = ("dramatic", "professional", "human_like", "accent_match", "words_per_second")


def make_client(args, location: str) -> genai.Client:
    if args.backend == "gemini-api":
        return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("pass --project or set GOOGLE_CLOUD_PROJECT")
    return genai.Client(vertexai=True, project=project, location=location)


async def render(client, args, system: str, turns: list, voice: str, lang: str | None) -> tuple[bytes, str]:
    speech = {"voice_config": types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))}
    if lang:
        speech["language_code"] = lang
    cfg = types.LiveConnectConfig(response_modalities=[types.Modality.AUDIO], system_instruction=system,
                                  speech_config=types.SpeechConfig(**speech),
                                  output_audio_transcription=types.AudioTranscriptionConfig())
    pcm, said = bytearray(), ""
    async with client.aio.live.connect(model=args.model, config=cfg) as s:
        if len(turns) > 1:  # seed history, then send the moment
            await s.send_client_content(turns=[types.Content(role=r, parts=[types.Part(text=t)])
                                               for r, t in turns[:-1]], turn_complete=False)
        await s.send_client_content(turns=[types.Content(role="user", parts=[types.Part(text=turns[-1][1])])],
                                    turn_complete=True)

        async def collect() -> None:
            nonlocal said
            async for m in s.receive():                       # one model turn
                sc = m.server_content
                if not sc:
                    continue
                for p in (sc.model_turn.parts if sc.model_turn else None) or []:
                    if p.inline_data and p.inline_data.data:
                        pcm.extend(p.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    said += sc.output_transcription.text

        await asyncio.wait_for(collect(), timeout=40)
    return bytes(pcm), said.strip()


def write_wav(path: Path, pcm: bytes, rate: int = 24000) -> None:   # Gemini Live output: 24 kHz s16le mono
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def narrowband(src: Path) -> Path:
    """What a PSTN caller hears: band-limit, 8 kHz mu-law round trip, back to 16-bit WAV."""
    dst = src.with_suffix(".nb.wav")
    mulaw = src.with_suffix(".nb.ulaw.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af", "highpass=f=300,lowpass=f=3400",
                    "-ar", "8000", "-ac", "1", "-acodec", "pcm_mulaw", str(mulaw)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mulaw), "-acodec", "pcm_s16le", str(dst)], check=True)
    mulaw.unlink(missing_ok=True)
    return dst


def judge(client, args, wav: Path, language: str, rubric: str) -> dict:
    q = rubric.format(context=args.context, language=language, accent=args.accent)
    audio = wav.read_bytes()
    for attempt in range(5):
        try:
            r = client.models.generate_content(
                model=args.judge_model,
                contents=[q, types.Part.from_bytes(data=audio, mime_type="audio/wav")],
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
            return json.loads(r.text)
        except Exception:  # noqa: BLE001
            time.sleep(5 * (attempt + 1))
    return {}


def summarise(rows: list[dict]) -> None:
    keyf = lambda r: (r["moment"], r["prompt"], r["voice"], r["lang"])  # noqa: E731
    print(f"\n{'moment':<14}{'prompt':<12}{'voice':<12}{'lang':<8}" + "".join(f"{k[:12]:>14}" for k in NUMERIC))
    for key, grp in itertools.groupby(sorted(rows, key=keyf), key=keyf):
        grp = list(grp)
        cells = []
        for k in NUMERIC:
            vals = [float(r[k]) for r in grp if isinstance(r.get(k), (int, float))]
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            cells.append(f"{statistics.mean(vals):.1f}±{sd:.1f}" if vals else "-")
        print(f"{key[0]:<14}{key[1]:<12}{key[2]:<12}{key[3]:<8}" + "".join(f"{c:>14}" for c in cells)
              + f"   n={len(grp)}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--moments", type=Path)
    ap.add_argument("--voices", default="Kore")
    ap.add_argument("--prompt", action="append", default=[], metavar="LABEL=PATH")
    ap.add_argument("--language-codes", default="none", help="comma list; 'none' = no speech_config.language_code")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rubric", type=Path, help="file with a rubric; placeholders {context} {language} {accent}")
    ap.add_argument("--context", default="customer-care phone line")
    ap.add_argument("--accent", default="natural local")
    ap.add_argument("--narrowband", action="store_true")
    ap.add_argument("--judge-only", action="store_true")
    ap.add_argument("--model", default="gemini-3.8-live")
    ap.add_argument("--judge-model", default="gemini-3.8-flash")
    ap.add_argument("--backend", choices=["vertex", "gemini-api"], default="vertex")
    ap.add_argument("--project", default="")
    ap.add_argument("--location", default="us")
    ap.add_argument("--judge-location", default="global")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    rubric = args.rubric.read_text() if args.rubric else DEFAULT_RUBRIC
    audio_dir = args.out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    key_path = args.out / "key.json"
    key: dict = json.loads(key_path.read_text()) if key_path.exists() else {}
    judge_client = make_client(args, args.judge_location)

    if not args.judge_only:
        if not args.moments:
            sys.exit("--moments is required unless --judge-only")
        moments = json.loads(args.moments.read_text())
        prompts = {}
        for item in args.prompt:
            label, _, path = item.partition("=")
            prompts[label] = Path(path).read_text()
        live_client = make_client(args, args.location)
        voices = [v.strip() for v in args.voices.split(",") if v.strip()]
        langs = [None if x.strip() == "none" else x.strip() for x in args.language_codes.split(",")]
        plan = []
        for name, m in moments.items():
            variants = {"fixed": m["system"]} if m.get("system") else prompts
            if not variants:
                sys.exit(f"moment {name} has no system and no --prompt was given")
            for (label, system), voice, lang, run in itertools.product(variants.items(), voices, langs,
                                                                       range(args.runs)):
                plan.append((name, m, label, system, voice, lang, run))
        sem = asyncio.Semaphore(args.concurrency)

        async def one(name, m, label, system, voice, lang, run):
            async with sem:
                for attempt in range(3):
                    try:
                        pcm, said = await render(live_client, args, system, m["turns"], voice, lang)
                        break
                    except Exception as exc:  # noqa: BLE001
                        print(f"retry {name} {label} {voice}: {exc}", file=sys.stderr)
                        await asyncio.sleep(5 * (attempt + 1))
                else:
                    return
            if not pcm:
                print(f"NO AUDIO {name} {label} {voice} {lang} (transcript: {said!r})", file=sys.stderr)
                return
            rid = uuid.uuid4().hex[:10]
            write_wav(audio_dir / f"{rid}.wav", pcm)
            key[rid] = {"moment": name, "language": m.get("language", "English"), "prompt": label, "voice": voice,
                        "lang": lang or "none", "run": run, "said": said,
                        "seconds": round(len(pcm) / 48000, 2)}

        await asyncio.gather(*(one(*p) for p in plan))
        key_path.write_text(json.dumps(key, ensure_ascii=False, indent=1))

    rows = []
    for rid in sorted(key, key=lambda _: uuid.uuid4().hex):   # shuffled judging order
        wav = audio_dir / f"{rid}.wav"
        if not wav.exists():
            continue
        if args.narrowband:
            wav = narrowband(wav)
        verdict = await asyncio.to_thread(judge, judge_client, args, wav, key[rid]["language"], rubric)
        row = {"id": rid, **key[rid], **verdict}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (args.out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    summarise(rows)


if __name__ == "__main__":
    asyncio.run(main())
