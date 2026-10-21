# 04. LiveKit Cloud for phone agents

Use this file when the agent runs on LiveKit Cloud: project and `lk` CLI setup, deploying and operating the agent, regions and cold starts, SIP inbound and outbound trunks, dispatch rules, transfers and DTMF, SIP participant attributes, the Twilio Connector, noise cancellation, recording with Egress, the LiveKit Inference gateway, pricing, and which API surface (1.7.1 vs 1.8.x) is verified. Generic LiveKit agent concepts (tasks, handoffs, testing, simulations) belong to LiveKit's own skills (`building-livekit-agents`, `operating-livekit-agents`, `testing-livekit-agents`) and the LiveKit Docs MCP (`claude mcp add --transport http livekit-docs https://docs.livekit.io/mcp`). Every docs page has a Markdown twin at `https://docs.livekit.io/<path>.md`. This API surface moves quickly, so check the docs before relying on any detail below.

## Contents

1. [What is verified, and against which version](#1-what-is-verified-and-against-which-version)
2. [Project setup and the lk CLI](#2-project-setup-and-the-lk-cli)
3. [Deploying the agent](#3-deploying-the-agent)
4. [Secrets and Google credentials](#4-secrets-and-google-credentials)
5. [Regions](#5-regions)
6. [Cold start and warm capacity](#6-cold-start-and-warm-capacity)
7. [SIP inbound: trunk and dispatch rule](#7-sip-inbound-trunk-and-dispatch-rule)
8. [SIP outbound: trunk and create_sip_participant](#8-sip-outbound)
9. [Transfers, DTMF, AMD](#9-transfers-dtmf-amd)
10. [SIP participant attributes and hang-up](#10-sip-participant-attributes-and-hang-up)
11. [Twilio Connector](#11-twilio-connector)
12. [Noise cancellation](#12-noise-cancellation)
13. [Recording: agent-side recorder vs Egress](#13-recording)
14. [LiveKit Inference gateway for Gemini Live](#14-livekit-inference-gateway)
15. [Pricing notes](#15-pricing-notes)

---

## 1. What is verified, and against which version

### 1.1 Version facts

| Item | 1.7.1 (production) | 1.8.x (current 1.8.8, 2026-10-15) | How verified |
|---|---|---|---|
| `AgentServer`, `@server.rtc_session(agent_name=, on_request=, on_session_end=)`, `server.setup_fnc` | Present | Present | Installed source of both |
| `room_io.RoomOptions(participant_identity, participant_kinds, close_on_disconnect=True, delete_room_on_close=False, audio_input=AudioInputOptions(...))` | Present | Present | Installed source of both |
| Legacy `WorkerOptions(entrypoint_fnc=, prewarm_fnc=)`, `RoomInputOptions`/`RoomOutputOptions` | Exported, deprecated | Exported, deprecated | Source |
| `AgentSession(aec_warmup_duration=, user_away_timeout=15.0, ivr_detection=)` | Present | Present | Source |
| `llm.RealtimeModelFallbackAdapter(models, cooldown=10.0, regenerate_on_swap=True)` | Present | Present | Source |
| `livekit.agents.beta.tools.EndCallTool(delete_room=True, end_instructions=, ignore_on_enter=)` | Present | Present | Source |
| `session.say(text, audio=frames)` with a realtime model | Works | Works (guard only fires when `audio` is not given) | Source (`agent_activity.py`) |
| `inference.RealtimeModel("google/gemini-3.8-live")` | **Absent** | Present | Source |
| Google plugin resume replays full history (livekit/agents#6000) | Bug, needs workaround (see `03-conversation-mechanics.md`) | Fixed in 1.8.1 | Production notes |
| Tools default to `NON_BLOCKING` on 3.8 | Not sent (3.8 treats tools as non-blocking anyway) | Plugin default for models containing `3.8` (1.8.3+) | Source of 1.8.4 |
| Bad tool schema raised outside `try` and left the agent silent (#7371) | Bug | Fixed 1.8.3 | Production notes |
| Manual activity detection (`automatic_activity_detection.disabled=True`) on 3.8 | Broken (answers off by one) | Still broken (bare `turn_complete`) in 1.8.3 | Live probes |

The "1.7.1 vs 1.8.x" split is **not** an `AgentServer` vs `WorkerOptions` split: 1.7.1 already has `AgentServer`, `rtc_session` and `RoomOptions`. The practical differences are the Google plugin's Vertex model check, the resume-history fix, the tool behaviour default, and LiveKit Inference.

### 1.2 The Vertex model-name bug (plugin 1.8.2 to 1.8.4)

`livekit-plugins-google` validates the model name against two frozen sets. Diffing the published wheels gives:

| Plugin version | `gemini-3.8-live` in Gemini-API set | in Vertex set | `vertexai=True` + `model="gemini-3.8-live"` |
|---|---|---|---|
| 1.7.1, 1.8.0, 1.8.1 | no | no | Accepted (no check matches) |
| **1.8.2, 1.8.3, 1.8.4** | yes | no | **`ValueError: Model 'gemini-3.8-live' is a Gemini API model, but vertexai=True`** at construction |
| 1.8.5 to 1.8.8 | yes | yes | Accepted; mismatches only log a warning |

Sources: wheel diff of `livekit/plugins/google/realtime/realtime_api.py` (done 2026-10-21); community thread https://community.livekit.io/t/gemini-3-8-live-rejected-with-vertexai-true-in-livekit-plugins-google-although-vertex-ai-serves-it-eu-multi-region/2179 (fix PR #7587).

Options, best first:
1. **Use 1.8.5 or later** if you are on 1.8.x.
2. **Stay on 1.7.1, pinned exactly** (what production did, because a framework upgrade also moved the cascaded pipeline).
3. **On 1.8.2-1.8.4, pass a path-form name:** `model="publishers/google/models/gemini-3.8-live"` (or the full `projects/<p>/locations/<l>/publishers/google/models/gemini-3.8-live`). Why it works: the path is not in either frozen set, so the check passes; google-genai's `t_model()` keeps names that start with `publishers/` or `projects/`, and `live.py` prefixes `projects/{project}/locations/{location}/`, so the wire request is identical. The plugin's 3.8-specific logic uses substring tests (`"3.8" in model`), so it still applies. Verified by reading google-genai 2.21.0 and plugin 1.8.4 source. **Not verified with a live connect through 1.8.2-1.8.4.**

`templates/livekit/agent_gemini_live.py` applies option 3 automatically when it detects a plugin version in the affected range.

### 1.3 Pinning

- Pin `livekit-agents[google,silero]`, `livekit-plugins-google`, `livekit-plugins-silero` and `google-genai` **exactly** (`==`). A Docker image that does `pip install .` from `pyproject.toml` ignores `uv.lock`, so `~=1.7.0` let a rebuild silently pull 1.8.x. Production kept a unit test asserting the exact pins.
- `livekit-plugins-noise-cancellation~=0.3` is fine (0.3.0 in production, 0.3.2 current).

## 2. Project setup and the lk CLI

```bash
brew install livekit-cli          # Linux: curl -sSL https://get.livekit.io/cli | bash
lk cloud auth                     # browser flow; links one project per run and mints a CLI key
lk project list                   # '*' marks the default
lk project set-default "<project-name>"
lk app env -w                     # writes LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET to .env.local
lk agent init my-agent --template agent-starter-python   # official starter (tests, Dockerfile, AGENTS.md)
```

- Every command also takes `--url --api-key --api-secret`, `--project NAME`, or the `LIVEKIT_*` env vars.
- Revoke with `lk cloud auth --revoke [--project NAME]`. This also revokes keys written by `lk app env`.
- Use the region-pinned realtime URL in production config (`wss://<subdomain>.<region>.rtc.livekit.cloud`); a readiness check can assert it.
- `lk docs` and the Docs MCP search the current docs. `lk skills install` (CLI 2.19.0+) installs LiveKit's agent skills and the Docs MCP.
- Source: https://docs.livekit.io/reference/developer-tools/livekit-cli/projects.md

## 3. Deploying the agent

Docs: https://docs.livekit.io/deploy/agents.md, https://docs.livekit.io/deploy/agents/managing-deployments.md, https://docs.livekit.io/reference/developer-tools/livekit-cli/agent/deployment.md

### 3.1 Commands (checked against lk 2.18.5 `--help`)

```bash
lk agent create --region ap-south --secrets-file ./secrets.env --secret-mount ./gcp-sa.json
                                   # registers the agent, writes livekit.toml, generates a Dockerfile if missing, builds, deploys
lk agent deploy                    # new version, rolling; -d staging for a non-production deployment
lk agent status                    # Running/Sleeping, CPU, memory, replicas (e.g. 0/1/1)
lk agent logs                      # alias tail; --log-type build for build logs; -d staging
lk agent versions                  # Production / Draining / Active
lk agent rollback [--version <VERSION_ID>]   # no rebuild; paid plans
lk agent secrets                   # names only; values can never be read back
lk agent update-secrets --secrets KEY=VAL [--secrets-file f] [--secret-mount ./file.json]   # rolling restart
lk agent update ...                # metadata and secrets only; there is no replica/scaling flag
lk agent restart                   # restart the pool without interrupting active sessions
lk agent promote -d staging        # promote a staging image to production
lk agent list ; lk agent delete [--id CA_xxx] [-d staging]   # without -d deletes the whole agent
lk agent config --id CA_xxx        # regenerate livekit.toml for an existing agent
lk agent dockerfile [--overwrite]  # generate Dockerfile + .dockerignore
lk agent dev | console | start     # local runners (dev registers to Cloud with auto-reload)
lk agent simulate --scenarios evals/scenarios.yaml agent.py   # Cloud simulations (see 10-testing-and-evaluation.md)
```

Conflict to note: lk 2.18.5's help says `rollback --version` "defaults to most recent previous to current". In production, the rollback only worked with `--version` given explicitly. Pass it.

### 3.2 livekit.toml

```toml
[project]
  subdomain = "<project-subdomain>"

[agent]
  id = "<agent-id>"
```

Generated by `lk agent create` or `lk agent config`. For multi-region, create one agent per region with its own file (`lk agent create --region us-east --config livekit.us-east.toml`, then `lk agent deploy --config livekit.us-east.toml`). Template: `templates/livekit/livekit.toml.example`.

### 3.3 Dockerfile rules

Source: https://docs.livekit.io/deploy/agents/builds.md

- glibc base (`python:3.12-slim` or Debian/Ubuntu slim). Alpine/musl is not supported.
- Explicit `WORKDIR`, non-root `USER`.
- Fixed `CMD`/`ENTRYPOINT` that runs `start` directly (`python agent.py start`). No wrapper scripts or backgrounding. The image does not contain `lk`.
- Bake model files in with `RUN python agent.py download-files` (Silero, turn detector, noise-cancellation weights).
- Do **not** set `LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET`; Cloud injects them.
- Do not copy `.env*`. The build context excludes `.env.*` and `.dockerignore`/`.gitignore` matches.
- Build timeout 10 min, context limit 1 GB.
- Bring-your-own image (`--image`, `--image-tar`) is Enterprise only.

Template: `templates/livekit/Dockerfile`.

### 3.4 Health, rollouts, draining

- New instances must pass the health check (agent HTTP server on `0.0.0.0:8081` in production) within **5 minutes**. Separately, each process's `setup_fnc` (prewarm) has a **hard 10 s** `initialize_process_timeout`; exceeding it kills and respawns the process forever. Keep network warm-up on a daemon thread.
- Old instances drain for up to **1 hour** so live calls finish.
- `LIVEKIT_AGENT_DEPLOYMENT` is `""` in production, the deployment name otherwise, and `dev-...` under `lk agent dev`.
- CI: `livekit/deploy-action` (https://github.com/livekit/deploy-action) with `OPERATION: deploy` and the LiveKit credentials as secrets.

### 3.5 Health lines worth logging (pattern from production)

Grep these after every deploy or secret change:
- per process: `live warmup: ok=True location=us greeting_cached=True`
- per call: `pipeline=realtime live_location=us warmup=ok greeting_cached=True`
- `warmup=unprobed` means a cold call arrived before prewarm finished.

## 4. Secrets and Google credentials

Source: https://docs.livekit.io/deploy/agents/secrets.md

- Secrets become environment variables. `.env.local` changes never reach the deployed worker.
- Names: letters, digits and `_`, up to 70 characters. Values up to 16 KB.
- **File secrets:** `--secret-mount ./google-application-credentials.json` mounts the file under `/etc/secrets/` (per the docs). Set `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/<file>` as a secret.
- Production lesson: the mount path is decided by the host. Discover it instead of hard-coding: search `/etc/secrets`, `/run/secrets`, `/secrets`, `/var/secrets`, `/app` for a JSON file with `"type": "service_account"`, set `GOOGLE_APPLICATION_CREDENTIALS`, log the path, else fall back to ADC. Do it in prewarm and again in `on_session_end`. A wrong path otherwise fails at the first Google client construction, which means silence on a connected line. The template implements this (`resolve_google_credentials()`).
- Secret values cannot be read back. After rotating a credential, re-validate end to end.
- Two rollback levers: flip a behaviour secret (e.g. `AGENT_PIPELINE=cascaded`, about 30 s, same code) or roll back the version.

## 5. Regions

| Thing | Values | Notes |
|---|---|---|
| Agent hosting | `us-east` (Ashburn), `eu-central` (Frankfurt), `ap-south` (Mumbai) | Fixed at creation. To move, create in the new region and delete the old agent. https://docs.livekit.io/deploy/admin/regions/endpoints.md#agent-deployments |
| SIP inbound endpoint | `sip:<subdomain>.sip.livekit.cloud`; pinned `<subdomain>.<region>.sip.livekit.cloud` with region in `eu, india, sa, us, japan, aus, canada` | The subdomain is the project ID without `p_` (`lk project list --json`). https://docs.livekit.io/telephony/start/sip-trunk-setup.md |
| SIP outbound pinning | `destination_country` (ISO-2, e.g. `in`, `us`) on the outbound trunk or inline config | |
| Static IPs | `143.223.88.0/21`, `161.115.160.0/19`, `153.57.128.0/18` for realtime, SIP signalling/media and webhooks in CA/EU/India/Japan/US | Elsewhere, use digest auth at the provider |
| Gemini Live (Vertex) | `us`, `eu`, `us-central1` only for `gemini-3.8-live` | See `02-gemini-live-model-and-config.md` |

India: SIP endpoint `<subdomain>.india.sip.livekit.cloud`, agent in `ap-south`, Gemini `us` with `us-central1` fallback. LiveKit's Plivo guide says India numbers "will fail to connect" without the pinned endpoint. The pricing page lists region pinning under Scale and above; the pinned SIP hostname is what the trunk needs, and production used it. Whether your plan includes it is **VERIFY** (same caveat in `06-sip-trunking-providers.md` section 2 and `12-india-telephony-compliance.md` section 2). Rationale and latency budget: `01-architecture-and-decisions.md` section 7.

## 6. Cold start and warm capacity

- **Build (free) plan:** production deployments can scale to zero. `lk agent status` shows `Sleeping` and replicas `0/1/1`. The first call after idle waits **10-20 s** for the agent to join while the caller hears ringing (observed in production as "about 10 rings": the only two slow pickups were after 347 and 1,360 idle minutes).
- **Ship/Scale:** production "stays warm" (pricing table: "Cold start prevention"). Non-production deployments always scale to zero.
- There is **no min-replicas flag** in `lk agent create/update/deploy` (checked in lk 2.18.5). Warm capacity is a plan or dashboard decision. On Build, place a wake-up test call before demos.
- Inside a warm instance, `num_idle_processes` (production default: CPU count) keeps processes pre-spawned with `setup_fnc` already run.
- An inbound caller hears ringing until the agent publishes audio and the SIP participant subscribes. The trunk `ringing_timeout` caps that wait, then sends `486 Busy Here` (status configurable with `ringing_timeout_status`).
- Everything before dispatch (INVITE, ringing, worker start) is invisible to agent analytics. Emit a `greeting_played{prerendered, duration_ms}` event so the opening cost is visible.

## 7. SIP inbound: trunk and dispatch rule

Building blocks (https://docs.livekit.io/telephony.md): an **inbound trunk** accepts calls from the provider, a **dispatch rule** picks the room and the agent, an **outbound trunk** places calls, and the caller becomes a **SIP participant** (`kind == SIP`). Supported: SIP over UDP/TCP/TLS, RFC 2833/4733 DTMF, REFER cold transfer, warm transfer, caller ID, SIP OPTIONS, RTP/SRTP. **Not supported:** SIP REGISTER, SIPREC, video.

### 7.1 Inbound trunk

```bash
lk sip inbound create templates/sip/inbound_trunk.json
lk sip inbound list
lk sip inbound update --id ST_xxx --numbers +91XXXXXXXXXX   # field update; a JSON file passed to update REPLACES the whole trunk
```

Fields (`SIPInboundTrunkInfo`, verified in livekit-api 1.2.2): `name, metadata, numbers, allowed_addresses, allowed_numbers, auth_username, auth_password, auth_realm, headers, headers_to_attributes, attributes_to_headers, include_headers, ringing_timeout, max_call_duration, krisp_enabled, media_encryption, media`.

- `numbers` must match the To/Request-URI the carrier sends, E.164 with `+`. Several numbers can share one trunk. Create one trunk and reuse it; never per call.
- With `numbers: []` you must set digest auth or `allowed_addresses`. On Cloud, `allowed_addresses` must be enabled by LiveKit support.
- Twilio Elastic SIP cannot send digest credentials on origination. Use number matching.
- `headers_to_attributes` maps only `X-*` headers, and they arrive asynchronously (listen to `participant_attributes_changed`).
- `krisp_enabled` is trunk-side Krisp. Prefer agent-side BVCTelephony and do not run both.
- Source: https://docs.livekit.io/telephony/accepting-calls/inbound-trunk.md

### 7.2 Dispatch rule

```bash
lk sip dispatch create templates/sip/dispatch_rule.json
lk sip dispatch list
```

- **There is no `--agent` flag.** Put `room_config.agents` in the JSON file. CLI shortcuts exist only for the rule type: `--caller PREFIX` (alias `--individual`), `--direct ROOM`, `--callee PREFIX --randomize`.
- Rule types (https://docs.livekit.io/telephony/accepting-calls/dispatch-rule.md):
  - `dispatch_rule_individual {room_prefix, pin, no_randomness}`: one room per caller, named `<prefix><callerNumber>_<random>`. The caller number appears in the room name (unredacted PII).
  - `dispatch_rule_direct {room_name, pin}`: everyone into one room.
  - `dispatch_rule_callee {room_prefix, randomize}`: room named after the dialled number.
- `room_config.agents[]` (`RoomAgentDispatch`: `agent_name, metadata, restart_policy, deployment, attributes`) is **explicit dispatch**. `agent_name` must equal `@server.rtc_session(agent_name=...)`. Without `agent_name` on the worker, auto-dispatch joins every new room.
- `hide_phone_number: false` is required for the agent to read the caller's number (`sip.phoneNumber`).
- `attributes` on the rule are inherited by SIP participants. `ctx.job.metadata` carries the dispatch `metadata`.
- Keep any fallback rule pointed at a deployed agent name. A rule naming a retired agent reaches nothing.
- Python equivalent:
  ```python
  await lkapi.sip.create_sip_dispatch_rule(api.CreateSIPDispatchRuleRequest(dispatch_rule=api.SIPDispatchRuleInfo(
      name="inbound-to-my-agent", trunk_ids=["ST_xxx"], hide_phone_number=False,
      rule=api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-")),
      room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="my-agent")]))))
  ```

### 7.3 LiveKit Phone Numbers

US local and toll-free numbers only, Cloud only. They need a dispatch rule but no inbound trunk: `lk number search --country-code US`, `lk number purchase --numbers +1... --sip-dispatch-rule-id SDR_...`. Not an option for Indian DIDs. Source: https://docs.livekit.io/telephony/start/phone-numbers.md

### 7.4 Acceptance call checklist

1. Provider logs show the call; LiveKit dashboard Telephony/Sessions show it; cross-reference provider and LiveKit call IDs (`sip.callID`).
2. An individual room and a SIP participant exist; the agent joined and published audio.
3. Clean hang-up from both sides; recording and transcript uploaded.
4. If it fails before LiveKit, read the provider logs first. If LiveKit received it but no agent answered, check the dispatch rule's trunk and `agent_name`, then the worker logs.

Carrier-specific setup (Plivo, Twilio, Exotel, Telnyx, operators via SBC): `06-sip-trunking-providers.md`.

## 8. SIP outbound

### 8.1 Outbound trunk

```bash
lk sip outbound create templates/sip/outbound_trunk.json --auth-user "$SIP_USER" --auth-pass "$SIP_PASS"
```

Fields (`SIPOutboundTrunkInfo`): `name, metadata, address, destination_country, transport, numbers, auth_username, auth_password, headers, headers_to_attributes, attributes_to_headers, include_headers, media_encryption, media, from_host`.
- `address` is a host or IP with optional `:port`, not a SIP URI.
- `transport`: `SIP_TRANSPORT_AUTO|UDP|TCP|TLS`. Set it explicitly for carriers without UDP (Exotel: TCP 5070 or TLS 443).
- `numbers`: caller IDs this trunk may use, or `["*"]` and set `sip_number` per call.
- `destination_country`: outbound region pinning (`in` for India).
- Inline alternative with no stored trunk: `CreateSIPParticipantRequest(trunk=api.SIPOutboundConfig(hostname=..., auth_username=..., auth_password=..., destination_country="IN"), sip_number="+91...")`.

### 8.2 Placing a call

Preferred: dispatch the agent with the number in metadata; the agent dials (template `templates/sip/create_participant_example.py` and the outbound branch in `templates/livekit/agent_gemini_live.py`).

```bash
lk dispatch create --new-room --agent-name my-agent --metadata '{"phone_number": "+91XXXXXXXXXX"}'
```

`CreateSIPParticipantRequest` fields (verified): `sip_trunk_id | trunk, sip_call_to, sip_number, room_name, participant_identity, participant_name, participant_metadata, participant_attributes, dtmf, play_ringtone, play_dialtone, hide_phone_number, headers, include_headers, ringing_timeout, max_call_duration, krisp_enabled, media_encryption, media, wait_until_answered, display_name, destination, to_user_override, sip_request_uri, sip_to_header, sip_from_header`.

| Outcome | `wait_until_answered=True` | `disconnect_reason` | Session auto-closes? |
|---|---|---|---|
| 200 OK | returns (`sip.callStatus=active`) | none | n/a |
| 486/603 | raises `api.SipCallError` | `USER_REJECTED` | yes |
| 408/480 no answer | raises | `USER_UNAVAILABLE` | **no**, call `ctx.shutdown()` |
| 5xx | raises | `SIP_TRUNK_FAILURE` | **no**, call `ctx.shutdown()` |
| Voicemail | returns 200 (not an error) | none | use AMD |

`SipCallError` has `.sip_status_code` and `.sip_status`. `ringing_timeout` can be at most 80 s. Source: https://docs.livekit.io/telephony/making-calls/outbound-calls.md

Outbound with Gemini Live: let the callee speak first ("hello"), then Gemini answers. LiveKit clears the AEC warm-up early for outbound SIP only. Outbound regulation for India (A2P declaration, 140/160 series, consent, calling hours) is in `12-india-telephony-compliance.md`.

## 9. Transfers, DTMF, AMD

### 9.1 Cold transfer (SIP REFER)

```python
await ctx.transfer_sip_participant(sip_participant, "tel:+91XXXXXXXXXX")    # JobContext helper (1.7.1 and 1.8.x)
# or
await ctx.api.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
    room_name=ctx.room.name, participant_identity=sip_identity,
    transfer_to="tel:+91XXXXXXXXXX", play_dialtone=False))
```

- `TransferSIPParticipantRequest` fields: `participant_identity, room_name, transfer_to, play_dialtone, headers, ringing_timeout` (default 30 s; on timeout the request errors and the caller stays in the room).
- `transfer_to`: `tel:+E164` or `sip:+E164@host`. Plivo needs `sip:+E164@<id>.zt.plivo.com`.
- The carrier must allow REFER. Twilio: `twilio api trunking v1 trunks update --sid TKxxx --transfer-mode enable-all --transfer-caller-id from-transferee`.
- CLI: `lk sip participant transfer --room R --identity I --to tel:+91...`.
- Implement as a tool registered **only when a transfer number is configured**. On failure, return a string telling the model the caller is still there and to offer a callback. Without a number, the prompt must never claim to transfer.
- Source: https://docs.livekit.io/telephony/features/transfers/cold.md

### 9.2 Warm transfer

`livekit.agents.beta.workflows.WarmTransferTask` (beta, Python): holds the caller with hold music, dials the human into a consult room, briefs them with a second session that reuses the session's models, then moves the human into the caller's room with `MoveParticipant`. Requirements: start the caller session with `RoomOptions(delete_room_on_close=False)`; an outbound trunk. The official example uses a cascaded pipeline. **Not tested end to end with Gemini Live.** Twilio-connector variant: `TwilioConnectorWarmTransferTask`. Sources: https://docs.livekit.io/telephony/features/transfers/warm.md, https://github.com/livekit/agents/tree/main/examples/warm-transfer

### 9.3 DTMF

- Receive: `@ctx.room.on("sip_dtmf_received")` with `rtc.SipDTMF` (`code`, `digit`, `participant`).
- Send: `await ctx.room.local_participant.publish_dtmf(code=1, digit="1")` (`*`=10, `#`=11); tool `livekit.agents.beta.tools.send_dtmf_events`.
- Dial-after-answer on outbound: `CreateSIPParticipantRequest(dtmf="ww1234#")` (`w` = 0.5 s).
- `AgentSession(ivr_detection=True)` and `livekit.agents.beta.workflows.GetDtmfTask` exist; untested with Gemini Live.
- Source: https://docs.livekit.io/telephony/features/dtmf.md

### 9.4 Answering machine detection

`livekit.agents.AMD` for outbound calls; example `examples/telephony/amd.py` in livekit/agents. Source: https://docs.livekit.io/telephony/features/answering-machine-detection.md

## 10. SIP participant attributes and hang-up

Source: https://docs.livekit.io/reference/telephony/sip-participant.md

| Attribute | Meaning |
|---|---|
| `sip.callID` | LiveKit SIP call ID (support/debug) |
| `sip.callIDFull` | Provider's SIP Call-ID |
| `sip.callStatus` | `dialing` -> `ringing` -> `active` -> `hangup` (`automation` while outbound DTMF plays) |
| `sip.phoneNumber` | Inbound: **caller's** number. Outbound: number dialled. Missing if `hide_phone_number` |
| `sip.trunkPhoneNumber` | Inbound: the number dialled (your DID). **Never use it as the caller's number** |
| `sip.trunkID`, `sip.ruleID` | Trunk and rule IDs (`ruleID` empty for outbound) |
| `sip.twilio.accountSid`, `sip.twilio.callSid` | Twilio trunks only |

- Read the caller number defensively in preferred order `sip.phoneNumber`, `sip.from`, `sip.fromUser`, requiring at least one digit. Log which keys arrived (values masked) so one real call settles what the platform sends.
- Resolve it **before** `session.start` (wait for the participant with an 8 s bound) and bake it into the prompt. On Vertex, `update_instructions()` mid-call injects the whole prompt as a model turn (see `02`).
- With `delete_room_on_close=True` the SIP participant is gone by the time `on_session_end` runs. Stash the number in `ctx.proc.userdata[f"caller_number:{job_id}"]` and pop it on read.
- All INVITE headers synchronously: RPC `lk.sip.GetRemoteHeaders` via `ctx.room.local_participant.perform_rpc(destination_identity=sip_identity, method="lk.sip.GetRemoteHeaders", payload="{}")`.

Disconnect reasons: `CLIENT_INITIATED` (either side hung up), `ROOM_DELETED`, `USER_REJECTED` all auto-close the session with `close_on_disconnect=True`. `USER_UNAVAILABLE` and `SIP_TRUNK_FAILURE` (outbound) do not.

**Hanging up from the agent:** `AgentSession.shutdown()` never touches the phone leg; without room deletion the caller hears silence until they hang up. Use `RoomOptions(delete_room_on_close=True)` plus `session.shutdown(drain=True)`: the room is deleted after the drain, so the goodbye is never clipped. Do not call `ctx.delete_room()` yourself right after `shutdown()`: shutdown is non-blocking, so the delete lands before the drain and clips the goodbye, and a fire-and-forget delete is cancelled at teardown. `EndCallTool(delete_room=True)` is the stock alternative, but it plays the tool-reply speech and shuts down with no grace window; the template uses a custom `end_call` (see `03-conversation-mechanics.md`).

## 11. Twilio Connector

Source: https://docs.livekit.io/telephony/connectors/twilio.md, API https://docs.livekit.io/reference/telephony/connectors-api.md

LiveKit Cloud bridges Twilio Media Streams into a room without SIP. Verified in livekit-api 1.2.1 and 1.2.2: `api.ConnectTwilioCallRequest` fields `twilio_call_direction` (`TWILIO_CALL_DIRECTION_INBOUND|OUTBOUND`), `room_name`, `agents`, `participant_identity`, `participant_name`, `participant_metadata`, `participant_attributes`, `destination_country`; response `connect_url`; method `lkapi.connector.connect_twilio_call(...)`.

```python
from livekit import api

async def twiml_for_inbound(call_sid: str) -> str:
    async with api.LiveKitAPI() as lkapi:
        resp = await lkapi.connector.connect_twilio_call(api.ConnectTwilioCallRequest(
            # nested enum on the request class (verified livekit-api 1.2.2): INBOUND=0, OUTBOUND=1
            twilio_call_direction=api.ConnectTwilioCallRequest.TWILIO_CALL_DIRECTION_INBOUND,
            room_name=f"call-{call_sid}",
            agents=[api.RoomAgentDispatch(agent_name="my-agent")],
            destination_country="US",
        ))
    return f'<Response><Connect><Stream url="{resp.connect_url}"/></Connect></Response>'
```

- Outbound: create the Twilio call with `Url` set to the `https` form of `connect_url` (replace `wss` with `https`). The connector participant joins the room immediately as a hidden participant.
- The caller appears as participant kind `CONNECTOR`, not `SIP`, so SIP attributes such as `sip.phoneNumber` are absent; pass caller details through `participant_attributes` or dispatch metadata. Wait with `ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_CONNECTOR)` . `CONNECTOR` is in `DEFAULT_PARTICIPANT_KINDS` (with `SIP` and `STANDARD`) in both 1.7.1 and 1.8.8, so RoomIO links it without extra options. Not tested on a real connector call.
- Audio is still 8 kHz mu-law at Twilio; LiveKit does the transcoding.
- **VERIFY before promising:** REFER-style transfer and DTMF through the connector are not documented. Warm transfer has a dedicated `TwilioConnectorWarmTransferTask`.
- Cloud only. Plivo, Exotel and carrier CPaaS streams have no connector; use SIP or a bridge (`07-websocket-media-streams.md`).

## 12. Noise cancellation

Source: https://docs.livekit.io/transport/media/noise-cancellation.md

```python
from livekit.plugins import noise_cancellation   # livekit-plugins-noise-cancellation~=0.3
room_options = room_io.RoomOptions(audio_input=room_io.AudioInputOptions(
    sample_rate=16000, frame_size_ms=50,
    noise_cancellation=noise_cancellation.BVCTelephony()))   # BVC tuned for SIP callers
```

- Runs inside the native audio stream, so Gemini and its server VAD hear only denoised audio. Background-voice cancellation matters more on realtime, because Gemini's server VAD alone decides barge-in.
- It is **not** echo cancellation. LiveKit applies no AEC to SIP participants (see the greeting hold design in `03`).
- Use **one** method: not BVC in code and Krisp on the trunk.
- Risk: it can clip soft onsets ("haan", "ji", single digits). A/B on real calls.
- Newer options in the docs: `livekit-plugins-krisp` (`krisp.voice_isolation_telephony(...)`) and ai-coustics. Not tested here.
- **Licensing:** these models authenticate against LiveKit Cloud with the room token, so they only work when the room is on LiveKit Cloud. Self-hosted needs your own Krisp SDK license or an ai-coustics key (see `05`).

## 13. Recording

Two ways to get call audio. Decide recording governance (consent disclosure, retention, residency, redaction) first; LiveKit warns that redaction is inaccurate on realtime sessions because user turns lack full speech timestamps.

### 13.1 Agent-side recorder (what production used)

- `session.start(record=...)` turns on Agent Observability's recorder. `record` accepts `True`, `False` or a `RecordingOptions` dict such as `{"audio": True, "transcript": True, "traces": True, "logs": True, "redaction": True}` (verified 1.7.1 and 1.8.8). Fail closed: pass `False` unless governance flags are set.
- The file is **stereo, caller on channel 0 (left), agent on channel 1 (right)**, which makes per-channel analysis possible. Cloud retention is a fixed 30 days and may be processed in the US.
- The file lives in the job container and disappears with it. Copy it out in `on_session_end`. The session report's `audio_recording_path` came back **empty** in production; the recorder actually writes `ctx.session_directory / "audio.ogg"`. Try both and log both.
- Object naming: `YYYY/MM/DD/<session_id>.ogg` partitioned in the business timezone; set `content_type="audio/ogg"`; skip zero-byte files.
- Background-audio tracks are not in this file.

### 13.2 Egress to your bucket (new StartEgress API)

Verified constructible in livekit-api 1.2.2 (also present in 1.2.1): `StartEgressRequest(room_name, template|web|media, preset, advanced, outputs, storage, webhooks)`, `MediaSource(audio=AudioConfig(capture_all, routes=[AudioRoute(track_id|participant_identity|participant_kind, channel)]))`, `AudioChannel.AUDIO_CHANNEL_LEFT|RIGHT|BOTH`, `ParticipantInfo.Kind.SIP|AGENT|CONNECTOR`, `Output(file=FileOutput(file_type, filepath))`, `StorageConfig(gcp=GCPUpload(...) | s3=S3Upload(...))`. Not tested against a live server.

```python
from livekit import api

def stereo_call_recording(room_name: str, bucket: str, gcs_sa_json: str) -> api.StartEgressRequest:
    # Caller LEFT, agent RIGHT: same orientation as the agent-side recorder.
    return api.StartEgressRequest(
        room_name=room_name,
        media=api.MediaSource(audio=api.AudioConfig(routes=[
            api.AudioRoute(participant_kind=api.ParticipantInfo.Kind.SIP,   channel=api.AudioChannel.AUDIO_CHANNEL_LEFT),
            api.AudioRoute(participant_kind=api.ParticipantInfo.Kind.AGENT, channel=api.AudioChannel.AUDIO_CHANNEL_RIGHT),
        ])),
        outputs=[api.Output(file=api.FileOutput(file_type=api.EncodedFileType.OGG, filepath=f"calls/{room_name}.ogg"))],
        storage=api.StorageConfig(gcp=api.GCPUpload(credentials=gcs_sa_json, bucket=bucket)),
        # S3: storage=api.StorageConfig(s3=api.S3Upload(access_key=..., secret=..., region="ap-south-1", bucket=...))
    )

info = await ctx.api.egress.start_egress(stereo_call_recording(ctx.room.name, "my-call-recordings", sa_json))
# stop: await ctx.api.egress.stop_egress(api.StopEgressRequest(egress_id=info.egress_id))
```

- Start it after the SIP participant is linked, from the entrypoint. It records what is in the room (including background audio tracks, unlike the agent recorder).
- Legacy equivalent, still accepted: `RoomCompositeEgressRequest(room_name=..., audio_only=True, audio_mixing=api.AudioMixing.DUAL_CHANNEL_AGENT, file_outputs=[api.EncodedFileOutput(...)])` via `start_room_composite_egress`.
- GCS: the service account needs Storage Object Creator; pass the JSON key contents. S3-compatible stores need `force_path_style=true`.
- Limits: file egress max 3 h; concurrent egress Build 2 / Ship 100 / Scale 500; audio-only transcode $0.004/min after included minutes.
- Source: https://docs.livekit.io/reference/other/egress/api.md

## 14. LiveKit Inference gateway

1.8.x only (absent in 1.7.1). `livekit.agents.inference.RealtimeModel` lists `google/gemini-3.8-live`, `google/gemini-3.8-live-extended-thinking` and `google/gemini-live` (read in 1.8.8 source).

```python
from livekit.agents import AgentSession, inference
session = AgentSession(llm=inference.RealtimeModel(model="google/gemini-3.8-live", voice="Puck"))
# or simply AgentSession(llm="google/gemini-3.8-live")
```

| | LiveKit Inference | Google plugin with your Vertex project |
|---|---|---|
| Credentials | None from Google; uses the LiveKit project | ADC / service account / API key |
| Billing | LiveKit invoice | Google invoice (committed-use discounts apply) |
| Region control | Not exposed as Vertex location (**VERIFY** where it runs) | `location="us"` etc. |
| Live config surface | Generic realtime options (`voice`, `modalities`, `input_audio_transcription`, `turn_detection`, `max_session_duration`, ...) | Full Gemini config (`realtime_input_config`, `context_window_compression`, `session_resumption`, custom vocabulary, `http_options`, ...) |
| Concurrency on Build plan | 5 realtime sessions | Vertex quota (PayGo docs say 1,000 per project; check Quotas) |
| Self-hosted LiveKit | Not available | Works |

Use the gateway for prototypes or when the customer will not hold a Google contract. Use the plugin when you need the Gemini-specific settings this skill relies on (compression, resumption, transcription vocabulary, region fallback), or residency control. **Not tested in a phone deployment.**

## 15. Pricing notes

Fetched from https://livekit.com/pricing on 2026-10-21. Re-check before quoting.

| Item | Build ($0) | Ship (from $50/mo) | Scale (from $500/mo) |
|---|---|---|---|
| Agent session minutes included, then | 1,000 | 5,000, then $0.01/min | 50,000, then $0.01/min |
| Concurrent Cloud agent sessions | 5 | 20 | up to 600 (starts at 50) |
| Agent deployments / non-prod | 1 / 0 | 2 / 2 | 4 / 5 |
| Third-party SIP minutes included, then | 1,000 | 5,000, then $0.004/min | 50,000, then $0.003/min |
| Cold start prevention | no (scale to zero) | yes | yes |
| Instant rollback | no | yes | yes |
| Region pinning, private links, HIPAA | no | no | yes |

- Voice isolation (Krisp VIVA telephony, ai-coustics): 100 / 1,000 / 10,000 minutes included, then $0.0012/min. Background noise suppression is a separate line; check current numbers.
- LiveKit US numbers: local $1/mo after one free; toll-free $2/mo + $0.02/min.
- Server API rate limit: 1,000 requests/min.
- Model costs (Gemini) are separate; see `01-architecture-and-decisions.md` section 8.
- Source for limits: https://docs.livekit.io/deploy/admin/quotas-and-limits.md
