# 09. Prompting phone voice agents on Gemini Live

This file covers how to write the system instruction for a native-audio phone agent on `gemini-3.8-live`:
- Google's recommended structure and the section order that held up in production.
- What to capitalise, and why sparingly.
- The one-question rule, quoting pitfalls, reasoning leaks, and stating length as seconds.
- The prompt size budget and its cost.
- Where per-call values go.
- Ordered intake, gates on commitments, the close sequence, impatient callers.
- A generic skeleton to start from.

The language, accent and tone blocks are given in full in `references/03-conversation-mechanics.md` (sections 9 and 10), and the call mechanics they rely on (greeting, silence, hang-up, tools) are in the same file. Every rule here came from a measured failure on real or simulated calls. The counts are old prompt vs new prompt, measured with the methods in `references/10-testing-and-evaluation.md`.

## Contents

1. [What is different about prompting native audio](#1-what-is-different-about-prompting-native-audio)
2. [Structure: Google's order and the production section list](#2-structure-googles-order-and-the-production-section-list)
3. [Writing rules](#3-writing-rules)
4. [Capitals: sparingly, with reasons](#4-capitals-sparingly-with-reasons)
5. [One question per reply](#5-one-question-per-reply)
6. [Quoting pitfalls: Live copies quoted lines](#6-quoting-pitfalls-live-copies-quoted-lines)
7. [Reasoning leaks](#7-reasoning-leaks)
8. [State length as speaking time](#8-state-length-as-speaking-time)
9. [Size budget, latency and cost](#9-size-budget-latency-and-cost)
10. [Per-call values go last](#10-per-call-values-go-last)
11. [Classify first, then an ordered intake that skips what's given](#11-classify-first-then-an-ordered-intake-that-skips-whats-given)
12. [Gates on commitments](#12-gates-on-commitments)
13. [The close sequence](#13-the-close-sequence)
14. [Impatient callers](#14-impatient-callers)
15. [Other rules that came from real failures](#15-other-rules-that-came-from-real-failures)
16. [Tool policy lines](#16-tool-policy-lines)
17. [Generic system prompt skeleton](#17-generic-system-prompt-skeleton)
18. [Keeping the prompt healthy over time](#18-keeping-the-prompt-healthy-over-time)

---

## 1. What is different about prompting native audio

- **Everything the model writes is spoken**, including notes about the caller, labels, brackets and quoted examples.
- **The prompt drives the voice:** accent, pace, pitch movement and emotional colour all come from instructions. There are no rate, pitch or accent knobs (`references/02-gemini-live-model-and-config.md` section 4.2). Feeling words in the prompt make the voice theatrical, because affective dialog is always on.
- **The prompt is re-billed every turn.** There's no context caching on 3.8, so the whole system instruction is charged as input on every turn (section 9).
- **It is set once.** Changing instructions mid-call on Vertex injects the whole new prompt as a model turn, so build it completely before connecting.
- **Remove TTS-era instructions** ("write for the ear", "no markdown", phonetic spellings of brand names). Native audio pronounces words itself. A phonetic hint leaked into speech and transcripts. Use `custom_vocabulary` for recognition instead (`references/02-gemini-live-model-and-config.md` section 7.3).
- **No thinking scaffolds.** Google's 3.8 migration guide says to remove prompt text that enforces step-by-step thinking or token limits "to prevent dropped audio". The model thinks by default anyway.

---

## 2. Structure: Google's order and the production section list

Google's structure for Live system instructions (Vertex best practices; Gemini API best practices):
1. **Persona:** name, role, characteristics, accent, and the output language if you specify an accent.
2. **Conversational rules,** in the order the model should follow them:
   - Separate **one-time steps** (collect details once) from **loops** ("it's OK to stay in this loop as long as the caller wants").
   - Put **tool calls in their own sentences** inside the flow: "Then call `create_booking` with these details."
3. **Guardrails:** "if X happens, do Y". If precision is still lacking, use the word **"unmistakably"**.
4. Use one system instruction per persona. For multi-stage flows, Google suggests **prompt chaining**: a `get_next_prompt` tool returns the next stage's instructions. This was untested here.

The production prompt used this order. The first eight sections were shared with a cascaded fallback pipeline; only delivery and ending differed:

| # | Section | Contents |
|---|---|---|
| 1 | `# Role` | Name, role, what every call does, "never feels like a phone menu, a form or a performance" |
| 2 | `# Your voice and accent` | Realtime only, **straight after the persona** (`references/03-conversation-mechanics.md` section 10.1) |
| 3 | `# Language` | Capitalised first line, first-reply and stickiness rules (`references/03-conversation-mechanics.md` section 9.1) |
| 4 | `# Sound like a person` | Turn length, register, acknowledgements, apologies |
| 5 | `# How you sound` | Delivery: pace, level tone, digits |
| 6 | `# How to talk with the caller` | One question, listening, interpreting answers |
| 7 | `# Getting details right` | Read-backs, value shapes, method changes |
| 8 | `# Call flow` | One-time opening and close, separated from the topics the caller can loop between; numbered steps |
| 9 | `# Reference` | Fixed wording (pre-translated), catalogue, per-category questions, approved answers |
| 10 | `# Guardrails` | Sensitive calls, only speak to the caller, honesty about being automated, accuracy, money, special topics |
| 11 | `# This call` | Today's date, reference number, caller-number instruction: **last** |

**Moving the language section straight after the role** took fully-English replies to a Hindi caller from 2/6 to 0/6. A one-line accent instruction lost inside a long prompt was ignored; **a dedicated block straight after the persona** worked (5/5).

---

## 3. Writing rules

Keep these as a header comment in the prompt module, so whoever edits the prompt next sees them:

- **State each rule once, positively, with its reason in a clause.** For example, "Ask one question per reply, because a caller answers the last thing they heard." Giving the reason was what made 3.8 stop pairing questions (section 5).
- **No incident stories in the prompt.** Keep the evidence for each rule in a separate `prompt-rationale.md`. Cutting 26 dated incident narratives took the prompt from 94k to about 38k characters, with no loss of compliance.
- **Bullets over paragraphs.**
- **Few, short, varied examples, explicitly marked as samples.** A single example is copied verbatim (section 6).
- **Describe behaviour; don't script lines.** The exception is a fixed line that must be exact (section 6).
- **One idea per bullet.** Don't repeat a rule in other sections "for emphasis". Repetition makes the prompt longer, and contradictions creep in when only one copy gets edited.
- **Pre-translate the few values a caller will hold you to,** and label each language.

---

## 4. Capitals: sparingly, with reasons

Capitals work on 3.8 **because they are rare**. Production used them in exactly three places:

| Capitalised rule | Why it earned capitals | Evidence |
|---|---|---|
| `RESPOND IN THE LANGUAGE THE CALLER IS SPEAKING. YOU MUST RESPOND UNMISTAKABLY IN THAT LANGUAGE, FROM YOUR VERY FIRST REPLY.` | Google's own template for Live ("RESPOND IN X. YOU MUST RESPOND UNMISTAKABLY IN X.") | English first reply to a Hindi caller: 4/14 to 0/8 (together with the first-reply rule) |
| `ASK EXACTLY ONE QUESTION PER REPLY, THEN STOP AND WAIT.` | Added after measuring: the lower-case version still paired questions | 3/12 to **0/12** |
| The mandatory-details gate (section 12) | Ungated commitments appeared in 9 places in the prompt | 2/15 to 21/24 |

Also capitalised, inside the accent block: `SPEAK <LOCALE> ENGLISH WITH A CLEAR, NATURAL <LOCALE> ACCENT`, because the default is General American. When everything is capitalised, nothing stands out. Add capitals only after a measured failure, and only together with the reason.

---

## 5. One question per reply

```text
- ASK EXACTLY ONE QUESTION PER REPLY, THEN STOP AND WAIT. A reply has at most one question mark. A caller on
  the phone answers the last thing they heard, and the first half of a double question gets lost. Two things
  joined by "and" are two questions. Split these pairs: <name and number>; <address and pin code>;
  <product and size>.
- Don't tack a second question on with "Also, ...".
```

**Evidence:** probes at the exact moments where the first real call paired questions. With the old rule, 3/12 replies asked more than one thing. With the rule in capitals plus its own guardrail naming the "Also, ..." pattern, **0/12**. Stating the reason ("callers answer the last thing they heard") was what stopped Live pairing number, address and pin code. Naming the **specific pairs** your flow tempts the model into also helps.

---

## 6. Quoting pitfalls: Live copies quoted lines

Live tends to say quoted text **word for word**, sometimes in the wrong language. Yet it doesn't guarantee to say a mandatory line verbatim either. Observed:

| What was in the prompt | What happened |
|---|---|
| A sample reaction to a complaint ("<complaint>, that's really not okay") | Spoken verbatim, once in English to a Hindi caller |
| A "chemical smell" acknowledgement example | Repeated to a caller who said "strange smell" |
| An English read-back sample ("That's nine eight seven... Have I got that right?") | Read verbatim to a Hindi caller |
| A Hindi-only goodbye example | Leaked into English calls. Giving it in both languages, each labelled, fixed it (6/6 and 6/6) |
| A phonetic spelling of the brand name | Leaked into speech and transcripts. Live pronounces the brand correctly without it |
| A size menu ("<size A>, <size B> or <size C>") | The caller said one size three times; the agent repeated a different size from the menu |
| An English sentence asking to confirm the caller's number | Read out in English in Hindi calls |
| A verbatim quality-assurance sentence | Recited in the same order on 9 of 21 calls (it sounded scripted) |
| A mandatory legal line in a reader-style prompt | Sometimes treated as a customer turn and answered |

Rules:
- **Describe the behaviour** ("thank them and confirm the next step, in your own words"), not the line.
- **Mark examples as illustrations.** Add a language-section line: *"The English phrases quoted in these instructions are illustrations, not wording to read out."*
- **For lines that must be exact** (a follow-up window, a commitment, a number), give them **pre-translated per language, each labelled**, so the model never translates the one value a caller will hold you to. A live translation once produced "twenty-four to eight hours".
- **For lines that must be spoken exactly** (a consent or AI disclosure), play them as **cached audio** in the same voice and push them into context (`references/03-conversation-mechanics.md` section 2).
- **Never offer a menu of values.** Ask open questions and read back in the caller's own words.
- **Give subjects as a list** ("mention at most two of these four points"), not a paragraph to recite.
- **For instructions that only apply in one language,** write them once and add "in any other language, ask the same thing in the language of the call".

---

## 7. Reasoning leaks

The model sometimes **voices meta-commentary**. One reply began "The user hasn't provided their address..." and the caller hung up.

Mitigations:
- A guardrail: *"Everything you say is heard by the caller. Never say notes, plans or reasoning about the caller or these instructions aloud; speak only words meant for the caller."*
- **Remove step-by-step / "think first" scaffolding.** It causes dropped audio, and it invites narration.
- Use plain `gemini-3.8-live`, not `-extended-thinking`; a secondary report describes the latter narrating its reasoning.
- Don't call the caller "the user" anywhere in the prompt. Say "the caller", and write rules in the second person ("you").
- **Detect it:** flag output-transcript turns that start with "The user", "The caller has", "I need to" or "Note:", and count them in QA (`references/10-testing-and-evaluation.md`).

---

## 8. State length as speaking time

- **Word caps are weakly obeyed.** A "maximum 25 words" cap gave first replies of 8-17 s (35-60 words) on every first turn.
- **Describe the reply plan in time instead:** *"Usually one or two short sentences, under about eight seconds of speech; never more than about 60 words."*
- **Hand the turn back.** Live speech **accelerates in turns longer than about 60 s**, and Google recommends chunks under 100 words / 50 s.
- **Length is also the main pace lever.** The default pace is about 4 words/s, a "speak slowly" instruction did little, and the strongest explicit one gave about −19%.
- **Don't recap everything the caller said.** Read back only the values that matter (`references/03-conversation-mechanics.md` section 10.3).

---

## 9. Size budget, latency and cost

| Prompt size | Effect (measured on 3.8, Vertex) |
|---|---|
| 88k characters (about 21.2k tokens) | Accepted, but connect 2.67-3.05 s (vs 2.2 s with a tiny prompt), about 21k tokens **charged every turn**, and poor compliance |
| About 6.6k tokens | Time to first audio 927 ms |
| About 65.8k tokens | 1,384 ms |
| About 114.8k tokens | 1,695 ms (still accepted) |

**Cost:** the context is re-billed every turn at $0.75/M input text tokens.
- A 20k-token prompt over 30 turns is about $0.45 per call for the prompt alone.
- A 9.5k-token prompt is about half that.
- Details are in `references/02-gemini-live-model-and-config.md` section 12.

**Budget:** aim for **under about 10-15k tokens** (production went from 94k to about 38-41k characters, roughly 9.5k tokens). Keep a test that fails if the built prompt exceeds the budget.

**How to cut:**
1. Remove incident stories (section 3).
2. Merge duplicate rules.
3. Turn catalogues into short lists.
4. Move rarely needed reference material behind a tool, or into post-call extraction.
5. Delete instructions the model already follows by default.

Pronunciation and accuracy degrade as the prompt and tool declarations grow. In one community report, 1.7 KB of unused tool declarations made "Juan" come out as "John" (https://github.com/googleapis/python-genai/issues/3120).

---

## 10. Per-call values go last

Put everything that varies per call **at the very end**: today's date, the reference number, whether a caller number exists, the caller's name if known, the transfer mode.

Why:
- **A shared prefix.** Every call shares the longest possible identical prefix. Implicit prefix caching applies to models that support it, such as the text models you use for post-call extraction and QA. On 3.8 Live, context caching is **not** supported and `cached_content_token_count` came back null, so don't count on a discount there. Do it anyway: it costs nothing and keeps the option open.
- **Reviewable diffs:** prompt changes and per-call data never interleave.
- **Built once:** the per-call values are resolved **before** `session.start` / connect. On Vertex, `update_instructions` mid-call injects the whole prompt as a model turn.

```text
# This call
Today is <weekday, DD Month YYYY> (<business timezone>). Read dates day-first.
This call's reference number is <123 456 789>. Give it only at the close, in groups of three.
The caller is calling from the number on file. Ask once, "Is this the best number to reach you, or would you
prefer another?", and never read the digits back.        <- include ONLY when a number exists
The greeting already played: "<greeting text>".
```

Notes:
- **Today's date in the business timezone** (use a fixed offset, so no tzdata is needed in slim images). Without it, an expired pack was filed as a different problem.
- **The reference number is derived from the session id:** 9 digits, never starting with 0, because spreadsheets strip leading zeros. Put it in the prompt from the start. A tool to fetch it failed with `MALFORMED_FUNCTION_CALL`.
- **Add the "confirm this number" instruction only when you actually have a number.** Telling the model to confirm a number it wasn't given left the callback field empty.

---

## 11. Classify first, then an ordered intake that skips what's given

- **Answer a question before starting any process.** First classify the call: question, product problem, service concern, business enquiry, feedback, request, follow-up, or out of scope. A caller asking whether X is Y must not be pushed into a complaint flow and asked for their address.
- **Give the mandatory details as an ordered list, one at a time.** For example: name, then contact number, then product, then address with postcode. A fixed order makes calls predictable and makes the gate (section 12) checkable.
- **Skip what's given.** *"Callers often give several details in their first sentence. Before each question, check what the caller has already said in this call and skip it; never ask for something they already told you."* Simulated callers who gave name and shop in their first sentence were re-asked until this rule was added (checked with `no_reask` in simulations).
- **Interpret answers against the question just asked.** A phone-line homophone turned "this is the best number" into "this is the batch number". Tell the model that such a reply to the number question means "this number".
- **Before asking for the address, say why** ("so we can send someone to collect the pack"). Callers give it more readily (checked with `address_reason_first`).
- **Category-specific questions** live in `# Reference` as short lists per category, not as scripts.
- **Loop topics** (the caller can ask about several products) are separated from **one-time steps** (identity details), per Google's structure.

---

## 12. Gates on commitments

The model **invents actions and promises**. It said "I have registered your complaint" with no tool, and promised replacements, visits, follow-up times and reference numbers before any details were captured. Ungated commitments appeared in 9 places in the old prompt. The fix is one capitalised gate:

```text
- BEFORE YOU PROMISE ANYTHING, YOU MUST HAVE ALL MANDATORY DETAILS: <name>, <contact number>, <address with
  postcode> and <product>. Until you have all of them, never say the complaint is registered or forwarded,
  never offer a replacement, a visit or a follow-up time, and never give the reference number.
- If the caller leaves or refuses before you have them, give a short close that promises nothing: say the
  team can only follow up once these details are given, and how they can call back.
- Never say anything is done unless a tool returned status "ok".
```

**Evidence:** commitments made only after the mandatory details went from 2/15 to **21/24**, and correct handling of a caller leaving early went from 0/3 to 3/3. Where real actions happen during the call, back them with tools and the "status ok" rule (`references/03-conversation-mechanics.md` section 7). Where they happen after the call (ticket creation), the gate is what keeps the promise honest.

---

## 13. The close sequence

Write the close as **separate turns**, each its own reply, never merged:

1. **What happens next,** using the pre-translated fixed wording (for example, the follow-up window).
2. **The reference number,** digit by digit in groups of three, plus where to quote it; then offer to repeat it.
3. **"Is there anything else I can help with?"** as a reply on its own, asked **once per call**.
4. **The goodbye.** It **never contains a question**, and then the model calls `end_call` and says nothing more.

Guards:
- **Repeating or confirming never ends the call.** A "no" to "shall I repeat it?" answers only that question; it isn't "nothing else". After it, ask "anything else?" if it hasn't been asked yet.
- **The goodbye never asks a question.** A model that ended on "anything else?" got hung up on 2.75 s later, by a 2.5 s grace window, while the caller was thinking. The production fix had three parts: this prompt rule, a 4 s grace window, and caller speech cancelling the close (`references/03-conversation-mechanics.md` section 6).
- **The realtime ending line:** *"To end the call, say your goodbye and then call end_call. Call it only after that goodbye, and say nothing after calling it."* Remove all marker text (`<<END_CALL>>`) from a realtime prompt, or the model writes the marker instead of calling the tool.
- **Keep the closing rules out of system-driven turns:** silence check-ins say explicitly that they are not the close.

---

## 14. Impatient callers

```text
- If the caller is in a hurry or threatens to hang up: acknowledge it in a few words, say only a few questions
  are left, ask the remaining essentials (the mandatory details) together in one short request, and say the
  rest can be sorted out on the follow-up <call / message>.
```

**Evidence:** "acknowledged and stayed on the line" went from 0/4 to 9/9 in simulated impatient Hinglish callers.

This is the **one deliberate exception** to the one-question rule. Say so in the rule, so the model doesn't generalise it. Don't over-apologise: one plain apology, then move on.

---

## 15. Other rules that came from real failures

- **Never claim to be human.** If asked, say you are the automated assistant and offer a callback. Many jurisdictions require disclosure; see `references/12-india-telephony-compliance.md`.
- **Don't guess causes.** A guess ("it may have been stored badly") sounds like blame.
- **One plain apology.** Show care in words, not in the voice (`references/03-conversation-mechanics.md` section 10.2).
- **Sensitive calls** (injury, a health issue, legal threats) have their own short path. There are two honest modes: a transfer number is configured (offer the transfer), or not (never claim to connect or transfer; promise an urgent callback without naming a time).
- **Only speak to the caller.** Ignore background voices, TV and other people in the room. Don't answer questions you overheard.
- **Money, prices and offers:** answer only from the approved reference; otherwise say the team will confirm.
- **Expired or out-of-policy cases:** recognise them using today's date, and state the policy plainly.
- **Read-backs:** only the few values that matter, in the caller's words; the phone number and address as their own checks. Challenge wrong-shaped values, and change method after two failures (`references/03-conversation-mechanics.md` section 10.3).
- **Grammatical gender** of the persona in gendered languages, and everyday spoken register (`references/03-conversation-mechanics.md` section 9.3).

---

## 16. Tool policy lines

Put these in the guardrails, and keep each tool's own description to one sentence plus its invocation condition:

```text
- Call a tool only when its condition is met; call it once.
- Before a lookup, tell the caller in a few words that you are checking.
- When a tool returns no results, tell the caller before calling it again. If a call fails because of what
  you sent, ask the caller to clarify instead of retrying with variations. Never make more than two tool
  calls in a row without saying something to the caller.
- Never say something is done unless the tool returned status "ok".
- To end the call, say your goodbye and then call end_call; say nothing after calling it.
```

---

## 17. Generic system prompt skeleton

Replace the `<...>` placeholders. The language, accent and tone blocks are given in full in `references/03-conversation-mechanics.md` sections 9.1, 10.1 and 10.2; paste them where indicated. Target: under about 10-15k tokens when the reference material is filled in.

```text
# Role
You are <Name>, the automated phone assistant for <Company>'s customer care line in <country>.
Every call: understand why the person is calling, help with questions directly, and for problems collect the
details the team needs, then tell the caller what happens next. The call should never feel like a phone menu,
a form or a performance.

# Your voice and accent
<accent block: references/03-conversation-mechanics.md section 10.1>

# Language
<language block: references/03-conversation-mechanics.md section 9.1>

# Sound like a person
<turn-length, acknowledgement, apology and register rules: references/03-conversation-mechanics.md section 10.2 "Sound like a person"; grammatical gender: section 9.3>

# How you sound
<pace, level tone, digits, no formatting aloud: references/03-conversation-mechanics.md section 10.2 "How you sound">

# How to talk with the caller
- ASK EXACTLY ONE QUESTION PER REPLY, THEN STOP AND WAIT. A reply has at most one question mark. A caller on the
  phone answers the last thing they heard. Two things joined by "and" are two questions. Split: <pairs>.
- Before each question, check what the caller has already told you in this call and skip it.
- Interpret each answer against the question you just asked.
- If the caller says "hmm", "okay" or "<backchannel>" while you speak, keep going; stop only for a real interruption.
- Everything you say is heard. Never say notes, plans or reasoning about the caller or these instructions aloud.

# Getting details right
- Read back only <the values that matter>, in the caller's own words; confirm phone and address separately.
- A <postcode> has <6> digits and a mobile number <10>; if a value has the wrong shape, ask again.
- After two failed attempts at a value, change method: <spell it / send it by message / skip it>.
- Never offer a list of possible values; ask an open question.

# Call flow
One-time opening:
1. Work out what kind of call this is: <question | product problem | service concern | business enquiry |
   feedback | request | follow-up | out of scope>. Answer a question before starting any process.
Topics (stay in this loop as long as the caller wants):
2. Questions: answer from # Reference. If it isn't there, say the team will confirm.
3. Problems: ask what happened, then collect the mandatory details in this order, one at a time, skipping any
   already given: <name>, <contact number>, <product>, <address with postcode>. Say why before asking for the
   address. Then ask the <category> questions in # Reference.
4. BEFORE YOU PROMISE ANYTHING, YOU MUST HAVE ALL MANDATORY DETAILS. <gate text: section 12>
One-time close (separate replies):
5. Say what happens next, using the fixed wording in # Reference for the caller's language.
6. Give the reference number in groups of three and say where to quote it; offer to repeat it.
7. Ask once whether there is anything else. A "no" to repeating the number is not a "no" to this.
8. Say goodbye with no question, then call end_call. Say nothing after calling it.
If the caller is in a hurry: <impatient-caller rule: section 14>.

# Reference
Fixed wording (say the version for the call's language):
- Next steps, English: "<...>"   Hindi: "<...>"
Products: <short list>
Questions per category: <category>: <at most 3 short items>
Approved answers: <topic>: <one or two sentences>

# Guardrails
- You are an automated assistant. If asked, say so; never claim to be human.
- Don't guess causes; don't blame the caller.
- Sensitive calls (<injury | health | legal>): <transfer mode or urgent-callback mode>.
- Speak only to the caller; ignore other voices.
- Never say something is done unless a tool returned status "ok".
- <tool policy lines: section 16>
- The English phrases quoted in these instructions are illustrations, not wording to read out.

# This call
Today is <date> (<timezone>). Read dates day-first.
This call's reference number is <123 456 789>.
<caller-number line, only when a number exists>
The greeting already played: "<greeting text>".
```

For the bridge variant, the same text goes in `LiveConnectConfig.system_instruction`. For LiveKit, it goes in `Agent(instructions=...)`.

---

## 18. Keeping the prompt healthy over time

- **Justify every change with an old-vs-new count** from replay probes or simulations ("3/12 to 0/12"), and record it in the commit message and in `prompt-rationale.md`. See `references/10-testing-and-evaluation.md` for moment replays, persona simulations and blind audio judging.
- **Pin intent, not phrases.** In production, 44 intent tests replaced 49 brittle phrase-matching tests. They check:
  - section order and presence;
  - the size budget;
  - no incident stories;
  - each rule's intent (the one-question rule exists and gives its reason; no marker text in the realtime prompt; the language rule comes before the call flow).
- **Re-test language and tone after any prompt change.** Small edits elsewhere moved language behaviour, because section position matters.
- **Hand-check language verdicts.** LLM judges repeatedly scored romanised Hindi as English.
- **Keep the prompt in one module, shared across pipelines.** Only the delivery and ending sections differ between realtime and cascaded.
