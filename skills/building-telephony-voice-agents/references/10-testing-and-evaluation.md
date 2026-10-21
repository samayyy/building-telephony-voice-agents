# Testing and evaluating phone voice agents

Purpose: how to prove a Gemini Live phone agent behaves before and after every change, with a layered strategy
that runs from free unit tests up to real test calls. Each layer catches failures the cheaper layers cannot see:
unit tests pin invariants, moment probes check one reply, simulated calls check whole-call behaviour, recording
replays check hearing and turn-taking on real phone audio, blind audio judging checks voice and tone, and recording
forensics measure what callers actually experienced. Runnable harnesses are in `templates/testing/`. Numbers marked
"measured" come from one production inbound care line (Gemini `gemini-3.8-live` on Vertex AI, LiveKit Agents
1.7.1, Indian PSTN via SIP, English/Hindi/Kannada callers) and are evidence, not guarantees.

For LiveKit's own test framework (pytest turn tests, `lk agent simulate` scenarios), use LiveKit's official skills
`testing-livekit-agents`, `writing-livekit-scenarios` and `running-livekit-simulations`
(https://github.com/livekit/agent-skills). This file covers what they do not: Gemini Live realtime specifics,
telephony audio, and judging methods.

## Contents

1. [The layers and what each catches](#1-the-layers-and-what-each-catches)
2. [Layer 0: unit tests for invariants and pure logic](#2-layer-0-unit-tests-for-invariants-and-pure-logic)
3. [Layer 1: live provider checks in the deploy gate](#3-layer-1-live-provider-checks-in-the-deploy-gate)
4. [Layer 2: moment probes](#4-layer-2-moment-probes)
5. [Layer 3: full simulated calls](#5-layer-3-full-simulated-calls)
6. [Layer 4: replaying real caller recordings](#6-layer-4-replaying-real-caller-recordings)
7. [Local VAD tests on recordings](#7-local-vad-tests-on-recordings)
8. [Blind voice and tone judging](#8-blind-voice-and-tone-judging)
9. [Recording forensics](#9-recording-forensics)
10. [Judge pitfalls and sample sizes](#10-judge-pitfalls-and-sample-sizes)
11. [Real test calls: review checklist](#11-real-test-calls-review-checklist)
12. [What to log on every call](#12-what-to-log-on-every-call)
13. [An acceptance bar that worked](#13-an-acceptance-bar-that-worked)

---

## 1. The layers and what each catches

| Layer | Catches | Misses | Cost / speed | Tool |
|---|---|---|---|---|
| 0. Unit tests | Config that silently breaks Live, lost dependency pins, prompt structure drift, close/latency/hold logic bugs, analytics allowlist gaps | Anything the provider does | Free, seconds | pytest |
| 1. Live provider check | A provider rejecting an option on every request (dead air from the greeting on) | Behaviour | Cents, < 1 min | small CLI that speaks to the real API |
| 2. Moment probe | One reply at a known-bad moment (paired questions, wrong language, early close) | Multi-turn drift | Cheap, minutes | text-seeded Live session + LLM judge |
| 3. Simulated call | Whole-call behaviour: language held, mandatory details before commitments, close sequence, re-asking | Hearing, echo, latency, turn-taking | Moderate | `templates/testing/simulate_calls.py` |
| 4. Recording replay | Echo phantom replies, barge-in, mis-hearing, first-reply language on real audio | Agent-side code (LiveKit hooks) | Moderate | `templates/testing/replay_recording.py` |
| 5. Voice/tone judging | Accent, "dramatic", robotic, pace | Content | Cheap | `templates/testing/voice_ab.py` |
| 6. Recording forensics | Real reply gaps, talk-over, hang-up races, what the caller actually heard | Root cause | Cheap | `templates/testing/measure_reply_gaps.py`, audio-LLM listen pass |
| 7. Real test calls | Carrier, SIP, region, cold start, everything end to end | Statistics (n is small) | Expensive, slow | a phone |

Rule that held up: **every prompt or config change ships with an old-vs-new count** (from a probe, simulation or
replay) recorded in the commit message, e.g. "paired questions at the 3 known moments: 3/12 -> 0/12".

## 2. Layer 0: unit tests for invariants and pure logic

Pin the facts that cost an incident to learn, so nobody "cleans them up" later.

**Configuration invariants** (see `references/02-gemini-live-model-and-config.md` for the reasons):

```python
FORBIDDEN = ("thinking_config", "enable_affective_dialog", "proactivity", "max_output_tokens")

def test_the_settings_that_break_gemini_live_are_never_sent():
    model = build_realtime_model(Settings.for_tests(), factory=RecordingFactory)
    for key in FORBIDDEN:
        assert key not in model.kwargs, key            # each one silently kills audio or the session on 3.8
    assert model.kwargs.get("response_modalities", ["AUDIO"]) == ["AUDIO"]

def test_context_compression_is_always_on():          # 15-min audio cap without it
    assert build_realtime_model(Settings.for_tests(), factory=RecordingFactory).kwargs["context_window_compression"]

def test_server_vad_is_left_alone_unless_a_knob_is_set():
    assert "realtime_input_config" not in build_realtime_model(Settings.for_tests(), factory=RecordingFactory).kwargs

def test_a_tuning_typo_never_refuses_a_call():
    s = Settings.for_tests(live_end_sensitivity="HGIH")
    assert s.pipeline_errors() and not s.cloud_errors()  # logged + fallback, never a refused call

def test_the_image_installs_exactly_the_tested_versions():
    deps = tomllib.loads(Path("pyproject.toml").read_text())["project"]["dependencies"]
    assert "livekit-agents[google,silero]==1.7.1" in deps      # the Dockerfile installs from pyproject, not uv.lock
    assert "google-genai==2.21.0" in deps
```

**Prompt invariants** (test intent and structure, not exact phrases; 44 intent tests replaced 49 brittle
phrase-pinning tests in the measured deployment):

- section order (role, voice/accent, language, ... per-call values last, so the shared prefix is maximal);
- size budget (e.g. `len(prompt) < 45_000` chars) because every prompt token is billed on every turn;
- no incident stories / dates in the prompt (evidence belongs in a separate rationale doc);
- the realtime prompt contains **no end-of-call marker text** when hang-up is a tool (the model writes the marker
  and ignores the tool otherwise);
- every system-driven instruction (silence check-in, max-duration goodbye) says it is not the close;
- quoted sample lines in other languages are labelled per language.

**Pure logic worth unit-testing** (all had real bugs):

| Logic | Cases to pin |
|---|---|
| Hang-up grace close | commit-time check (caller speaking at commit cancels), close generations (a stale task can neither commit nor cancel a newer farewell), cancel returns to LISTENING |
| Latency anchoring | reply before local VAD end is still measured; audio without end of speech is not a sample; anchors only when the agent is not speaking |
| Greeting caller-audio hold | held frames discarded on no barge-in; replay starts at `speech_start - lead`; bounded buffer |
| Silence ladder | "away" counts as listening; check-in does not reset the ladder; staged once each |
| Reference numbers | never start with 0; derived deterministically from the session id |
| Analytics PII validator | allowlist first, substring backstop only for unknown keys; no phone-like runs / emails in values |
| Analytics call sites | an **AST test** that reads every `_emit(...)` call and fails if an attribute is not on the allowlist (the emitter swallows rejections, so two call sites had silently dropped every event) |
| Extractor -> CRM | one test fills **every** extractable field and pushes it through every guard and value map |

## 3. Layer 1: live provider checks in the deploy gate

Offline tests cannot see a provider rejecting a request. Measured incident: a "harmless" TTS option (custom
pronunciations) was rejected with `INVALID_ARGUMENT` on every synthesis, giving dead air from the greeting on,
while unit tests, lint and the offline eval were all green.

Add a command that speaks to the real API with the deployed configuration and exits non-zero on failure; run it
before every speech-path deploy, and prove it catches the original failure before trusting it:

```python
async def live_check(settings) -> None:
    """Connect with the exact production LiveConnectConfig, request one short line, require audio bytes."""
    cfg = production_live_config(settings)          # same builder the agent uses
    async with client.aio.live.connect(model=settings.model, config=cfg) as s:
        await s.send_realtime_input(text="Say exactly: line check.")
        audio = 0
        async for m in s.receive():
            sc = m.server_content
            for p in (sc.model_turn.parts if sc and sc.model_turn else None) or []:
                audio += len(p.inline_data.data or b"") if p.inline_data else 0
    if audio < 4800:                                 # < 100 ms of 24 kHz PCM16 = no real audio
        raise SystemExit("live check: no audio returned")   # e.g. thinking config set on 3.8
```

The same probe doubles as the per-process reachability check at prewarm
(`references/11-deployment-ops-and-integrations.md`).

## 4. Layer 2: moment probes

Take a real (redacted) or simulated conversation, cut it just before the moment of interest, seed the history
into a fresh Live session, and judge **only the next reply** against one sentence.

```python
async def next_reply(system: str, turns: list[tuple[str, str]]) -> str:
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=system,
                                  output_audio_transcription=types.AudioTranscriptionConfig(),
                                  speech_config=types.SpeechConfig(voice_config=VOICE))
    said = ""
    async with client.aio.live.connect(model="gemini-3.8-live", config=cfg) as s:
        await s.send_client_content(                       # history: roles "user" / "model"
            turns=[types.Content(role=r, parts=[types.Part(text=t)]) for r, t in turns[:-1]],
            turn_complete=False)
        await s.send_client_content(                       # the moment
            turns=[types.Content(role="user", parts=[types.Part(text=turns[-1][1])])], turn_complete=True)
        async for m in s.receive():                        # one model turn
            sc = m.server_content
            if m.tool_call:
                said += " [TOOL:" + ",".join(f.name for f in m.tool_call.function_calls) + "]"
                break
            if sc and sc.output_transcription and sc.output_transcription.text:
                said += sc.output_transcription.text
    return said.strip()

def judge(reply: str, criterion: str) -> bool:     # text model, JSON mode, temperature 0
    r = judge_client.models.generate_content(
        model="gemini-3.8-flash",
        contents=f"Agent reply (spoken, transcribed): {reply!r}\nCriterion: {criterion}\n"
                 'Does the reply meet the criterion? JSON {"pass": true|false, "why": "..."}',
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
    return json.loads(r.text)["pass"]
```

Practice:

- Name moments after the failure: `close_step1`, `no_early_close`, `size_readback`, `first_reply_language_hi`,
  `one_question_after_address`. Keep a `MOMENTS = {name: (turns, criterion)}` file under version control.
- A/B prompt variants by **string replacement on the real prompt builder** with an `assert old in prompt` guard, or
  import the builder from two source trees (old commit vs working tree). Never hand-copy prompts.
- Seed history **without** `HistoryConfig(initial_history_in_client_content=True)`; with it, the first content is
  treated as history and the trigger gets no reply on 3.8 (`references/02-gemini-live-model-and-config.md`).
- Output `SUMMARY <variant> <moment>: k/N`. N = 6-12 per variant per moment is the usual working size (see section 10).
- Measured: paired questions at three real moments 3/12 -> 0/12 after the capitalised one-question rule with its
  reason. Note that 3/12 vs 0/12 alone is not statistically significant (Fisher p = 0.22); it was accepted because
  the failure was reproduced at three separate moments and confirmed in full simulations.

## 5. Layer 3: full simulated calls

`templates/testing/simulate_calls.py` runs LLM caller personas against the real Live agent and judges each
transcript with named boolean checks, with an old-vs-new prompt A/B in one command:

```bash
python simulate_calls.py --personas personas.json --prompt old=old.txt --prompt new=new.txt \
    --greeting "Hello, you've reached ... How can I help?" --runs 6 --concurrency 5 --out sims/
```

Loop per call: caller line = text LLM(persona + "Call so far: ... You:"), sent to Live as a user text turn with
`turn_complete=True`; agent reply = output transcription until `turn_complete`; stop on `[HANGUP]`, the end-call
tool, or 30 turns. The greeting is seeded as a **model** turn because production plays it from cache.

**Personas** - model them on real callers from test calls, with fake PII:

| Persona | Exposes |
|---|---|
| Terse caller who answers only what is asked, gives a vague complaint ("very bad quality") until offered options | over-long questioning, re-asking, menus that prime answers |
| Non-English caller who gives name and shop in the first sentence | re-asking already-given details, wrong-language first reply |
| Impatient caller in mixed language who threatens to hang up | defensive stock lines, failing to cut to essentials |
| Caller who leaves early / refuses the address | commitments made without mandatory details |
| Caller without the product / with an expired one | invented remedies, missing date logic |
| Worried parent / safety (injury, glass) caller | tone, urgent path, forbidden promises |
| Caller answering in short English inside a non-English call ("No, same number") | language drifting to English at the close |

**Checks** are behavioural booleans with the pass condition spelled out: `one_question`, `language_held`,
`mandatory_before_confirm` ("Find the first agent turn that registers the complaint or gives a reference. Before it,
the agent had asked for X and Y; or there was no such turn."), `no_reask`, `anything_else_once`, `close_numbers`,
`address_reason_first`, `caller_stayed`.

**Objective metrics** (no LLM, computed by the harness): replies with more than one `?`, words per reply,
empty agent turns, text-turn -> first-audio time, and **English-only reply ratio in non-English calls** (no
non-Latin letters and >= 25% English function words, so romanised Hindi does not count as English). Measured on the
production prompt: 1/100 to 1/115 English-only replies in Hindi calls; with `language_code=en-IN` set, 6-12% vs 2-4%
without - the evidence that turned the language hint off by default.

**Matrix runs**: prompt old/new x config (e.g. language hint on/off) x persona x N, files named
`sim_<label>_<persona>_<i>.json`; keep the transcripts, because you will re-judge them (section 10).

Limits: text in, text out. It does not test hearing, echo, noise, turn-taking or latency, and output transcription
is lossy (an empty agent turn in a sim can be a transcription loss, not silence). A variant that synthesises caller
audio with a TTS and streams it is closer, but TTS callers are cleaner than real ones.

**LiveKit's simulator** (`lk agent simulate --scenarios scenarios.yaml`, and `lk agent simulate audio` with
`--background-noise`, `--low-quality-microphone`, `--packet-loss`; scenario YAML with `agent_description`,
`scenarios[].instructions`, `agent_expectations`) runs your actual agent code including LiveKit hooks. Measured
gotchas: the hosted audio-simulation quota ran out mid-campaign, and verdicts were exported without transcripts (add
a dev-only transcript dump in `on_session_end`). In text simulation mode skip timers such as the silence monitor
(`ctx.simulation_context().simulation_mode == SIMULATION_MODE_TEXT`). VERIFY current flags with `lk agent simulate
--help` (CLI 2.18.5 / 2.19.1 at time of writing).

## 6. Layer 4: replaying real caller recordings

The single most valuable test for phone-specific bugs. `templates/testing/replay_recording.py`:

1. Splits the stored stereo recording (LiveKit RecorderIO: **caller ch0, agent ch1**) and decodes the caller
   channel to 16 kHz PCM16 mono (`ffmpeg -af "pan=mono|c0=c0" -ar 16000 -f s16le`).
2. Streams it into a fresh Live session **at real-time pace in 100 ms chunks**, exactly as production would,
   then keeps streaming silence (Gemini's VAD never emits ACTIVITY_END on a stream that stops).
3. Applies the production greeting timing: the greeting window, then pushing the greeting text into context as a
   model turn when it finished uninterrupted.
4. Logs timestamped `HEARD` (input transcription), `SAID` (output transcription), `FIRST_AUDIO`, `INTERRUPTED`,
   `TURN_DONE`, `ACTIVITY_START/END`, `TOOL_CALL`, and flags a phantom reply during the greeting.

Variants are flags: `--greeting-mode open|gate|hold`, `--custom-vocab`, `--language-code`, `--language-codes`,
`--start-sensitivity`, `--end-sensitivity`, `--silence-ms`, `--voice`.

**Greeting echo test.** Phone lines carry the greeting's own echo back on the caller channel and LiveKit has no AEC
for SIP participants. Replaying pre-fix recordings with `--greeting-mode open` reproduced the agent answering a
phantom utterance over its own greeting **24/24**; holding caller audio fixed it (**0/57**). Run it on every
greeting, voice or VAD change.

**Barge-in test.** Mix a real caller utterance (from another recording) into an echo-only greeting window:

```bash
python replay_recording.py echo_call.ogg --prompt system.txt --greeting-text "..." --greeting-seconds 8.6 \
  --seconds 25 --greeting-mode hold --mix other_call.ogg --mix-from 11.4 --mix-to 16.2 --mix-at 3.0 \
  --barge-in-at 3.15 --barge-in-threshold 0.8 --replay-lead 0.6 --runs 3
```

Pass = the caller's words appear in `HEARD` and the next `SAID` answers them. Take `--barge-in-at` from your local
VAD over the mixed audio (section 7); `auto` is a crude energy detector.

**Language trade-off test.** Replaying real mixed-language recordings showed an English first reply to a Hindi
caller **4/7 with `language_code=en-IN` vs 0/10 without** (Fisher p = 0.015) - a measurement text sims could not
have produced. Also used for: custom vocabulary on/off (does a brand word still transcribe; does a common word get
pulled into a brand), and `START_SENSITIVITY_LOW` (barge-in still ~0.57 s; no help against echo).

What replay does not cover: your agent-side code (LiveKit hooks, hold class, close logic). Pair it with unit tests
of that code and with real calls.

## 7. Local VAD tests on recordings

If a local VAD (e.g. Silero in LiveKit) gates anything - greeting barge-in, hang-up interlocks, latency anchors -
calibrate its thresholds on **real caller channels**, not synthetic audio:

```python
vad = silero.VAD.load(min_speech_duration=0.1, min_silence_duration=0.25, prefix_padding_duration=0.5,
                      activation_threshold=0.5, sample_rate=16000, force_cpu=True)   # production settings
stream = vad.stream()
for i in range(0, len(x) - 320, 320):                       # 20 ms frames of 16 kHz int16
    stream.push_frame(rtc.AudioFrame(x[i:i+320].tobytes(), 16000, 1, 320))
stream.end_input()
async for ev in stream:
    if ev.type == VADEventType.START_OF_SPEECH: start = ev.timestamp - ev.speech_duration
    elif ev.type == VADEventType.END_OF_SPEECH: segments.append((start, ev.timestamp, ev.speech_duration))
```

Two measurements that set the production thresholds:

1. **Longest false burst** over echo-only greeting windows (echo, office chatter, hold noise): at most **0.70 s**
   -> greeting barge-in threshold **0.8 s** of continuous speech.
2. **Detection time of a real interruption**: a real sentence mixed in at 3.0 s was detected in **0.15 s**
   -> replay lead **0.6 s** before the VAD start so no word is clipped.

Re-run when you change noise cancellation, the carrier, or VAD settings. Remember `min_silence_duration` delays
the end-of-speech event; subtract it when you use the event as "caller stopped".

## 8. Blind voice and tone judging

Voice, accent and tone live in the audio. `templates/testing/voice_ab.py` renders the same moments across voices x
prompts x language hints, saves 24 kHz WAVs under random ids, and asks a multimodal model to rate each blind:

```python
Q = ("This is one reply from a voice agent on a consumer-care line. The owner wants a calm, natural, professional "
     "conversational tone, like an experienced call-centre executive, and complained that the voice sounds dramatic. "
     "Listen and rate strictly. JSON: {\"dramatic\": 1-5 (5 = theatrical: exclamations, pitch spikes, breathy "
     "sympathy, sing-song), \"professional\": 1-5, \"human_like\": 1-5, \"<locale>_accent\": 1-5, \"notes\": \"short\"}")
r = judge.models.generate_content(model="gemini-3.8-flash",
        contents=[Q, types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
```

- **Moments**: the greeting (rendered with the greeting-render prompt), an upset caller, a safety complaint, a plain
  question, a digit read-back; each in every caller language.
- **Rubric**: contrast-anchored ("5 = theatrical: exclamations, pitch spikes ...") and "rate strictly". An earlier
  un-anchored rubric scored everything 9-10/10 and separated nothing.
- **Blind**: random file ids; the judge never sees voice or prompt names; shuffle order. Optionally judge the
  narrowband version (`--narrowband`: 300-3400 Hz, 8 kHz mu-law), which is what callers hear.
- **Measured uses**: first real call judged American-accented 6/6 -> a dedicated accent block after the persona gave
  Indian 5/5 in 3/3 sessions; emotional moments dramatic 4.6 -> 2.6/5, professional 2.1 -> 3.4 after the calm-tone
  block; final voice chosen at dramatic 2.9 vs 3.9, professional 3.4 vs 2.6.
- **Objective companion**: autocorrelation F0 per channel (8 kHz, octave-error filtering), semitone spread p10-p90.
  Human agents on the same line spread 6-8 semitones vs the bot's 8-11 - an objective version of "dramatic". A crude
  F0 check also flags voices that sit in an unexpected pitch range on a narrowband line.
- Listen to the top two configurations yourself before deciding. Judges are a filter.

## 9. Recording forensics

**Energy-based timing** (`templates/testing/measure_reply_gaps.py`): per-channel energy segmentation of the stereo
recording gives speech on/off times independent of any SDK metric. Equivalent quick look with ffmpeg:

```bash
ffmpeg -i call.ogg -af "pan=mono|c0=c1,silencedetect=noise=-40dB:d=0.35" -f null - 2>&1 | grep silence_
```

It reports greeting onset (measured 1.2-2.5 s after recording start on every call), greeting duration,
caller-end -> agent-start gaps (p50/p90/max), callers' think time after the agent stops (use it to size the hang-up
grace window; a caller thinking in Hindi was cut 2.75 s after a question), talk-over events, and >2 s gaps. It is the
tool that proved a hang-up on a talking caller (caller speech 503.47-504.85 s, close at ~506.2 s). RecorderIO
recordings are taken at the agent, so add the PSTN/SIP legs (~0.1-0.3 s each way) for what callers experience. The
caller channel carries the agent's echo; check talk-over flags by listening, or use `--echo-margin`.

Why not SDK metrics: on the realtime path `RealtimeModelMetrics.ttft` reads 0.000-0.013 s on every turn (measured
from the first server message, which already carries audio), and `ChatMessage.metrics` lack `e2e_latency`. See
`references/03-conversation-mechanics.md` for in-process measurement from local VAD end to first audio.

**Audio-LLM listen pass**: send both channel WAVs to a multimodal model with a forensic prompt:

```text
File 1 is the CALLER channel, file 2 is the AGENT channel (an AI voice agent). Both start at 0:00.
1. Timestamped transcript of BOTH channels merged in time order: [mm:ss] CALLER/AGENT: exact words, in the script
   spoken. Include sounds on the caller channel before the first real words (noise, IVR, echo of the agent).
2. Mark every place the agent: speaks when the caller said nothing; talks over the caller; reads out labels,
   brackets or notes ("The user...", "no speech"); answers something never said; switches language.
3. Agent voice: accent, words per second (greeting and typical replies), intonation, what sounds robotic, with
   timestamps. Score human-like 1-5.
4. Every gap over 2 s where the caller has finished and the agent has not started.
Do not guess words you can't hear; write [inaudible].
```

Its caller-channel transcript is also a **second ASR opinion**: it matched what callers said next ("I said X, not
Y") where Live's input transcription was wrong. Join results with your per-call events (idle time before the call,
prerendered greeting, close reasons) before concluding.

## 10. Judge pitfalls and sample sizes

| Pitfall | Measured symptom | Mitigation |
|---|---|---|
| Language misjudged | Judges repeatedly scored romanised-Hindi transcripts as English | Say "romanised or native script; English loanwords fine" in the check; use the objective English-only metric; hand-check every language verdict |
| Lenient, un-anchored rubric | Every voice 9-10/10 | Contrast anchors per score, "rate strictly", 1-5 not 1-10 |
| Over-strict wording | A check fails replies a human would pass | Spell out the pass condition and what does not count ("saying I will note this while still asking does NOT count") |
| Judge variance | Same transcript flips between runs | `temperature=0`, JSON schema, `--judge-repeats 3` majority vote on contested checks; report unanimity |
| Self-preference | Judge and agent from the same family agree too easily | Re-judge a sample with a different model; keep transcripts so re-judging is cheap |
| Transcript artefacts | Lossy/late output transcription; empty agent turns; `<no speech>` placeholders | Strip placeholders; treat empty turns as "unknown", not "silent"; confirm with audio when it matters |
| Small n | 3/12 -> 0/12 looks decisive | Use intervals and exact tests (below) |

**Sample size.** Use the Wilson 95% interval (printed by `simulate_calls.py`) and Fisher's exact test for A/B:

| Result | 95% interval for the true rate |
|---|---|
| 0/6 | 0-39% |
| 0/12 | 0-24% |
| 0/24 | 0-14% |
| 0/57 | 0-6.3% |
| 1/100 | 0.2-5.4% |
| 3/12 | 9-53% |
| 6/12 | 25-75% |

| A/B example (measured) | Fisher two-sided p |
|---|---|
| paired questions 3/12 -> 0/12 | 0.22 (suggestive only) |
| English first reply with hint 4/7 vs 0/10 | 0.015 |
| commitments after mandatory details 2/15 -> 21/24 | < 0.0001 |
| phantom greeting reply 24/24 -> 0/57 | < 0.0001 |

Guidance: to show a failure rate falling from ~30% to ~5% at p < 0.05 you need roughly **30-40 runs per arm**;
6-12 is enough only for large effects or as a screen. Spread runs across several moments/personas rather than
repeating one, re-run the winner fresh before shipping, and quote k/n (never a bare percentage) in commit messages.

## 11. Real test calls: review checklist

Before the call:
- [ ] Wake the deployment (scale-to-zero plans add 10-20 s cold start; place a throwaway call first) and confirm
      health lines in the logs (`references/11-deployment-ops-and-integrations.md`).
- [ ] Script the call: language, persona, which moments you are testing; note the time.
- [ ] Recording, transcript storage and CRM writes in the mode you intend (dry-run vs marked test ticket).

Per call, review in this order:
- [ ] **Carrier/SIP**: provider log shows the call; LiveKit telephony log shows it; IDs cross-referenced; time to
      answer (rings); clean BYE in both directions.
- [ ] **Opening**: greeting started within ~1-2.5 s of answer; `prerendered=true`; no phantom reply over the greeting;
      caller can interrupt it.
- [ ] **Hearing**: input transcript vs what was said (listen pass); brand/product words; digits and postcodes.
- [ ] **Language**: first reply in the caller's language; held through read-backs, close and goodbye.
- [ ] **Turn-taking**: reply gaps p50/p90 (`measure_reply_gaps.py`); talk-over; cut-offs of the caller.
- [ ] **Behaviour**: one question per reply; nothing re-asked; no invented actions ("I have registered...");
      commitments only after mandatory details; no notes or labels spoken aloud.
- [ ] **Voice**: accent, pace, tone at emotional moments; any one-turn voice change.
- [ ] **Silence and ending**: check-in in the call's language; goodbye never clipped; no speech after goodbye; line
      actually hung up; no hang-up on a talking caller.
- [ ] **Long call** (> 9-10 min): GoAway/reconnect survived; no duplicated history; context intact.
- [ ] **After the call**: ticket filed with the right fields (read back from the CRM), caller number present,
      recording uploaded and playable, transcript rows, analytics events, cost event.

## 12. What to log on every call

Logs and events are your test data in production. Minimum set (PII-free unless stated):

| Event / log line | Fields | Why |
|---|---|---|
| `call_started` | pipeline that actually answered, release version, live location, warmup state (`ok`/`unprobed`/failed) | split dashboards by pipeline; spot cold calls |
| `greeting_played` | `prerendered`, `duration_ms`, `interrupted` | opening cost is otherwise invisible (nothing before dispatch is logged) |
| `user_turn_completed` | turn index, language label if trustworthy | realtime has no `on_user_turn_completed`; emit from final `user_input_transcribed` or calls show zero caller turns |
| `turn_latency` | `e2e_latency_ms` (local VAD end -> first audio), `after_server_vad_ms`, pipeline | the only honest latency on realtime |
| `provider_error` | error type, provider, model, layer, recoverable, stage | per-turn latency is survivor-biased; a call with seven 502s looked healthy |
| `silence` / `call_closing` / `call_close_cancelled` | stage, close reason, decided by | audit hang-up decisions |
| `call_ended` | duration, ended by (caller/agent/timeout/error) | |
| `model_usage` | input/cached/output tokens, audio seconds (from the typed usage object) | cost per call |
| `call_summary` | route, ticket status, contained, handed off, missing slots count, QA passed, assignee resolved (from CRM response) | outcome dashboards |
| Masked SIP attributes | which keys arrived (`sip.phoneNumber`, ...) with values masked `***1234` | one real call settles what the platform sends |
| Recording + transcript (restricted store) | stereo audio, turns with timestamps | replay and forensics (sections 6, 9) |

Pipeline details (outbox, Pub/Sub, BigQuery, PII validation) are in
`references/11-deployment-ops-and-integrations.md`.

## 13. An acceptance bar that worked

Used to promote the realtime (Gemini Live) pipeline over a cascaded STT -> LLM -> TTS one:

- p50 end-of-speech -> first audio below the cascaded p50 (measured cascaded 3.17 s over 196 real turns; realtime
  1.2-1.6 s typical);
- zero wrong-language first replies across non-English scenarios (sims + replays);
- zero hang-ups on a talking caller (forensics on every test call);
- a ticket filed on every complaint call (read back from the CRM);
- client scenarios passing at least as many checks as the old pipeline;
- real calls in every language, a sensitive/health call, a "repeat the reference" close, a question-not-complaint
  call, and one call longer than 9 minutes (GoAway reconnect).
