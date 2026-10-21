#!/usr/bin/env python3
"""Full simulated phone calls: LLM caller persona  <->  Gemini Live agent  ->  LLM judge.

What it does
------------
For every (prompt variant x persona x run):
  1. Opens a Gemini Live session with your agent's system prompt (audio out, output transcription on).
  2. Seeds the fixed greeting as a model turn (the greeting is normally played from cache, not generated).
  3. Loops: a text LLM plays the caller from a persona brief and the call so far; its line is sent to Live as a
     user text turn; the agent's spoken reply is collected from output transcription.
  4. Stops when the caller says [HANGUP], the agent calls the end-call tool, or --max-turns is reached.
  5. A text LLM judges the whole transcript against named boolean checks (JSON schema, temperature 0).
  6. Objective metrics are computed without any LLM (reply length, multi-question replies,
     English-only replies in a non-English call, text-turn -> first-audio time).

It prints per-run verdicts and a SUMMARY table (pass k/n with a Wilson 95% interval) per prompt variant,
so an old-vs-new prompt A/B is one command:

    python simulate_calls.py --personas personas.json \
        --prompt old=prompts/old.txt --prompt new=prompts/new.txt --runs 6 --out sims/

Text-in for the caller is a deliberate simplification: it is fast and cheap and tests *behaviour*
(language held, one question per reply, commitments gated, close sequence). It does NOT test hearing,
turn-taking, echo or latency on a phone line: use replay_recording.py for that.

personas.json
-------------
{
  "common_checks": {                     # applied to every persona (optional)
    "one_question": "Every agent reply asked at most one question."
  },
  "personas": {
    "terse_en": {
      "persona": "You are a caller phoning a consumer helpline. Short plain English, one fact at a time, answer only
                  what is asked. Problem: <...>. Name <fake>. Address <fake>. When asked anything else, say no thanks.",
      "language": "en",                  # expected call language (ISO 639-1); non-"en" enables english_only_ratio
      "checks": {
        "mandatory_before_confirm": "Find the first agent turn that says the complaint is registered or gives a
            reference number. Before that turn the agent had asked for the contact number and the address; or there
            was no such turn.",
        "anything_else_once": "'Anything else?' was asked exactly once."
      }
    },
    "impatient_hi": {
      "persona": "You are an impatient caller speaking Hinglish (romanised Hindi with English words) ...",
      "language": "hi",
      "checks": {
        "language_held": "After the fixed English greeting, every agent reply was in Hindi (romanised or Devanagari;
            English loanwords are fine). A reply made of English sentences fails this."
      }
    }
  }
}

Write checks as observable behaviour with the pass condition spelled out ("Find the first turn that ...; before it
...; or there was no such turn"). Vague checks ("was polite") produce noisy verdicts. Use fake PII only.

Requirements: google-genai (verified against 2.21.0). Vertex: ADC + --project (or GOOGLE_CLOUD_PROJECT).
Gemini API: --backend gemini-api and GEMINI_API_KEY. Model ids are flags; verify them against current docs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

CALLER_SUFFIX = (
    " Output only your next spoken line as the caller, nothing else: no stage directions, no quotes, no speaker label."
    " After the agent has said goodbye, reply only: [HANGUP]."
)

# Function words that are common in English and rare in romanised Hindi/Indic text. Used only for the
# objective english_only metric; the LLM judge is NOT trusted for language (it misreads romanised Hindi as English).
_EN_FUNCTION_WORDS = {
    "the", "is", "are", "you", "your", "and", "to", "a", "an", "of", "for", "please", "can", "could", "i", "we",
    "it", "this", "that", "have", "has", "will", "would", "what", "my", "me", "with", "on", "in", "be", "do",
    "not", "thank", "thanks", "sorry", "there", "anything", "else", "okay", "sure", "let", "know", "about",
}
_LATIN_WORD = re.compile(r"[A-Za-z']+")
_NON_LATIN_LETTER = re.compile(r"[ऀ-෿؀-ۿ฀-๿一-鿿]")  # Indic, Arabic, Thai, CJK


# --------------------------------------------------------------------------------------------- clients
def make_client(args: argparse.Namespace, location: str) -> genai.Client:
    if args.backend == "gemini-api":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            sys.exit("set GEMINI_API_KEY for --backend gemini-api")
        return genai.Client(api_key=key)
    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("pass --project or set GOOGLE_CLOUD_PROJECT for --backend vertex")
    return genai.Client(vertexai=True, project=project, location=location)


# --------------------------------------------------------------------------------------------- metrics
def english_only(text: str) -> bool:
    """True when a reply is plainly English: no non-Latin letters and >= 25% English function words."""
    if _NON_LATIN_LETTER.search(text):
        return False
    words = [w.lower() for w in _LATIN_WORD.findall(text)]
    if len(words) < 3:
        return False
    return sum(w in _EN_FUNCTION_WORDS for w in words) / len(words) >= 0.25


def objective_metrics(convo: list[dict], language: str) -> dict:
    agent = [t for t in convo[1:] if t["role"] == "agent"]  # skip the seeded greeting
    texts = [t["text"] for t in agent if t["text"]]
    words = [len(t.split()) for t in texts]
    ttfa = [t["first_audio_ms"] for t in agent if t.get("first_audio_ms") is not None]
    out = {
        "agent_replies": len(agent),
        "empty_replies": sum(1 for t in agent if not t["text"]),
        "mean_words": round(sum(words) / len(words), 1) if words else 0,
        "max_words": max(words) if words else 0,
        "multi_question_replies": sum(1 for t in texts if t.count("?") > 1),
        "text_ttfa_ms_p50": sorted(ttfa)[len(ttfa) // 2] if ttfa else None,
        "ended_by": convo[-1].get("ended_by") if convo else None,
    }
    if language and language != "en" and texts:
        out["english_only_replies"] = sum(english_only(t) for t in texts)
        out["english_only_ratio"] = round(out["english_only_replies"] / len(texts), 3)
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


# --------------------------------------------------------------------------------------------- one call
def live_config(args: argparse.Namespace, system: str) -> types.LiveConnectConfig:
    speech = {"voice_config": types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=args.voice))}
    if args.language_code:
        speech["language_code"] = args.language_code
    kwargs = dict(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=system,
        speech_config=types.SpeechConfig(**speech),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    if args.end_call_tool:
        kwargs["tools"] = [types.Tool(function_declarations=[types.FunctionDeclaration(
            name=args.end_call_tool,
            description="End the phone call. Call it only after you have said goodbye. Say nothing after calling it.",
        )])]
    # Deliberately NOT set: thinking config, enable_affective_dialog, proactivity, max_output_tokens,
    # TEXT modality. Each silently breaks gemini-3.8-live (see references/02-gemini-live-model-and-config.md).
    return types.LiveConnectConfig(**kwargs)


async def agent_reply(session, args: argparse.Namespace) -> dict:
    """Collect one agent turn. Answers tool calls generically; stops on the end-call tool."""
    said, first_audio, tools, ended = "", None, [], False
    t0 = time.monotonic()

    async def _collect() -> None:
        nonlocal said, first_audio, ended
        while True:
            async for m in session.receive():          # yields one model turn, ends after turn_complete
                if m.tool_call:
                    responses = []
                    for fc in m.tool_call.function_calls or []:
                        tools.append({"name": fc.name, "args": dict(fc.args or {})})
                        if fc.name == args.end_call_tool:
                            ended = True
                        responses.append(types.FunctionResponse(
                            id=fc.id, name=fc.name, response={"status": "ok"}))
                    if ended:
                        return
                    await session.send_tool_response(function_responses=responses)
                sc = m.server_content
                if not sc:
                    continue
                if sc.model_turn and first_audio is None:
                    if any(p.inline_data and p.inline_data.data for p in sc.model_turn.parts or []):
                        first_audio = time.monotonic()
                if sc.output_transcription and sc.output_transcription.text:
                    said += sc.output_transcription.text
                if sc.turn_complete:
                    return

    try:
        await asyncio.wait_for(_collect(), timeout=args.turn_timeout)
    except asyncio.TimeoutError:
        tools.append({"name": "<turn timeout>", "args": {}})
    text = re.sub(r"<no speech>|\{pause\}", "", said, flags=re.IGNORECASE).strip()  # Live silence placeholders
    return {"role": "agent", "text": text, "tools": tools, "ended_call": ended,
            "first_audio_ms": round((first_audio - t0) * 1000) if first_audio else None}


async def caller_line(text_client, args, persona: str, convo: list[dict]) -> str:
    hist = "\n".join(f"{'Agent' if t['role'] == 'agent' else 'You'}: {t['text']}" for t in convo)
    prompt = f"{persona}{CALLER_SUFFIX}\n\nCall so far:\n{hist}\nYou:"
    for attempt in range(5):
        try:
            r = await asyncio.to_thread(text_client.models.generate_content, model=args.caller_model,
                                        contents=prompt, config=types.GenerateContentConfig(temperature=0.7))
            return (r.text or "").strip()
        except Exception as exc:  # noqa: BLE001 - transport errors, quota; retry with backoff
            await asyncio.sleep(2 * (attempt + 1))
            last = exc
    raise RuntimeError(f"caller LLM failed: {last}")


async def simulate(live_client, text_client, args, system: str, persona: str) -> list[dict]:
    convo = [{"role": "agent", "text": args.greeting, "seeded": True}]
    async with live_client.aio.live.connect(model=args.model, config=live_config(args, system)) as s:
        if args.greeting:
            # The greeting is played from cache in production; Gemini only learns it through context.
            await s.send_client_content(
                turns=[types.Content(role="model", parts=[types.Part(text=args.greeting)])], turn_complete=False)
        for _ in range(args.max_turns):
            line = await caller_line(text_client, args, persona, convo)
            if not line or "[HANGUP]" in line:
                convo[-1]["ended_by"] = "caller"
                break
            convo.append({"role": "caller", "text": line})
            await s.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=line)])], turn_complete=True)
            reply = await agent_reply(s, args)
            convo.append(reply)
            if reply["ended_call"]:
                reply["ended_by"] = "agent_tool"
                break
        else:
            convo[-1]["ended_by"] = "max_turns"
    return convo


# --------------------------------------------------------------------------------------------- judge
def judge(text_client, args, convo: list[dict], checks: dict[str, str]) -> dict:
    transcript = "\n".join(f"{'AGENT' if t['role'] == 'agent' else 'CALLER'}: {t['text']}"
                           + (f"  [tool: {', '.join(x['name'] for x in t.get('tools', []))}]" if t.get("tools") else "")
                           for t in convo)
    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={**{k: types.Schema(type=types.Type.BOOLEAN) for k in checks},
                    "notes": types.Schema(type=types.Type.STRING)},
        required=[*checks, "notes"])
    prompt = (
        "You are a strict QA reviewer of phone calls between a caller and an automated voice agent. The agent text is"
        " a speech transcription, so ignore punctuation and spelling. Romanised and native-script text in the same"
        " language count as that language; English loanwords inside another language do not make a reply English.\n\n"
        f"Transcript:\n{transcript}\n\nFor each check, answer true only if the transcript clearly satisfies it.\n"
        f"{json.dumps(checks, ensure_ascii=False, indent=1)}\n"
        "In notes, quote the agent line that made any check fail."
    )
    votes: list[dict] = []
    for _ in range(args.judge_repeats):
        for attempt in range(5):
            try:
                r = text_client.models.generate_content(
                    model=args.judge_model, contents=prompt,
                    config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                                       response_schema=schema))
                votes.append(json.loads(r.text))
                break
            except Exception:  # noqa: BLE001
                time.sleep(3 * (attempt + 1))
    if not votes:
        return {}
    verdict = {k: sum(bool(v.get(k)) for v in votes) * 2 > len(votes) for k in checks}  # majority vote
    verdict["notes"] = " | ".join(str(v.get("notes", "")) for v in votes)
    verdict["judge_unanimous"] = all(len({bool(v.get(k)) for v in votes}) == 1 for k in checks)
    return verdict


# --------------------------------------------------------------------------------------------- runner
async def run_one(label, system, name, spec, i, args, live_client, text_client, common, sem, out_dir):
    checks = {**common, **spec.get("checks", {})}
    async with sem:
        convo = None
        for attempt in range(3):
            try:
                convo = await simulate(live_client, text_client, args, system, spec["persona"])
                break
            except Exception as exc:  # noqa: BLE001 - connection drops, 1011, quota
                print(f"retry {label} {name} {i}: {exc}", flush=True)
                await asyncio.sleep(5 * (attempt + 1))
        if convo is None:
            return None
    verdict = await asyncio.to_thread(judge, text_client, args, convo, checks)
    metrics = objective_metrics(convo, spec.get("language", "en"))
    row = {"label": label, "persona": name, "run": i, "verdict": verdict, "metrics": metrics, "convo": convo}
    (out_dir / f"sim_{label}_{name}_{i}.json").write_text(json.dumps(row, ensure_ascii=False, indent=1))
    print(label, name, i, {k: verdict.get(k) for k in checks}, metrics, flush=True)
    return row


def summarise(rows: list[dict], personas: dict, common: dict) -> list[dict]:
    summary = []
    labels = sorted({r["label"] for r in rows})
    for name, spec in personas.items():
        for check in {**common, **spec.get("checks", {})}:
            for label in labels:
                rs = [r for r in rows if r["label"] == label and r["persona"] == name and r["verdict"]]
                k, n = sum(bool(r["verdict"].get(check)) for r in rs), len(rs)
                lo, hi = wilson(k, n)
                summary.append({"label": label, "persona": name, "check": check, "pass": k, "n": n,
                                "ci95": [round(lo, 2), round(hi, 2)]})
                print(f"SUMMARY {label:>10} {name:<20} {check:<28} {k}/{n}  (95% CI {lo:.2f}-{hi:.2f})")
    for label in labels:
        rs = [r for r in rows if r["label"] == label and "english_only_replies" in r["metrics"]]
        if rs:
            eo = sum(r["metrics"]["english_only_replies"] for r in rs)
            tot = sum(r["metrics"]["agent_replies"] - r["metrics"]["empty_replies"] for r in rs)
            print(f"METRIC  {label:>10} english_only replies in non-English calls: {eo}/{tot}")
        mq = sum(r["metrics"]["multi_question_replies"] for r in rows if r["label"] == label)
        print(f"METRIC  {label:>10} replies with >1 question mark: {mq}")
    return summary


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--personas", required=True, type=Path)
    ap.add_argument("--prompt", action="append", required=True, metavar="LABEL=PATH",
                    help="system prompt file; repeat for an A/B (old=old.txt --prompt new=new.txt)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="", help="comma-separated persona names")
    ap.add_argument("--greeting", default="", help="fixed greeting text seeded as the first model turn")
    ap.add_argument("--voice", default="Kore")
    ap.add_argument("--language-code", default="", help="speech_config.language_code (off by default; see refs)")
    ap.add_argument("--end-call-tool", default="end_call", help="declare a no-arg end-call tool ('' = none)")
    ap.add_argument("--model", default="gemini-3.8-live")
    ap.add_argument("--caller-model", default="gemini-3.8-flash")
    ap.add_argument("--judge-model", default="gemini-3.8-flash")
    ap.add_argument("--judge-repeats", type=int, default=1, help="odd number > 1 = majority vote")
    ap.add_argument("--backend", choices=["vertex", "gemini-api"], default="vertex")
    ap.add_argument("--project", default="")
    ap.add_argument("--location", default="us", help="Live location (us, eu, us-central1 for 3.8)")
    ap.add_argument("--text-location", default="global", help="location for caller/judge text models")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--turn-timeout", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=Path("sims"))
    args = ap.parse_args()

    spec = json.loads(args.personas.read_text())
    personas, common = spec["personas"], spec.get("common_checks", {})
    if args.only:
        personas = {k: v for k, v in personas.items() if k in args.only.split(",")}
    prompts = {}
    for item in args.prompt:
        label, _, path = item.partition("=")
        if not path:
            sys.exit(f"--prompt expects LABEL=PATH, got {item!r}")
        prompts[label] = Path(path).read_text()
    args.out.mkdir(parents=True, exist_ok=True)

    live_client = make_client(args, args.location)
    text_client = make_client(args, args.text_location)
    sem = asyncio.Semaphore(args.concurrency)
    jobs = [run_one(label, system, name, p, i, args, live_client, text_client, common, sem, args.out)
            for label, system in prompts.items() for name, p in personas.items() for i in range(args.runs)]
    rows = [r for r in await asyncio.gather(*jobs) if r]
    summary = summarise(rows, personas, common)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
