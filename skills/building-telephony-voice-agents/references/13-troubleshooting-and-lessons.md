# Troubleshooting and hard-won lessons

Purpose: a symptom -> cause -> fix lookup for phone voice agents on Gemini Live, LiveKit, SIP trunks and provider
WebSocket media streams. Rows come from a production inbound care line (measured on `gemini-3.8-live` via Vertex,
livekit-agents / livekit-plugins-google 1.7.1, google-genai 2.21.0, Indian PSTN over SIP) and from vendor docs and
community reports (marked COMM / VERIFY where unconfirmed). Find the area, match the symptom, apply the fix, then
prove it with a test from `references/10-testing-and-evaluation.md`. Explanations live in the sibling references
linked in each section; this file stays terse.

How to start any investigation: (1) get the stereo recording and the event timeline for the call, (2) run
`templates/testing/measure_reply_gaps.py` and an audio-LLM listen pass, (3) check the provider's call log and the
LiveKit telephony/session log, (4) only then read code.

## Contents

1. [No audio, dead air, silence](#1-no-audio-dead-air-silence)
2. [Gemini Live session setup and lifecycle](#2-gemini-live-session-setup-and-lifecycle)
3. [LiveKit agent framework and the Google realtime plugin](#3-livekit-agent-framework-and-the-google-realtime-plugin)
4. [Greeting, echo and barge-in](#4-greeting-echo-and-barge-in)
5. [Ending the call, silence handling](#5-ending-the-call-silence-handling)
6. [Turn-taking and latency](#6-turn-taking-and-latency)
7. [Language, accent, voice and tone](#7-language-accent-voice-and-tone)
8. [Prompted behaviour](#8-prompted-behaviour)
9. [Transcription and recognition](#9-transcription-and-recognition)
10. [Tools](#10-tools)
11. [SIP trunking and carriers](#11-sip-trunking-and-carriers)
12. [WebSocket media streams](#12-websocket-media-streams)
13. [Audio formats and quality](#13-audio-formats-and-quality)
14. [Deployment and operations](#14-deployment-and-operations)
15. [Post-call, CRM, analytics, recordings](#15-post-call-crm-analytics-recordings)
16. [Testing and judging](#16-testing-and-judging)

---

## 1. No audio, dead air, silence

| Symptom | Likely cause | Fix |
|---|---|---|
| Output transcript arrives but **zero audio chunks**, every turn | `thinking_budget=0` or `thinking_level=MINIMAL` on `gemini-3.8-live` (reproduced 3/3) | Never send thinking config; pin with a unit test. `references/02-gemini-live-model-and-config.md` |
| Some turns have a transcript but all-zero/empty audio, `turn_complete` sometimes missing | Known 3.8 "silent audio turn" issue (COMM forum 186220, both backends) | Detect transcript text with near-zero PCM energy for > ~1.5 s; regenerate with `send_client_content(..., turn_complete=True)`; track rate per call |
| Dead air from the greeting on after a "harmless" config change; all tests green | Provider rejects an option on every request (seen: a TTS option -> `INVALID_ARGUMENT`) | Live provider check in the deploy gate that speaks to the real API (`references/10-testing-and-evaluation.md` §3) |
| Greeting plays, then silence for the whole call | Live unreachable / wrong region / connect failed and nothing fell back | Prewarm probe with region fallback, and fall back to a cascaded pipeline if both fail; `RealtimeModelFallbackAdapter` in-call |
| Caller hears ringing for 10-20 s, or hangs up before answer | Scale-to-zero cold start; network work inside prewarm | Keep production warm (paid plan) or wake-up call; move network warm-up to a daemon thread (§14) |
| Turn never finishes | Small `max_output_tokens` (audio tokens ~25/s count against it) | Leave it unset |
| Custom bridge: caller speaks, model never answers | Bridge stops sending frames in silence, so server VAD never emits ACTIVITY_END; or VAD disabled with no activity markers | Stream continuously (send silence frames), or `audio_stream_end=True` after > 1 s pause; with manual VAD always send `activity_start`/`activity_end` |
| Silence after a mid-call tool change or long call | Reconnect in progress (~2.2 s) on `update_tools` / GoAway | Register tools before start; see §2 for GoAway |
| Agent session ended but caller still on a silent line | `AgentSession.shutdown()` does not drop the SIP leg | `RoomOptions(delete_room_on_close=True)` (§5) |
| Agent silent with a single log line after adding a tool | Bad tool schema raised outside the plugin's `try` (livekit/agents#7371, fixed 1.8.3) | Flat schemas (str/int/enum); upgrade or validate schemas in a unit test |

## 2. Gemini Live session setup and lifecycle

| Symptom | Likely cause | Fix |
|---|---|---|
| Close `1008 Publisher model ... was not found` | Region: 3.8 Live is served only in `us`, `eu` (multi-region) and `us-central1` on Vertex; not `global` or Asia regions | `location="us"` (connects ~0.6-0.9 s faster than `us-central1` from India), fallback `us-central1`; validate location in config. A REST catalogue listing is not proof - only `connect()` is |
| `1007 Affective dialog is not yet supported on Beyond backend` | `enable_affective_dialog=True` | Remove; always on in 3.8. Same for `proactivity` (accepted, ignored) |
| `1007 Unsupported modality for Vertex Live API` | `response_modalities=["TEXT"]` or `["AUDIO","TEXT"]` | `["AUDIO"]` only; use output transcription for text. Half-cascade (Live listens, separate TTS speaks) is not possible on Vertex 3.8 |
| `1007 No matching speaker voice found` | Unknown voice name (e.g. Chirp-style `en-IN-Chirp3-HD-Kore`) | Use a prebuilt Live voice name (case-insensitive); validate against the plugin's list |
| `Unknown name "speaking_rate"` / `word_timestamp` / `diarization` at setup | Not supported on 3.8 even though the SDK has fields | Remove; control pace in the prompt |
| `gemini-3.8-live-extended-thinking` fails | Not served on Vertex (failed in every location tried); Gemini API only per docs | Use `gemini-3.8-live` |
| Calls die at ~15 min | Audio-only sessions without compression are capped at 15 min | `context_window_compression=SlidingWindow(target_tokens=64_000)` always on; cap call length (900 s used) |
| `1011 The service is currently unavailable` ~10 min into a call | Connection lifetime ~10 min: GoAway at ~542 s (`time_left=30s`), close ~602 s; compression does not change it | Session resumption (handle from `session_resumption_update`, reconnect ~2.2-2.3 s, context restored); buffer caller audio during the swap |
| After ~9 min every turn appears twice / model repeats itself | Plugin 1.7.1 replays local history on a resumed connection (livekit/agents#6000, fixed 1.8.1) | Upgrade, or wrap `live.connect` and drop the first `turn_complete=False` client content on a resumed connection |
| Mid-call instruction update makes the model weird / costs ~21k tokens | On Vertex `update_instructions()` sends the whole prompt as a `role="model"` turn | Resolve per-call facts (caller number, date) before `session.start`; late facts as a short `update_chat_ctx` note, never a system-role message (dropped) |
| History-seeded session gives no reply to the first trigger | `HistoryConfig(initial_history_in_client_content=True)` makes the first content count as history | Don't set it; seed with `send_client_content(turn_complete=False)` then a `turn_complete=True` turn |
| One failed connect drops the call (half greeting, dead line) | Plugin does not retry a failed **first** connect; RoomIO then deletes the room | `RealtimeModelFallbackAdapter([primary_region, fallback_region])` sharing one signals object; prewarm probe |
| `1007` treated as fatal mid-call | Plugin maps 1007 to "context exhausted", not retried | Find the offending config; keep context compression on |
| Connect takes 2.7-3 s | Large system prompt (88k chars ~ 21k tokens) and per-connect credential refresh | Trim the prompt (cut to ~38k chars); pass pre-refreshed credentials (connect 1.46 -> 1.27 s); hide connect behind a cached greeting |
| Language/locale string accepted but no effect, or a typo accepted | `speech_config.language_code` accepts any string and does not enforce output language | Validate BCP-47 shape yourself; enforce language in the prompt (§7) |

## 3. LiveKit agent framework and the Google realtime plugin

| Symptom | Likely cause | Fix |
|---|---|---|
| `ValueError` at startup after a rebuild | `livekit-plugins-google` 1.8.2-1.8.4 reject `vertexai=True` + short name `gemini-3.8-live` (fixed in 1.8.5+); image installed from `pyproject.toml` with a loose pin | Pin `==` exactly and test the pins; use 1.7.1 or >= 1.8.5, or on 1.8.2-1.8.4 pass `publishers/google/models/gemini-3.8-live` (or the full `projects/<p>/locations/<l>/...` resource name) |
| `session.say("text")` raises with Gemini | Realtime model has `supports_say=False` without a TTS | `session.say(text, audio=cached_frames)`, or `generate_reply(instructions="Say ...")` |
| `allow_interruptions=False` raises / is ignored | Server-side turn detection is on for Google realtime | Accept interruptible speech; gate audio another way (§4) |
| Turn detector, endpointing, `min_words`, `false_interruption_timeout`, `preemptive_generation` have no effect | Inert with a Google realtime model (server VAD owns turns) | Don't tune them on realtime; tune server VAD knobs or the prompt |
| `on_user_turn_completed` never fires; `tts_node` never called | Realtime path commits no user turn; audio goes through `realtime_audio_output_node` | Emit user-turn events from final `user_input_transcribed`; post-process output in `transcription_node` only |
| `user_state` says "speaking" when the caller has stopped; `ttft` ~0 s every turn | Plugin 1.7.1 drops `voice_activity`, thought tokens, interim transcripts; ttft measured from first server message | Pass an explicit Silero `vad=` to `AgentSession`; tap the SDK `receive()` for ACTIVITY_START/END and first audio |
| Client-side turn detection answers off by one (model answers a "." or repeats the greeting) | Manual activity mode through the plugin is broken on 3.8 (1.7.1 sends a "." turn; 1.8.3 a bare `turn_complete`) | Keep server VAD |
| Agent joins every room / wrong rooms | No `agent_name` on `@server.rtc_session` -> automatic dispatch | Always set `agent_name` and dispatch explicitly (SIP dispatch rule `roomConfig.agents`) |
| Deprecation warnings: `WorkerOptions`, `RoomInputOptions` | Old API (pre-1.8 blog posts) | `AgentServer` + `@server.rtc_session`; `room_options=room_io.RoomOptions(audio_input=...)` (`references/04-livekit-cloud.md`) |
| `session.start(participant=...)` TypeError | No such kwarg | `RoomOptions(participant_identity=...)` |
| Two resamples on input, extra CPU | RoomIO default input 24 kHz, plugin wants 16 kHz | `AudioInputOptions(sample_rate=16000, frame_size_ms=50)` |
| `pre_connect_audio=True` does nothing on calls | Works only with client-SDK tracks, not SIP | Ignore for telephony |
| Thinking/typing sound never plays on realtime | Plays only while `agent_state=="thinking"`, which on realtime happens only during tool execution | Use it only on cascaded or around slow tools |
| Late model audio dropped with a warning | Output stream closes on `generation_complete`, not `turn_complete` | Expected; nothing to do |
| After a barge-in the model "remembers" saying words the caller never heard | `truncate()` is a no-op on Gemini; the server keeps the whole reply | No fix in the plugin; local history keeps only played text - don't rely on server memory of interrupted turns |
| `generate_reply` greeting waits ~5 s then errors on a slow connect | Plugin waits 5 s for `generation_created` | Play a cached greeting with `say(text, audio=...)`, so connect time is hidden |
| Any tool or voice change mid-call adds a ~2.2 s gap | `update_tools` / `update_options(voice, temperature, tool_behavior)` reconnect the Live session (`mutable_tools=False`) | Decide tools and options before `session.start` |
| Process-wide state leaks between calls | `JobProcess.userdata` outlives a call | Key per-call values by job id; pop on read; lock shared singletons |
| Outbound call: callee unavailable but session stays open | `USER_UNAVAILABLE` / `SIP_TRUNK_FAILURE` do not auto-close `AgentSession` | Catch `api.SipCallError` / disconnect reason and call `ctx.shutdown()` |

## 4. Greeting, echo and barge-in

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent answers a phantom utterance over its own greeting ("I can help you with ...") | PSTN line echo of the greeting reaches Gemini's server VAD; LiveKit has no AEC for SIP participants | **Hold** caller audio during the greeting; at the end wait an echo tail (0.4 s) and discard; measured 24/24 -> 0/57. `references/03-conversation-mechanics.md` |
| Caller can't interrupt the greeting, or first words lost | Dropping caller audio during the greeting; LiveKit `aec_warmup_duration` (3 s default on inbound SIP) replaces caller audio with silence | Hold-not-drop; on >= 0.8 s continuous local-VAD speech release and **replay** from 0.6 s before speech start, and interrupt the greeting; `aec_warmup_duration=None` |
| Barge-in threshold fires on echo, or misses real speech | Threshold not calibrated on real lines | Measure on recorded caller channels: echo/chatter bursts max 0.70 s, real speech detected in 0.15 s -> 0.8 s threshold (`references/10-testing-and-evaluation.md` §7) |
| Caller's first reply cancelled after they talked over the greeting | Pushing the greeting into context (`update_chat_ctx`) after an interrupted greeting cancels the in-flight reply | Push greeting context only `if not handle.interrupted` |
| Gemini doesn't know it greeted (repeats the greeting / re-introduces itself) | `say(audio=...)` only updates the local chat context | After uninterrupted playout, `update_chat_ctx` to push the assistant message; also state in the prompt that the greeting was played |
| Greeting paraphrased or with a preamble ("Sure, here it is") on every call | Live does not always read a fixed line; the bad render was cached | Verify the render's transcript (>= 70% of words, <= 3 extra) before caching; render with the accent instruction |
| Greeting blocks forever; call cancelled ~35 s later | `wait_for_playout()` never returns when no participant is linked | Bound the participant wait (8 s) and playout wait (20 s) |
| Slow first words (> 1 s) on a fully generated greeting | Connect + generation; realtime first utterance is usually > 1 s | Play a cached greeting rendered at prewarm in the same Live voice while Gemini connects |
| Caller speech during a WS-bridge greeting interrupts it unintentionally | Same echo/VAD issue on a bridge | Gate or hold inbound audio during the greeting (send silence to keep VAD alive) (COMM gemini-live-api-examples#53) |
| Barge-in takes ~0.5 s to stop the agent | Gemini `interrupted` arrives ~0.5-0.58 s after voice onset; `START_SENSITIVITY_LOW` ~0.57 s | Expected; on WS bridges flush local queue **and** send the provider `clear` immediately (§12) |
| Noise cancellation clips "haan", "ji", digits | NC can cut soft onsets | A/B on real calls; disable only if clipped first syllables appear. Use one NC method only (not BVC in code and Krisp at the trunk) |

## 5. Ending the call, silence handling

| Symptom | Likely cause | Fix |
|---|---|---|
| Line never hangs up after goodbye | `end_call` tool returned a string: LiveKit holds `agent_state="thinking"` waiting for a reply that never comes, so the "listening" edge that arms the close never fires | Return `None` from the tool; `await context.wait_for_playout()` first |
| Caller hears "I have ended the call" or a "no speech" placeholder after goodbye | Vertex answers even an empty tool result; SILENT scheduling dropped | While a close is armed or pending, `speech_handle.interrupt(force=True)` on every `speech_created` |
| Model writes an end marker instead of calling the tool | Marker instructions still in the realtime prompt | Remove all marker text from the realtime prompt; "say goodbye, then call end_call, say nothing after" |
| Caller cut off mid-question after the farewell | Close cancel waited for a `speaking` edge that had already happened | Decide at **commit** time: cancel if `user_state=="speaking"` or caller speech within 1 s before arming; close generations so stale tasks can't act |
| Caller cut ~2.75 s after "anything else?" | Goodbye ended on a question; grace 2.5 s too short | Prompt: goodbye never asks a question, "anything else" is its own reply; grace 4 s; caller speech cancels |
| Caller left on a silent open line | `shutdown()` doesn't touch the SIP leg | `RoomOptions(delete_room_on_close=True)` (deletes after drain); never hand-roll `ctx.delete_room()` after `shutdown()` (clips goodbye; the delete gets cancelled in teardown) |
| Call sits open until max duration after a cough post-goodbye | Cancelled close did not return the lifecycle to listening | `cancel_pending_close()` resets to LISTENING |
| Silent caller never checked on | `user_away_timeout` (15 s) flips `user_state` to "away" before the 20 s check-in; monitor accepted only "listening" | Treat "away" as the caller holding the floor |
| Silent non-English caller checked on in English | Fixed English check-in strings | `generate_reply(instructions="In the language you have been speaking ... one short sentence")` |
| Silence check-in hangs up two stages early / gives the reference number | Prompt's closing rules leak into system-driven turns | Every system-driven instruction says "this is not the end: do not give the reference, do not ask anything else, do not call end_call" |
| Check-in repeats forever | Check-in reset the silence clock/ladder | Leave clock and stage alone after a check-in |
| Model never speaks to a silent caller | Gemini does not prompt on its own (12 s probe) | Silence monitor (20 s check-in, 40 s warning, 60 s close used) |
| Goodbye clipped | Room deleted before playout finished | `shutdown(drain=True)` + `delete_room_on_close`; on WS bridges wait for the provider `mark` before hanging up |

## 6. Turn-taking and latency

| Symptom | Likely cause | Fix |
|---|---|---|
| ~1.5-1.8 s from caller stop to first audio | Server VAD end-of-speech + the India-US hop dominate (default measured median ~1.6 s from India) | Accept, or test `END_SENSITIVITY_HIGH` + `silence_duration_ms` 250-400 (one probe set 1.24-1.32 s, another no gain); shorter silence cuts off callers reading codes |
| Latency metric shows the fastest turns missing | Gemini replied before the local VAD's min-silence elapsed and those samples were dropped | Hold first-audio time and measure when the VAD end arrives |
| Fake 250-400 ms latency samples | Backchannels during agent speech anchored "end of speech" | Anchor only when the agent isn't speaking; ACTIVITY_START resets anchors |
| ACTIVITY_END-based latency reads ~0 s | ACTIVITY_END often arrives in the same message as the first audio | Measure from local VAD end minus `min_silence_duration` |
| Reply held back on a noisy line | Realtime playout waits on the local VAD's user-silence event | Tune local VAD or noise cancellation; check `user_state` stuck in "speaking" |
| Extra ~0.5-1 s on every turn with an agent near the model | Agent placed in the US for a US model: the ocean crossing moves onto lossy UDP media | Keep the agent/bridge in the SIP media region; the model hop is a TCP WebSocket |
| Speech speeds up in long replies | Live accelerates past ~60 s of speech | Keep turns short: prompt for under ~8 s / ~60 words; Google's ceiling is 100 words / 50 s |
| Agent talks over callers who pause mid-sentence | Short silence window or `END_SENSITIVITY_HIGH` | Default VAD; give the model a rule to wait for codes/addresses |

## 7. Language, accent, voice and tone

| Symptom | Likely cause | Fix |
|---|---|---|
| Non-English caller answered in English (esp. first reply) | English greeting biases the model | Capitalised language rule straight after the role ("RESPOND IN THE LANGUAGE THE CALLER IS SPEAKING ... FROM YOUR VERY FIRST REPLY"), first-reply rule, whole-reply rule (measured 4/14 -> 0/8; 1/100-1/115 English-only replies in final sims). `references/09-prompting-voice-agents.md` |
| Setting `language_code` made language switching worse | Measured English first reply to Hindi callers 4/7 with `en-IN` vs 0/10 without | Leave `language_code` off by default; fix accent in the prompt |
| Close drifts into English after short English answers ("No, same number") | Model treats short English as a switch | Rule: short English answers inside a non-English call don't change the language; switch only on request or two whole sentences running |
| American accent | Live voices default to General American English (first call judged American 6/6) | Dedicated accent block right after the persona (one line was drowned out); same instruction in the greeting render prompt |
| "Sounds dramatic" / theatrical sympathy | Prompt asked for fresh reactions, "melody", feeling words | Level intonation, care in words not voice, plain repeated acknowledgements, no exclamations; calmer voice chosen by blind judging (dramatic 4.6 -> 2.6/5) |
| Too fast (~4 words/s) | Default pace; no rate knob; "speak slowly" barely works (~-19%) | Keep turns short; slow only for numbers/addresses |
| Masculine forms for a female persona in gendered languages | Model default | State the persona's grammatical gender (went to 0 after the rule) |
| Voice changes for one turn | Known 3.8 limitation | None; rare |
| A word from another language appears (e.g. Tamil word in a Kannada reply) | Language outside the officially supported list | Native-speaker QA before launching that language |

## 8. Prompted behaviour

| Symptom | Likely cause | Fix |
|---|---|---|
| Prompt lines spoken verbatim, sometimes in the wrong language | Live copies quoted samples | Describe behaviour instead of quoting; label quoted English as illustration; give fixed values pre-translated per language |
| Agent says a value from a menu (caller said one size three times, agent repeated another from the menu) | A size/option menu in the prompt primed substitution | Ask open questions; read back in the caller's words; extractor records the caller's value |
| Two questions per reply; caller answers only the last | Pairing ("..., and your address?") | "ASK EXACTLY ONE QUESTION PER REPLY" with its reason and a guardrail naming the "Also, ..." pattern (3/12 -> 0/12) |
| Promises replacement/visit/reference with no details captured | Commitments ungated | One capitalised mandatory-details gate; otherwise a short close that promises nothing (2/15 -> 21/24) |
| "I have registered your complaint" with no tool | Model invents actions | Gate commitments in the prompt; make every side effect a real tool; "never say X is done unless the tool returned ok" |
| "The user hasn't ..." spoken aloud; caller hangs up | Model narrates notes | "Everything you say is heard; never say notes or reasoning aloud"; confirm the model id isn't an extended-thinking variant |
| Pushes a question-asker into a complaint flow | No call classification | Classify first (question / problem / request / ...); answer questions before any process |
| Expired product handled as a defect | Model doesn't know today's date | Today's date (business timezone) last in the prompt; day-first dates rule |
| Pronunciation hint leaks into speech (a phonetic spelling of the brand name is read out) | Hint text is copyable | Remove; Live pronounces brands well without it |
| Audio dropped after adding "think step by step / re-read" lines | Thinking scaffolds drop audio on 3.8 (migration guide) | No thinking scaffolds in the prompt |
| Impatient caller hangs up | Long serial questioning, defensive stock lines | Acknowledge briefly, say only a few questions are left, ask essentials together, move the rest to a follow-up channel (0/4 -> 9/9) |
| A promised time window comes out garbled ("twenty-four to eight hours") | Model translated the value live | Pre-translate fixed values per language |
| Cost/latency grows with every prompt edit | Prompt billed every turn; TTFA grows with size (6.6k tokens 927 ms, 65.8k 1,384 ms, 114.8k 1,695 ms) | Size budget test; rules once, positively, with reasons; incident evidence in a separate doc |

## 9. Transcription and recognition

| Symptom | Likely cause | Fix |
|---|---|---|
| Brand or product misspelled in transcripts | No recogniser bias | `input_audio_transcription.custom_vocabulary` with **phrases** for words a recogniser would genuinely mangle (up to 1,000 terms) |
| Common words pulled into brand words; a farewell turned into a brand name and cancelled a hang-up | Over-broad vocabulary (bare brand word, retailer name, "batch number") | Phrases only; remove bare words one phoneme from common words; one shared list across pipelines |
| "Best number" heard as "batch number" | Phone-line homophone, worse with boosting | Interpret answers against the question just asked; tell the model what that reply means |
| `<no speech>` / `{pause}` stored as turns | Live output transcription placeholders | Strip with `re.compile(r"<no speech>|\{pause\}", re.I)` before storage and extraction |
| Agent turns missing or cut in stored transcripts | Output transcription lossy and ~1.7 s late (2/12 empty, 1-2/12 truncated) | Never drive control from it; capture data through tool args; re-transcribe the recording for QA |
| No live captions / early intent | Input transcript arrives in one piece at end of turn; `interim_input_transcription` never arrived; no `finished` flag on 3.8 input transcripts | Design without interim text; don't wait for `finished` |
| Short leading interjections missing ("Hi,", "Haan ji,") | Known behaviour | Tolerate in extraction |
| English after a Hindi turn in Devanagari; romanised Hindi in ~10% of runs | Script choice varies | Post-call extraction and judges must accept mixed script |
| `language_code` / language field always empty or always "en" | Realtime transcription leaves it empty; a cascaded gateway defaulted it | Don't drive anything from a field you haven't seen populated on a real call |
| `language_codes` hints show no effect | Measured no visible effect | Optional; rely on the model |

## 10. Tools

| Symptom | Likely cause | Fix |
|---|---|---|
| Free-form `dict` parameter arrives as `{}` | Stripped to `{"type":"OBJECT"}` | Flat schemas: str/int/enum |
| Hang-up/transfer logic races the model | 3.8 treats tools as NON_BLOCKING by default (plugin 1.7.1 sends no `behavior`; 1.8.3+ sets `tool_behavior=NON_BLOCKING`) | Don't rely on blocking semantics; gate on playout and lifecycle state |
| Model keeps retrying a tool | Empty or uninformative tool responses | Return `{status, retryable, message}`; add a retry cap in the system instruction |
| Pronunciation/latency worsens with many tools | Large tool declarations | One-sentence descriptions; fewer tools per session |
| A "get reference number" tool fails with `MALFORMED_FUNCTION_CALL` | Unreliable on this model | Put deterministic values (reference number from session id) in the prompt |
| Transfer tool claims success when no number exists | Tool registered unconditionally | Register `transfer_to_human` only when a transfer target is configured; on failure tell the model the caller is still there |

## 11. SIP trunking and carriers

| Symptom | Likely cause | Fix |
|---|---|---|
| Caller hears **busy**; nothing reaches LiveKit | Provider trial lapsed / account not upgraded / KYC pending | Top up/upgrade; check the provider call log first whenever LiveKit shows nothing |
| Call never reaches LiveKit; provider shows failure | Origination URI missing `;transport=tcp` (Twilio sends UDP by default) or wrong host | `sip:<subdomain>.sip.livekit.cloud;transport=tcp` (or `;transport=tls`); `sips:` scheme unsupported on Twilio |
| India numbers fail to connect; Plivo hangup **4590** | Media-anchoring rule: India traffic must use India region pinning | `<subdomain>.india.sip.livekit.cloud`; outbound `destination_country: "in"`; India-region provider account. `references/12-india-telephony-compliance.md` |
| 404 / 503 from LiveKit | Inbound trunk `numbers` don't match the To/Request-URI format exactly | Match +E.164 exactly (or the provider's format); clean up trunk `numbers` (a community 503 was fixed this way) |
| 403 / 4001 | Digest credentials, IP ACL, or wrong edge IP:port | Re-check credentials both sides; use the exact edge IP:port the provider assigns |
| Twilio origination auth fails | Twilio Elastic SIP cannot send digest on origination | Use `numbers`/allowed numbers or IP allow-list on the LiveKit inbound trunk |
| Twilio outbound `400 Bad Request` | Number without `+` | E.164 with `+` |
| Provider only offers SIP REGISTER | LiveKit SIP does not support REGISTER | Put Asterisk/FreeSWITCH in between (registers upstream, peers with LiveKit) |
| Exotel trunk won't connect over UDP | Exotel supports TCP 5070 / TLS 443 only | Set `transport` explicitly on LiveKit trunks |
| Connects, but no audio or one-way audio | RTP ports blocked (e.g. UDP 10000-40000) / NAT | Open media ports from the provider's listed pools; on self-hosted SIP `use_external_ip: true` and open `rtp_port` range |
| Call drops after ~30 s (or 15 s after audio stops) | No RTP: LiveKit media timeout (30 s before first packet, 15 s after) / NAT | Fix media path; `media.media_timeout` only if justified |
| TLS trunk calls rejected | Media encryption mismatch (provider requires SRTP) | LiveKit `media_encryption` ALLOW/REQUIRE to match; "non-encrypted calls will be rejected" on Twilio secure trunking |
| G.722 never negotiated | Carrier is G.711-only (Twilio GA codecs PCMU/PCMA; Plivo Zentrunk G.711 only) | Expect narrowband; leave codec defaults; Opus over SIP unverified (VERIFY) |
| Rings, then silence / no bot | No agent in the room: dispatch rule `agentName` != code `agent_name`, rule points at a retired agent, worker down or cold | `lk sip dispatch list`, `lk agent status`, worker logs; keep any fallback rule pointed at a deployed agent |
| Caller number missing in the agent | `hidePhoneNumber: true` on the dispatch rule, or reading the wrong attribute | `hidePhoneNumber: false`; read `sip.phoneNumber` (then `sip.from`, `sip.fromUser`); never `sip.trunkPhoneNumber` (that's the number dialled) |
| Calls throttled at volume | Carrier per-trunk call-rate limits (Exotel default 20 calls/min) | Ask the carrier to raise limits before load tests |
| Transfer fails | REFER not enabled at the provider; wrong target format | Enable REFER (Twilio: Call Transfer + Enable PSTN Transfer); Plivo target `sip:+E164@<id>.zt.plivo.com`; Twilio blind transfers only |
| Indian domestic DID unavailable on Twilio | Twilio sells only +91 800 international toll-free in India, answered outside India | Use an Indian carrier (`references/12-india-telephony-compliance.md`) |
| Provider/LiveKit call can't be matched | Different call IDs per platform | Log `sip.callID`, `sip.callIDFull` and provider headers (map `X-...` headers with `headers_to_attributes`) |

## 12. WebSocket media streams

| Symptom | Likely cause | Fix |
|---|---|---|
| Chipmunk / slow / "low" voice | Sample-rate mismatch between wire and model (16 kHz in, 24 kHz out) | Resample correctly per direction; declare the right rate (`references/08-audio-formats-and-conversion.md`) |
| Loud static | mu-law bytes treated as PCM (or vice versa), wrong endianness (Telnyx L16 big-endian per RFC 3551, VERIFY), WAV header bytes in payload | Decode per provider profile; Twilio: payload must not contain file headers |
| Barge-in: agent keeps talking after the caller interrupts | Provider buffers queued audio (Twilio queues; Plivo ~60 s; Vonage ~60 s) | On Gemini `interrupted`: flush local queue and send provider `clear` (Twilio/Exotel `clear`, Plivo `clearAudio`); pace output with a 100-200 ms lead instead of bursting |
| Exotel: interrupted audio still plays briefly | Exotel `clear` drops only unplayed chunks; the playing chunk finishes | Small chunks (3200 B) and short lead |
| Exotel playback gaps | Chunks not multiples of 320 B or below the 3.2 KB minimum | 3200 B frames (safe under both readings of the docs) |
| Exotel hangs up ~4 s into Connect calls | Frames sent back-to-back (sample-code report, VERIFY) | Pace in real time |
| Exotel session fails at start / WS close 1006 | Bot didn't respond within 10 s; self-signed cert; LB idle timeout; egress IPs blocked | Valid public cert; fast handshake; raise LB idle timeouts; allow Exotel IPs |
| Twilio signature validation fails on the WSS handshake | Scheme/trailing-slash mismatch in the signed URL | Validate `wss://` and `https://` with and without trailing `/` (VERIFY which Twilio signs) |
| Twilio rejects stream URL with a query string | Query strings not allowed | Pass values with `<Parameter>`; read them from `start.customParameters` |
| Twilio TwiML after `<Connect><Stream>` never runs; recording missing | `<Connect>` blocks subsequent TwiML | Start `<Start><Recording>` before `<Connect>`; transfer via `<Connect action>` URL or REST update |
| No audio back to caller on Twilio | `<Start><Stream>` is one-way (fork) | Use `<Connect><Stream>` for bidirectional |
| Plivo: **every call in the account** hung up | `DELETE .../Call/` without `call_uuid` disconnects all calls | Always pass `call_uuid`; guard in code |
| Plivo stream closes 1008 | Invalid `X-Plivo-Signature-V3` | Validate with the right auth token and nonce |
| Bridge adds tens of ms per turn | Streaming soxr HQ resampler holds back 12-43 ms (up to 66 ms in a steady-tone test); proxies buffer WS frames; Nagle | Low-latency polyphase FIR (~1.5 ms) or flush soxr at turn end; `TCP_NODELAY`; no buffering proxies |
| Twilio media path slow from India | Twilio media processed in US1/IE1/AU1 only | Host the bridge next to the Twilio region; prefer an Indian carrier for Indian callers |
| WS bridge can't stop the Twilio stream without ending the call | Twilio: only ending or updating the call stops a bidirectional stream | REST update with new TwiML (transfer) or `Status=completed` (hang-up) |

## 13. Audio formats and quality

| Symptom | Likely cause | Fix |
|---|---|---|
| Harsh, aliased agent voice on the phone (4.5-8 dB SNR on 24k -> 8k; ratecv overall 4.5-15 dB) | `audioop.ratecv` or naive decimation 24 kHz -> 8 kHz | Proper low-pass resampler (stateful FIR 82-84 dB, soxr HQ 80-81 dB measured) - `references/08-audio-formats-and-conversion.md` |
| Clicks at chunk boundaries | Stateless per-chunk resampling | Keep resampler state across chunks |
| Agent voice sounds muffled vs demos | Narrowband G.711: Gemini's 24 kHz is band-limited to ~3.4 kHz | Expected; judge voices on narrowband renders (`voice_ab.py --narrowband`) |
| Recognition fine in tests, worse on calls | Test audio too clean (TTS callers) | Replay real recordings; simulate narrowband + noise (300-3400 Hz, mu-law round trip, -35 dB noise) |
| Choppy output | Bursting or irregular sends; queue underflow | Real-time pacer with 100-200 ms lead; `AudioSource(queue_size_ms=200)` on LiveKit |

## 14. Deployment and operations

| Symptom | Likely cause | Fix |
|---|---|---|
| Worker killed and respawned in a loop | Prewarm exceeded `initialize_process_timeout` (hard 10 s in 1.7.1) | Only local work in `setup_fnc`; network warm-up on a daemon thread; test worst case under budget |
| First call after idle answered slowly, `prerendered=false` | Cold worker; background render not finished | Warm replicas; publish caches only when complete; fallback path |
| A tuning typo refused every call | Realtime knobs validated as hard errors | Two-tier validation (`pipeline_errors` -> log + fallback, `cloud_errors` -> refuse); readiness check fails the deploy |
| Changed `.env.local`, nothing changed in production | Deployed workers read only platform secrets | `lk agent update-secrets`; re-validate end to end (values can't be read back) |
| CRM writes silently failing after a secret rotation | Wrong credential value | End-to-end marked test ticket after every rotation |
| Silence on a connected line at first Google call | Service-account key path wrong in the container | Discover the mounted key at prewarm; log the path; fall back to ADC |
| `lk agent rollback` errors | `--version` required in practice | Pass `--version <VERSION_ID>` (from `lk agent versions`) |
| Can't move the agent to another region | Region fixed at creation | Create a new agent in the new region, switch dispatch, delete the old |
| New version never takes traffic | Health check not passing within 5 min | Keep prewarm short; check `--log-type build` and runtime logs |
| Dashboards can't split pipelines/releases | Hard-coded `agent_version` | Record the pipeline that actually answered per call |
| Noise cancellation fails on self-hosted | LiveKit Cloud NC models (Krisp/BVC) authenticate against LiveKit Cloud only | Self-hosted: Krisp licence or ai-coustics with your own key (`references/05-livekit-self-hosted.md`) |

## 15. Post-call, CRM, analytics, recordings

| Symptom | Likely cause | Fix |
|---|---|---|
| CRM dropdowns empty on every voice ticket | Labels sent instead of option tags; CRM silently drops unknown values | Value maps from the live instance; enum of option names in the extraction schema; map to tags; unmatched values into the internal note; validate every tag at prewarm (`references/11-deployment-ops-and-integrations.md` §11) |
| Tickets lost after adding a field | Field added to the extractor but not the sink's allowlist (`Unknown semantic fields`; 6 of 8 lost) | One list; end-to-end test through every guard |
| Tickets never created in production, dry-run fine | Per-call OAuth + schema validation pushed submission past job teardown | One sink per process built at prewarm (thread-safe); hook builds on demand |
| Post-call hook killed at 300 s; call lost ticket, transcript, analytics | Extraction call with no timeout hung | Explicit HTTP timeout per attempt (60 s), bounded retries sized to the budget; isolate stages |
| 429s cost tickets under load | No retries on capacity errors | Retry 408/429/5xx with jittered backoff; never retry schema violations |
| Duplicate tickets after retries | No idempotency | External id per call; search-then-update; `Idempotency-Key` |
| Extracted booleans invented | Inference allowed | Only explicit statements; omit otherwise |
| Extraction failure loses the call | No fallback | File unclassified ticket from the transcript, flag `extraction_failed` |
| Every call reports zero cost | `SessionReport.to_dict()` strips the usage `type` discriminator | Read usage from the typed `session_report.model_usage` |
| Analytics events silently dropped | Substring PII backstop applied to allowlisted keys; call-site attributes missing from allowlist; emitter swallows errors | Allowlist first, backstop only for unknown keys; AST test of call sites |
| Long calls missing analytics rows | Outbox export limit 100; container deleted with the job | Export all rows at call end (limit 1000) |
| Realtime calls show zero caller turns | `on_user_turn_completed` never fires on realtime | Emit from final `user_input_transcribed` events |
| Recording never uploaded | `audio_recording_path` empty in the report; file dies with the container | Also try `ctx.session_directory / "audio.ogg"`; upload in the post-call hook |
| Recordings under the wrong day / `unknown-date/`; won't play in browser | UTC partitioning; ISO-only timestamp parsing; no content type | Business-timezone dates; parse epoch and ISO; `content_type="audio/ogg"`; skip zero-byte files |
| Caller number missing from tickets | With `delete_room_on_close` the SIP participant is gone before the hook | Stash in `proc.userdata[f"caller_number:{job_id}"]` during the call; pop on read |
| Reference number doesn't match any ticket | Spreadsheet stripped a leading zero | Never issue references starting with 0 |
| Extracted dates wrong (expired filed as defect) | No date context in the extractor | Today's date (business timezone) in the extraction prompt; day-first rule |
| Stored transcripts contain unredacted PII in broad datasets | Content routed through analytics | Caller content only in restricted datasets; validated, PII-free events elsewhere |

## 16. Testing and judging

| Symptom | Likely cause | Fix |
|---|---|---|
| Judge says romanised Hindi replies are English | LLM judge limitation | Explicit check wording ("romanised or native script; loanwords fine"); objective English-only metric; hand-check language verdicts |
| Every voice scores 9-10 | Un-anchored lenient rubric | Strict, contrast-anchored 1-5 rubric; blind ids |
| Prompt "fix" doesn't hold in production | Small n; text sims can't see audio problems | Wilson intervals / Fisher tests; replay real recordings; real calls (`references/10-testing-and-evaluation.md` §10) |
| Custom harness gets no reply after streaming a WAV | Stream stopped after speech, so no ACTIVITY_END | Keep sending silence after the audio |
| LiveKit simulation verdicts without transcripts; audio sim quota exhausted | Hosted simulator limits | Dev-only transcript dump in `on_session_end`; budget audio sims; use your own replay harness |
| Silence monitor fires in text simulations | Timers run in text simulation mode | Skip timers when `simulation_mode == SIMULATION_MODE_TEXT` |
