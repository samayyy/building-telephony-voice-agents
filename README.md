# Building telephony voice agents (Agent Skill)

An [Agent Skill](https://agentskills.io) that teaches your coding agent how to build, connect and tune **phone-call
voice agents** on **Google Gemini Live** (`gemini-3.8-live`, native audio). It covers two runtimes:

- **LiveKit**: the managed Cloud service or the self-hosted open-source server;
- **a direct WebSocket bridge** to Gemini Live.

Calls reach the agent over **SIP trunking** or a provider's **WebSocket media stream**.

It was written from a production deployment: Gemini Live on LiveKit Cloud behind an Indian SIP trunk, with Hindi,
English and Kannada callers. Every provider and API detail was checked against current docs, and the audio
conversion numbers were measured. Where a source couldn't be confirmed, the text says **VERIFY**.

> Works with Claude Code, Claude apps (claude.ai / Desktop), the Claude API and Agent SDK, OpenAI Codex, Cursor,
> Google Antigravity, Gemini CLI, GitHub Copilot / VS Code, Windsurf, OpenCode, Cline, Roo Code, Goose, Amp and Kiro.
> These are any agents that read the `SKILL.md` format.

---

## What it covers

| Area | Highlights |
|---|---|
| **Choosing an architecture** | A decision tree for SIP or a WebSocket stream, and for LiveKit Cloud, self-hosted LiveKit, a direct bridge or Pipecat, with latency and cost trade-offs |
| **Gemini Live config** | <ul><li>Regions, voices (with blind-test results), language codes, transcription, server-VAD settings, context compression, session resumption and GoAway, pricing</li><li>**The settings that silently break a call**: thinking off means no audio; `affective_dialog` is rejected with 1007; a small `max_output_tokens` means turns never finish</li></ul> |
| **Conversation mechanics** | <ul><li>Greeting rendered ahead of time; holding and replaying caller audio during the greeting to stop greeting-echo replies and allow barge-in</li><li>Interruptions; the silence ladder (check-in, warning, close); a guarded hang-up</li><li>Tool calling, sync or async, blocking or non-blocking, with fillers; VAD and turn-taking; multilingual callers; voice and tone</li><li>**Background, thinking and hold music**; transfer to a human; DTMF</li></ul> |
| **LiveKit Cloud** | Agent deploys, SIP trunks and dispatch rules, recording with egress, the Twilio Connector, India region pinning, versions to pin and versions with known bugs |
| **LiveKit self-hosted** | `livekit-server`, `livekit/sip` and `egress` with docker-compose, ports, TLS, and Redis |
| **SIP trunking** | <ul><li>Step-by-step for Plivo, Twilio Elastic SIP, Exotel vSIP, Telnyx, Vonage and Indian enterprise trunks</li><li>An Asterisk or FreeSWITCH bridge for carriers that only support SIP REGISTER</li><li>Outbound calls, transfer, DTMF</li></ul> |
| **WebSocket media streams** | <ul><li>The full wire protocol for Twilio Media Streams, Exotel Voicebot / Connect Voice AI, Plivo Audio Streams, Telnyx and Vonage, plus how to map a carrier protocol documented only under NDA</li><li>A side-by-side table and one adapter interface for all providers</li></ul> |
| **Audio** | μ-law / A-law / L16 at 8, 16 and 24 kHz; resampling (measured: a stateful FIR gives 82–84 dB SNR, while `audioop.ratecv` gives 4.5–15 dB); frame sizes, real-time pacing, barge-in timing |
| **Prompting** | Persona, then rules, then guardrails; language taken from the caller's first words; one question per turn; commitments gated on required details; a calm tone that doesn't sound dramatic |
| **Testing** | Simulated callers with an LLM judge, replay of real recordings, blind voice A/B tests, reply-gap measurement |
| **Ops** | Deploy and rollback, warm workers, health probes, per-call events, recordings, post-call extraction to a CRM (including the Zendesk dropdown trap) |
| **India** | Media anchoring, number KYC, the TRAI/DoT rules for AI calls, recording consent, the DPDP Act |
| **Troubleshooting** | A table of symptom, cause and fix, built from real incidents |

### Repository layout

```
.claude-plugin/                 marketplace.json + plugin.json (Claude Code / claude.ai plugin install)
skills/building-telephony-voice-agents/
├── SKILL.md                    entry point: decision tree, build checklist, "never do" table, defaults
├── references/                 13 deep-dive files (01-architecture … 13-troubleshooting)
├── templates/
│   ├── livekit/                production-style LiveKit + Gemini Live agent, Dockerfile, livekit.toml, env
│   ├── sip/                    inbound/outbound trunk + dispatch-rule JSON, outbound-call script
│   ├── selfhost/               docker-compose for livekit-server + sip + egress (+ configs)
│   ├── ws_bridge/              FastAPI bridge (Twilio, Exotel, Plivo adapters) + tested audio module
│   └── testing/                simulated callers, recording replay, voice A/B, reply-gap measurement
└── scripts/audio_selftest.py   offline audio-conversion test suite (prints RESULT: PASS)
```

---

## Install

The skill is the folder `skills/building-telephony-voice-agents/`. Each platform just needs that folder in its skills
directory. Pick your platform below.

### Any agent, in one command (recommended)

The [`skills` CLI](https://github.com/vercel-labs/skills) ([skills.sh](https://skills.sh)) installs the skill into
the right folder for about 80 agents:

```bash
# interactive: pick agents and scope
npx skills add samayyy/building-telephony-voice-agents

# non-interactive: global install for specific agents
npx skills add samayyy/building-telephony-voice-agents -g -y \
  -a claude-code -a codex -a cursor -a antigravity -a github-copilot
```

Useful flags:
- `-g` installs for your user rather than the current project.
- `-a <agent>` targets one agent and can be repeated.
- `--copy` copies the files instead of symlinking them.

`npx skills update` pulls new versions. Its built-in paths for a few agents (Antigravity, Goose, Windsurf) can lag
behind those vendors' docs. If a skill doesn't show up, use the manual path from the sections below.

### Claude Code

**Option A: plugin marketplace.** This lets you update with `/plugin marketplace update`.

```
/plugin marketplace add samayyy/building-telephony-voice-agents
/plugin install building-telephony-voice-agents@telephony-voice-agents
```

From a shell, run the same two steps as commands:
```bash
claude plugin marketplace add samayyy/building-telephony-voice-agents
claude plugin install building-telephony-voice-agents@telephony-voice-agents
```

**Option B: copy the folder.**

```bash
git clone https://github.com/samayyy/building-telephony-voice-agents.git
# personal (all projects)
cp -R building-telephony-voice-agents/skills/building-telephony-voice-agents ~/.claude/skills/
# or project-only (commit it so your team gets it)
mkdir -p .claude/skills && cp -R building-telephony-voice-agents/skills/building-telephony-voice-agents .claude/skills/
```

**Use it.** Claude loads the skill on its own when you ask about phone or voice agents. You can also call it
directly:
- after a folder install: `/building-telephony-voice-agents`;
- after a plugin install: `/building-telephony-voice-agents:building-telephony-voice-agents`.

If you created the skills folder mid-session, run `/reload-skills` (or `/reload-plugins` after a plugin install).

### Claude apps (claude.ai web and Claude Desktop)

1. Turn on **Settings → Capabilities → Code execution and file creation**. On Team or Enterprise plans, an owner
   must allow skills under **Organization settings**.
2. Download `building-telephony-voice-agents.zip` from the
   [latest release](https://github.com/samayyy/building-telephony-voice-agents/releases/latest), or build it:
   ```bash
   cd skills && zip -r ../building-telephony-voice-agents.zip building-telephony-voice-agents
   ```
   The zip must contain the folder at its top level (`building-telephony-voice-agents/SKILL.md`), not a bare
   `SKILL.md`.
3. Go to **Customize → Skills → + → Upload a skill** and choose the zip.

Alternatively, go to **Customize → Plugins → Add → Add marketplace** and enter
`samayyy/building-telephony-voice-agents`.

To use it, ask about phone or voice agents in a chat, or type `/` and pick the skill.

> If an upload is rejected for its description length, note that an older help article mentions a 200-character
> limit. Shorten the `description` in `SKILL.md` and zip it again. The current docs say 1,024 characters; this
> skill's description is about 920.

### Claude API

Upload once. The command needs every file in the folder, so it's easiest to script:

```bash
cd skills
args=(); while IFS= read -r f; do args+=(-F "files[]=@$f;filename=$f"); done \
  < <(find building-telephony-voice-agents -type f ! -name '.DS_Store')
curl https://api.anthropic.com/v1/skills \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -F "display_title=Building telephony voice agents" "${args[@]}"
# -> {"id": "skill_...", "latest_version_id": "skver_...", ...}
```

Then attach it to a Messages request alongside the code-execution tool:

```json
{
  "model": "claude-opus-5-5",
  "max_tokens": 4096,
  "container": { "skills": [{ "type": "custom", "skill_id": "skill_...", "version": "latest" }] },
  "tools": [{ "type": "code_execution_20250825", "name": "code_execution" }],
  "messages": [{ "role": "user", "content": "Design a Twilio Media Streams to Gemini Live bridge for 8 kHz μ-law." }]
}
```

The API sandbox has **no network access**, so the skill works there as knowledge and code generation. It can't
reach LiveKit, Twilio or Gemini.

### Claude Agent SDK

The SDK loads skills from `~/.claude/skills/` and `.claude/skills/` (copy the folder there, as in Option B above). It
also needs these options:

```python
options = ClaudeAgentOptions(setting_sources=["user", "project"], allowed_tools=["Skill", "Read", "Bash", ...])
```
```ts
const options = { settingSources: ["user", "project"], allowedTools: ["Skill", "Read", "Bash"] };
```

### OpenAI Codex (CLI, IDE extension, app)

```bash
git clone https://github.com/samayyy/building-telephony-voice-agents.git
mkdir -p ~/.agents/skills && cp -R building-telephony-voice-agents/skills/building-telephony-voice-agents ~/.agents/skills/
# project-only instead: .agents/skills/ in your repo
```

Codex's built-in installer works too. Inside Codex, run:
```
$skill-installer install https://github.com/samayyy/building-telephony-voice-agents/tree/main/skills/building-telephony-voice-agents
```

Restart Codex afterwards. To use the skill, type `$building-telephony-voice-agents`, pick it from `/skills`, or let
Codex choose it from the description.

### Cursor

Copy the folder to `~/.cursor/skills/` (personal), or to `.cursor/skills/` or `.agents/skills/` in the project:

```bash
mkdir -p ~/.cursor/skills && cp -R building-telephony-voice-agents/skills/building-telephony-voice-agents ~/.cursor/skills/
```

Cursor also reads `~/.claude/skills/`, so a Claude Code install already works here. To use it, Cursor picks it up
on its own, or type `/` in Agent chat and choose `building-telephony-voice-agents`.

### Google Antigravity

| Scope | Path |
|---|---|
| Workspace (app, IDE, CLI) | `<workspace>/.agents/skills/building-telephony-voice-agents/` |
| Global (app, IDE) | `~/.gemini/config/skills/building-telephony-voice-agents/` (the IDE also reads the older `~/.gemini/antigravity/skills/`) |
| Global (Antigravity CLI `agy`) | `~/.gemini/antigravity-cli/skills/building-telephony-voice-agents/` |

```bash
mkdir -p ~/.gemini/config/skills && cp -R building-telephony-voice-agents/skills/building-telephony-voice-agents ~/.gemini/config/skills/
```

To use it, Antigravity picks it up on its own, or type `/building-telephony-voice-agents`.

### Gemini CLI

Google is moving consumer users from Gemini CLI to Antigravity CLI (see above). Enterprise and API-key users can
still run:

```bash
gemini skills install https://github.com/samayyy/building-telephony-voice-agents \
  --path skills/building-telephony-voice-agents --scope user
```

Or copy the folder to `~/.gemini/skills/` or `.gemini/skills/`. Check it with `/skills list`; the model activates it
when relevant, after you approve.

### GitHub Copilot (VS Code, Copilot CLI, coding agent)

Copy the folder to `.github/skills/` (project) or `~/.copilot/skills/` (personal). Copilot also reads
`.claude/skills/` and `.agents/skills/`, and no setting is needed. To use it, type `/building-telephony-voice-agents`
in chat, or Copilot loads it on its own.

### Other agents

| Agent | Project folder | Personal folder | How it's used |
|---|---|---|---|
| Windsurf / Devin Desktop | `.devin/skills/` (older `.windsurf/skills/`) or `.agents/skills/` | `~/.codeium/windsurf/skills/` | Picked up on its own, or `@building-telephony-voice-agents` |
| OpenCode | `.opencode/skills/` or `.agents/skills/` | `~/.config/opencode/skills/` | The model calls the `skill` tool |
| Cline | `.cline/skills/` or `.claude/skills/` | `~/.cline/skills/` | Picked up on its own, or `/building-telephony-voice-agents` |
| Roo Code | `.roo/skills/` or `.agents/skills/` | `~/.roo/skills/` | Picked up on its own |
| Goose | `.agents/skills/` | `~/.agents/skills/` | Picked up on its own |
| Amp | `.agents/skills/` | `~/.config/agents/skills/` | `amp skill add samayyy/building-telephony-voice-agents` |
| Kiro | `.kiro/skills/` | `~/.kiro/skills/` | Picked up on its own, or `/building-telephony-voice-agents` (GitHub import needs the `skills/building-telephony-voice-agents` subfolder URL) |
| Anything else | Use [`openskills`](https://github.com/numman-ali/openskills) to list the skill in `AGENTS.md` | | `npx openskills read building-telephony-voice-agents` |

`.agents/skills/` is becoming the shared location: Codex, Cursor, Antigravity, Gemini CLI, Copilot, Roo, Goose and
Amp all read it.

---

## How to use it

Just describe the job. The skill tells the agent which reference to read, and its templates give working code to
start from. For example:

- *"Build an inbound voice agent on Gemini Live for our Plivo number using LiveKit Cloud. Callers speak Hindi and English."*
- *"We only have Exotel's Voicebot applet. Build a WebSocket bridge to Gemini Live at 16 kHz."*
- *"Self-host LiveKit with SIP on a GCP VM in Mumbai and connect a Twilio Elastic SIP trunk."*
- *"Callers say the bot replies before they speak, right after the greeting. Debug it."*
- *"Add hold music while the agent looks up an order, and transfer to a human on request."*
- *"Our carrier gave us a WebSocket streaming guide under NDA. Write an adapter for the bridge."*
- *"Set up a test harness that replays our call recordings against the new prompt."*

The agent usually works in this order:
1. Pick a path using the decision tree in `SKILL.md`.
2. Configure the model (`references/02`).
3. Wire up the telephony (`references/06` for SIP, `07` for streams).
4. Get the audio right (`references/08`).
5. Get the conversation mechanics right (`references/03`).
6. Write the prompt (`references/09`).
7. Test (`references/10`).
8. Ship (`references/11`).

### Running the templates yourself

```bash
# audio conversion self-test (no network; numpy, scipy, soxr, audioop-lts)
pip install -r skills/building-telephony-voice-agents/templates/ws_bridge/requirements.txt
python skills/building-telephony-voice-agents/scripts/audio_selftest.py      # -> RESULT: PASS

# LiveKit agent:  see templates/livekit/ (env.example, livekit.toml.example, Dockerfile)
# WebSocket bridge: see templates/ws_bridge/README.md (uvicorn bridge:app, wss:// via a TLS terminator)
# SIP config:     see templates/sip/README.md (lk sip inbound create inbound_trunk.json ...)
# Self-hosted:    see templates/selfhost/README.md (docker compose up)
```

You'll need these accounts and credentials, set as environment variables (never in code):
- a Google Cloud project with Vertex AI, or a Gemini API key;
- a LiveKit Cloud project or your own server;
- a telephony provider account.

---

## Status and caveats

- **Checked on 2026-10-21** against livekit-agents 1.7.1 (in production) and 1.8.8 (docs), google-genai
  2.21.0 / 2.32.0, `gemini-3.8-live`, and current provider docs. These APIs change fast, so re-check versions; the
  skill tells the agent where to look things up.
- **About 80 `VERIFY` markers** flag details the docs didn't confirm, or where sources disagree (for example Exotel
  outbound auth, Vonage hang-up, India rule dates). Confirm them with the provider before you rely on them.
- **Testing so far.** The templates compile, the audio self-test passes, and the bridge passed offline smoke tests
  with a fake Gemini session. **None of the templates has run on a live phone call yet.** Make one real call per
  provider before production.
- **Greeting barge-in threshold.** The bridge detects greeting barge-in with a simple energy VAD, but the 0.8 s
  threshold was measured with Silero VAD. Re-measure it on your own lines.
- **Voice default.** The default voice is **Gacrux**, the calmest in blind tests on narrowband phone audio. Run the
  voice A/B tool for your own callers.
- **Not legal advice.** The India compliance notes are a starting checklist; confirm them with your carrier and
  counsel.

## Contributing

Issues and PRs are welcome, especially:
- results from live calls;
- new provider adapters;
- resolved `VERIFY` items, with a source link.

Keep `SKILL.md` under 500 lines and put detail in `references/`. Never commit credentials, customer data or protocol
documents shared under NDA.

## License

[MIT](LICENSE)
