# Deployment, operations and integrations

Purpose: how to ship, run and observe a phone voice agent, and how to turn each call into reliable downstream
records (analytics, recordings, transcripts, CRM tickets) without ever making the caller wait for them. Covers deploy
and rollback, secrets and credentials, readiness checks, dependency pinning, cold starts and prewarm, per-call
observability, post-call extraction with a stronger LLM, CRM (Zendesk-shaped) integration, PII and security, and cost
monitoring. Examples assume LiveKit Cloud agents (CLI `lk` 2.18-2.19, livekit-agents 1.7.1 in the measured
production system, 1.8.8 current on 2026-10-21) and Google Cloud for storage/analytics; the patterns carry over to a
self-hosted bridge. For LiveKit-generic operations (worker model, drain, event-loop errors) also see LiveKit's
official `operating-livekit-agents` skill (https://github.com/livekit/agent-skills); for LiveKit Cloud specifics see
`references/04-livekit-cloud.md`.

## Contents

1. [Principles](#1-principles)
2. [Deploy, rollback, secrets, status, logs](#2-deploy-rollback-secrets-status-logs)
3. [Pin dependencies exactly](#3-pin-dependencies-exactly)
4. [Config validation and readiness checks](#4-config-validation-and-readiness-checks)
5. [Credentials](#5-credentials)
6. [Cold start, warm replicas and prewarm](#6-cold-start-warm-replicas-and-prewarm)
7. [Observability: events, outbox, Pub/Sub, BigQuery](#7-observability-events-outbox-pubsub-bigquery)
8. [Recordings and transcripts](#8-recordings-and-transcripts)
9. [Post-call pipeline](#9-post-call-pipeline)
10. [Post-call extraction with a stronger LLM](#10-post-call-extraction-with-a-stronger-llm)
11. [CRM / ticketing integration (Zendesk-shaped)](#11-crm--ticketing-integration-zendesk-shaped)
12. [Security and PII](#12-security-and-pii)
13. [Cost monitoring](#13-cost-monitoring)
14. [SLOs and alerts to start with](#14-slos-and-alerts-to-start-with)

---

## 1. Principles

- **Nothing on the call path does network I/O it can avoid.** Analytics append to a local outbox; CRM writes,
  extraction and uploads happen after the caller has hung up.
- **An optimisation is never the reason a caller hears nothing.** Every cache (greeting audio, warm CRM client,
  model probe) has a slow-but-working fallback.
- **A config typo never costs a call.** Tuning errors are logged and fall back; only missing hard requirements refuse
  calls, and the readiness check fails the deploy before that can happen.
- **Never lose a record.** If extraction fails, file an unclassified ticket from the transcript; if the CRM fails,
  keep the payload for replay.
- **Record what actually happened** (pipeline that answered, model location, prerendered greeting) rather than build
  labels.

## 2. Deploy, rollback, secrets, status, logs

LiveKit Cloud (docs: https://docs.livekit.io/deploy/agents/managing-deployments.md,
https://docs.livekit.io/reference/developer-tools/livekit-cli/agent/deployment.md):

```bash
lk agent create --region ap-south --secrets-file ./secrets.env --secret-mount ./gcp-sa.json   # once; region is immutable
lk agent deploy                                   # build from Dockerfile, rolling deploy of a new version
lk agent status                                   # Running/Sleeping, CPU, memory, replicas (e.g. 0/1/1)
lk agent logs                                     # runtime logs;  --log-type build for build logs
lk agent versions                                 # Production / Draining / Active
lk agent rollback --version <VERSION_ID>          # no rebuild; pass --version explicitly (measured: required);
                                                  #   paid plans only per pricing page
lk agent secrets                                  # names only - values can never be read back
lk agent update-secrets --secrets KEY=VAL         # rolling restart; do it when no call is live
lk agent restart                                  # restart pool without interrupting active sessions
```

- `livekit.toml` holds `[project] subdomain` and `[agent] id`; commit it, not secrets.
- Secrets **are** the runtime environment. `.env.local` changes never reach the deployed worker.
- Rolling deploys: new instances must pass the health check (agent HTTP server, default `:8081`) within 5 min; old
  instances drain for up to 1 h, so in-flight calls finish on the old version.
- **Two rollback levers**, fastest first: (1) flip a behaviour secret (e.g. `AGENT_PIPELINE=cascaded`) - same code,
  rolling restart in ~30 s; (2) `lk agent rollback` to the previous version.
- CI: `livekit/deploy-action` GitHub Action (https://github.com/livekit/deploy-action).
- Self-hosted bridge (e.g. Cloud Run / GKE): same ideas - immutable image tags, traffic split for rollback, secrets
  from a secret manager mounted at runtime, min instances >= 1 (section 6), and WebSocket-friendly timeouts
  (`references/07-websocket-media-streams.md`).

**Health lines to grep after every deploy or secret change** (make your agent log them):

```text
live warmup: ok=True location=us ... greeting_cached=True                     # once per worker process
pipeline=realtime live_location=us (configured realtime; warmup=ok greeting_cached=True error=-)   # once per call
warmup=unprobed                                                               # a cold call beat prewarm
```

After re-setting any credential secret, **re-validate end to end** (place a test call that writes a marked test
ticket): a mismatched value silently costs every CRM write and you cannot read the secret back to check.

**Dockerfile** (LiveKit Cloud requires a glibc base, non-root user, fixed `CMD` running `start`; it injects
`LIVEKIT_URL/API_KEY/API_SECRET` itself - https://docs.livekit.io/deploy/agents/builds.md):

```dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN python -m my_agent.agent download-files      # bake Silero / turn-detector / NC model files into the image
USER 10001
ENTRYPOINT ["python", "-m", "my_agent.agent"]
CMD ["start"]
```

`.dockerignore`: `.env*`, `.git`, `.venv`, tests, recordings, docs.

## 3. Pin dependencies exactly

The image above installs from `pyproject.toml`, **not** from the lockfile, so `~=1.7.0` or `>=1.0` lets a rebuild
silently pull a new minor. Measured: `livekit-plugins-google` 1.8.2-1.8.4 reject `vertexai=True` with the short model
name `gemini-3.8-live` (`ValueError`; fixed in 1.8.5+), so an unpinned rebuild of a 1.7.x image that landed on
those versions would have broken every call (`references/02-gemini-live-model-and-config.md` section 2.5).

```toml
dependencies = [
  "livekit-agents[google,silero]==1.7.1",
  "livekit-plugins-google==1.7.1",
  "livekit-plugins-silero==1.7.1",
  "livekit-plugins-noise-cancellation~=0.3.0",
  "google-genai==2.21.0",
]
```

- Pin with `==`, and add a unit test that reads `pyproject.toml` and asserts the pins
  (`references/10-testing-and-evaluation.md` section 2).
- Upgrade deliberately: read the changelog for the realtime plugin, re-run probes/sims/replays, then bump. Avoid
  framework upgrades days before client testing - they move every pipeline at once.
- Any subclassing or monkey-patching of plugin internals (receive taps, resume fixes) is tied to the exact version;
  say so in a comment next to the pin.

## 4. Config validation and readiness checks

Vertex accepts many wrong values until connect time (any location string, any `language_code`), and plugins silently
drop options on the wrong model. Validate yourself, in two tiers:

```python
def pipeline_errors(self) -> list[str]:
    """Realtime tuning and pipeline choice. Logged; the call is answered on the fallback pipeline. Never raises."""
    # AGENT_PIPELINE in {cascaded, realtime}; sensitivities in {"", HIGH, LOW}; ms values non-negative integers
    # (collect parse errors, never raise: a typo must not crash prewarm); location in {us, eu, us-central1};
    # voice in the plugin's voice list; language hint matches [a-z]{2}-[A-Z]{2}; model id starts with "gemini-"

def cloud_errors(self) -> list[str]:
    """Hard requirements. The entrypoint refuses the call if any are present."""
    # project id, LiveKit URL/key/secret present; recording governance flags consistent; fallback model differs
    # from primary; thinking levels valid per model; ...
```

**Readiness CLI** run in CI and before go-live, printing JSON and exiting non-zero when not ready:

```json
{"ready": false, "checks": [
  {"name": "approved_content_loads", "ok": true},
  {"name": "cloud_errors_empty", "ok": true},
  {"name": "pipeline_errors_empty", "ok": false, "detail": "GEMINI_LIVE_LOCATION=asia-south1 not served"},
  {"name": "livekit_url_region_pinned", "ok": true, "detail": "wss://<subdomain>.<region>.rtc.livekit.cloud"},
  {"name": "crm_mode", "ok": true, "detail": "dry_run"},
  {"name": "gcloud_project_matches", "ok": true},
  {"name": "lk_cli_installed", "ok": true}
]}
```

Including `pipeline_errors` in readiness means a deploy cannot pass while the configured pipeline is being ignored.
Pair it with the live provider check (`references/10-testing-and-evaluation.md` section 3).

## 5. Credentials

- **Never in code or images.** Service-account keys, CRM OAuth secrets, carrier auth tokens and LiveKit keys live in
  the platform's secret store. Scan the repo in CI.
- LiveKit Cloud: `--secret-mount ./gcp-sa.json` mounts the file at `/etc/secrets/<name>`; set
  `GOOGLE_APPLICATION_CREDENTIALS` to that path as a secret (https://docs.livekit.io/deploy/agents/secrets.md).
  Defensive variant used in production: at prewarm, search `/etc/secrets`, `/run/secrets`, `/secrets`,
  `/var/secrets`, `/app`, `/tmp` (one level) for a JSON file with `"type": "service_account"`, set
  `GOOGLE_APPLICATION_CREDENTIALS`, log the path; else fall back to ADC. Call it again in the post-call hook. A wrong
  path otherwise fails at the first Google client construction - silence on a connected line.
- Pass the Vertex `project` explicitly; ADC does not always carry one.
- Small latency win: the google-genai SDK refreshes ADC credentials on every `live.connect` when none are passed
  (token fetch measured 123-711 ms); pre-refreshed credentials cut connect from ~1.46 s to ~1.27 s.
- Least privilege: separate service accounts for the agent (Vertex user, Pub/Sub publisher, storage object creator
  on one bucket) and for analytics/dashboards (read-only). Restricted datasets for caller content.
- Webhook/stream authentication for WS providers (Twilio `X-Twilio-Signature`, Plivo V3 signature, etc.):
  `references/07-websocket-media-streams.md`.

## 6. Cold start, warm replicas and prewarm

- **Scale to zero hurts phones.** On LiveKit Cloud's free Build plan production agents scale to zero; a cold start
  adds 10-20 s before the agent joins while the caller hears ringing (docs), observed as "~10 rings". The two slow
  pickups in the measured deployment were exactly the two calls after long idle (347 and 1,360 min). Paid plans keep
  production warm ("cold start prevention"); `lk` has no min-replicas flag (checked in lk 2.18.5; `references/04-livekit-cloud.md`
  section 6). Otherwise place a wake-up call before tests and demos.
- **The prewarm budget is hard.** In livekit-agents 1.7.1, `initialize_process_timeout` is 10 s; past it the
  supervisor kills the process and the pool respawns it forever. No `try/except` helps (the kill is external). Keep
  `setup_fnc` to local work (load the VAD) and put network warm-up on a daemon thread:

```python
def prewarm(proc: JobProcess) -> None:
    resolve_google_credentials()
    settings = Settings.from_env()
    threading.Thread(target=warm_network_caches, args=(proc, settings), daemon=True).start()   # off the init path
    proc.userdata["vad"] = silero.VAD.load(min_speech_duration=0.1, min_silence_duration=0.25,
                                           prefix_padding_duration=0.5, activation_threshold=0.5,
                                           sample_rate=16000, force_cpu=True)

def warm_network_caches(proc, settings):
    try:
        build_ticket_sink(settings)                       # OAuth + CRM schema validation (~2.8 s), once per process
    except Exception as exc:
        LOGGER.error("crm warm-up failed: %s", exc)      # calls still work; post-call builds it on demand
    warmup = asyncio.run(warm_live(settings))             # probe primary then fallback region + render greeting
    proc.userdata["live_warmup"] = warmup                 # publish only when complete
```

  Measured: container start -> worker registration took 11.2 s, 7.4 s of it CRM OAuth + TTS render inside
  `setup_fnc`; a job dispatched 30 ms after registration found the caller already gone.
- **Health probe at prewarm**: one raw Live session per process that (a) proves the model is reachable in the primary
  region, else tries the fallback region, else marks the process to answer on the fallback pipeline ("a greeting
  followed by silence is the worst thing a line can do"); and (b) renders the greeting in the Live voice and
  **verifies the transcript** (>= 70% of the greeting's words, <= 3 extra words) before caching it. Details:
  `references/03-conversation-mechanics.md`.
- Publish caches only when complete, so a call arriving mid-render sees "no cache" and falls back.
- `JobProcess.userdata` outlives a call: key per-call stashes by job id and pop on read. Singletons that both a
  background thread and the post-call hook may build need a lock.

## 7. Observability: events, outbox, Pub/Sub, BigQuery

```text
agent worker --(append, PII-validated)--> local SQLite outbox
  --(post-call hook, export all rows)--> Pub/Sub topic (schema-bound; event_id attribute)
  --> BigQuery subscription --> partitioned raw_event table --> views --> dashboard (IAP-protected)
caller content (redacted transcript rows, extracted insight row) --> separate RESTRICTED dataset, never the outbox
```

- Pub/Sub BigQuery subscriptions write messages straight into a table (https://cloud.google.com/pubsub/docs/bigquery).
  Delivery is at-least-once: dedupe on `event_id` in views.
- **Export the whole outbox at call end** (`limit=1000` in the measured system): the default 100 left a long call's
  rows in a container deleted with the job.
- Point views at `raw_event` directly; typed tables nobody feeds stay empty.
- **Event validation order matters**: exact-key allowlist first; a forbidden-substring backstop (`address, caller,
  comment, email, message, name, phone, pincode, prompt, text, transcript, utterance`) only for keys not on the
  allowlist; string values <= 128 chars, rejected if they contain a >= 10-digit run or an email. Applying the
  substring check to allowlisted keys silently discarded every `transcription_delay_ms` (contains "transcript").
- The emitter swallows rejections into a log line, so add an AST test over every call site.

**Per-call events** (attribute names are a suggestion; keep them stable):

| Event | Key attributes |
|---|---|
| `call_started` | `pipeline` (that actually answered), `release_version`, `live_location`, `warmup_state` |
| `greeting_played` | `prerendered`, `duration_ms`, `interrupted` |
| `user_turn_completed` | `turn_index`, `language` (only if a real call showed it populated) |
| `turn_latency` | `pipeline`, `e2e_latency_ms`, `after_server_vad_ms` |
| `provider_error` | `error_type`, `provider`, `model`, `layer` (realtime/stt/llm/tts), `recoverable`, `event_stage` |
| `silence` | `event_stage` (check_in / final_warning / close) |
| `call_closing` / `call_close_cancelled` | `close_reason`, `decided_by` |
| `call_ended` | `duration_s`, `ended_by` |
| `model_usage` | `usage_kind`, `input_tokens`, `input_cached_tokens`, `output_tokens`, `audio_seconds` |
| `call_summary` | `route`, `ticket_status`, `contained`, `handed_off`, `missing_slots_count`, `qa_passed`, `assignee_resolved` |
| `qa_result` | `dimension`, `passed`, `reason_code` |

Notes from production:
- `call_started` is emitted after `ctx.connect()`, so INVITE, ringing and worker start are invisible; that is why
  `greeting_played.prerendered` and idle-before-call matter.
- On the realtime path `on_user_turn_completed` never fires: emit user turns from final `user_input_transcribed`
  events, or every realtime call shows zero caller turns.
- Emit **provider errors in their own right** (`session.on("error")`): latency only covers turns that produced audio.
- `assignee_resolved` must come from the CRM's response, not "a ticket exists".
- Stash per-call facts (caller number, pipeline) in `proc.userdata` keyed by job id **during** the call: with
  `delete_room_on_close=True` the SIP participant is gone by the time the post-call hook runs.

## 8. Recordings and transcripts

- LiveKit `session.start(record=...)` enables Agent Observability's RecorderIO: stereo, **caller ch0, agent ch1**,
  works on realtime. Cloud retention is 30 days and may be processed in the US. Egress to your own bucket is the
  alternative (`references/04-livekit-cloud.md`).
- **Fail closed on governance**: record only when audio recording, transcript storage and governance approval flags
  are all set; otherwise `record=False`.
- The file lives inside the job container and dies with it - upload in the post-call hook. The report's
  `audio_recording_path` came back **empty** on simulations and real calls (1.7.1); RecorderIO wrote
  `ctx.session_directory / "audio.ogg"`. Try both, log both:

```python
for candidate in (report.get("audio_recording_path"), ctx.session_directory / "audio.ogg"):
    if candidate and Path(candidate).exists() and Path(candidate).stat().st_size > 0:   # skip zero-byte files
        uri = await asyncio.to_thread(store.upload, candidate, session_id=ctx.job.id,
                                      started_at=report.get("audio_recording_started_at") or report.get("started_at"))
        if uri:
            break
```

- Object names `YYYY/MM/DD/<session_id>.ogg` partitioned by the **business timezone's** date (UTC puts late-night
  calls under the wrong day); parse both float epoch and ISO timestamps (ISO-only put every upload under
  `unknown-date/`); set `content_type="audio/ogg"` or browsers refuse to play it.
- Realtime redaction in LiveKit recordings is inaccurate (LiveKit warns realtime user turns lack complete speech
  timestamps); your bucket copy is unredacted - a governance decision, not a technical one.
- Transcript rows: one row per turn (session id, turn index, speaker, redacted text, timestamps) in the restricted
  dataset. Strip Live's silence placeholders first: `re.compile(r"<no speech>|\{pause\}", re.I)`. Agent turns come
  from lossy output transcription (2/12 empty, 1-2/12 truncated in probes); for trusted QA, re-transcribe the
  recording post-call.

## 9. Post-call pipeline

```python
@server.rtc_session(agent_name="my-agent", on_session_end=post_call)
async def entrypoint(ctx: JobContext) -> None: ...

async def post_call(ctx: JobContext) -> None:
    report_obj = ctx.make_session_report()
    report = report_obj.to_dict()                       # chat_history, metrics, audio path
    usage = model_usage(report_obj.model_usage)         # typed object: to_dict() drops the usage `type`
                                                        # discriminator (exclude_defaults) -> every call cost 0
    caller = ctx.proc.userdata.pop(f"caller_number:{ctx.job.id}", None)
    result = await stage("ticket", asyncio.to_thread(processor.process, report, session_id=ctx.job.id,
                                                     caller_number=caller))
    await stage("qa", asyncio.to_thread(evaluate_session, report))    # scored even if ticketing failed
    await stage("transcripts", ...)                                   # restricted dataset
    await stage("recording", ...)                                     # bucket upload (section 8)
    await stage("analytics", asyncio.to_thread(outbox.export, limit=1000))
```

- Each `stage` catches and logs its own failure: a CRM outage must not lose the recording, analytics or QA. QA is
  scored separately because "the calls that fail are the ones most needing a score".
- The hook has a hard budget (observed **300 s** in livekit-agents 1.7.1: `on_session_end timed out after 300s`, the
  process was killed and the call lost ticket, transcript and analytics). Give every network call an explicit timeout
  and size retries to fit (section 10). VERIFY the budget on your version.
- Order by value: ticket first, then transcripts/recording, then analytics.

## 10. Post-call extraction with a stronger LLM

After hang-up, latency costs nobody: use the strongest text model available, JSON mode with a response schema,
explicit thinking level, and `max_output_tokens` around 1024.

**Schema shape** (enums come from the CRM's live option names, section 11):

```json
{
  "type": "OBJECT",
  "properties": {
    "route":            {"type": "STRING", "enum": ["complaint", "query", "feedback", "request", "out_of_scope"]},
    "scenario_id":      {"type": "STRING", "enum": ["<allowed scenario ids>"]},
    "language":         {"type": "STRING", "enum": ["en", "hi", "kn", "mixed"]},
    "priority":         {"type": "STRING", "enum": ["low", "normal", "high", "urgent"]},
    "ticket_required":  {"type": "BOOLEAN"},
    "caller_confirmed": {"type": "BOOLEAN"},
    "confidence":       {"type": "NUMBER"},
    "fields": {"type": "OBJECT", "properties": {
        "name":           {"type": "STRING"},
        "callback_phone": {"type": "STRING"},
        "postcode":       {"type": "STRING"},
        "product":        {"type": "STRING", "enum": ["<CRM option NAMES for the product dropdown>"]},
        "concern_type":   {"type": "STRING", "enum": ["<CRM option NAMES>"]},
        "sample_retained":{"type": "STRING", "enum": ["yes", "no"]}
    }},
    "evidence_turn_ids": {"type": "ARRAY", "items": {"type": "STRING"}}
  },
  "required": ["route", "scenario_id", "ticket_required", "confidence", "evidence_turn_ids"]
}
```

Keep schemas flat; give enums of **option names** (what the model understands) and map to CRM tags in code.

**Prompt rules that fixed real data errors:**

- Record **only facts the caller stated**. Boolean-ish fields (`sample_retained`, `resolution_accepted`) only on an
  explicit statement - they were invented in 3/6 replays when inference was allowed.
- When caller and agent disagree (the agent misheard or substituted), record what the **caller** said.
- Omit a value the agent said it could not note.
- Phone and postcode must have the right digit count, or be omitted. Don't normalise spoken street names.
- Put **today's date** (business timezone) in the prompt; parse dates day-first where that is local practice.
  Without it an expired product was filed as a different complaint category.
- Transcripts are mixed-script (Devanagari and romanised in one call); say so.

**Validate the output** in code: `scenario_id` in the allowed set; `0 <= confidence <= 1`; every
`evidence_turn_ids` entry is an existing **user** turn id; an empty body raises with the `finish_reason` (parsing ""
as `{}` died with a bare `KeyError`).

**Timeouts and retries**: `http_options=types.HttpOptions(timeout=60_000)` per attempt (20 s was too tight for a
43-turn call; no timeout hung the hook until it was killed). 3 attempts x 60 s with jittered exponential backoff
(1.5 s base) ~ 190 s worst case, inside the 300 s hook budget. Retry only transport/capacity errors (408, 429, 500,
502, 503, 504, RESOURCE_EXHAUSTED, UNAVAILABLE, DEADLINE_EXCEEDED, INTERNAL); **never** retry a schema violation
(it hides a defect). 429s under concurrent post-call load had cost 6/21 tickets before retries.

**Deterministic policy pass afterwards may only escalate** (e.g. any safety keyword -> urgent), never downgrade a
safety route; log rule/model disagreements (`safety_recall_gap`) without changing routing.

**Failure fallback = file it unclassified, never lose it**: if extraction fails, still file a ticket from the
transcript with placeholder classification, mark `extraction_failed=true` (tag + event) so dashboards and alerts see
it, and let a human triage.

## 11. CRM / ticketing integration (Zendesk-shaped)

Lessons from a Zendesk integration; the same traps exist in most CRMs with picklists.

- **Dropdown fields accept the option's tag/value, not its display label, and silently drop anything else.**
  In Zendesk, a drop-down ("tagger") custom field's value is the option's `value` tag
  (https://developer.zendesk.com/api-reference/ticketing/tickets/ticket_fields/). Every voice ticket lost its
  category until this was fixed. The fix:
  1. Dump the live instance's ticket fields, groups and forms (read-only script below).
  2. Build value maps `{option name -> tag}` per field from that dump, checked into the repo.
  3. Give the extractor the **option names as enums**; map name -> tag in the sink.
  4. Any value with no matching option goes into the internal note ("Not set (no matching option): <field>=<value>")
     instead of vanishing.
  5. A test fails if a category the system can file has no option.

```python
# read-only: dump fields/options/groups/forms (paginate via next_page)
with httpx.Client(base_url=f"https://{SUBDOMAIN}.zendesk.com/api/v2", headers=auth, timeout=30) as c:
    for key, path in [("ticket_fields", "/ticket_fields.json"), ("groups", "/groups.json"),
                      ("ticket_forms", "/ticket_forms.json")]:
        url, items = path, []
        while url:
            j = c.get(url).raise_for_status().json()
            items += j.get(key, [])
            url = j.get("next_page")
        meta[key] = items
value_maps = {f["title"]: {o["name"]: o["value"] for o in f.get("custom_field_options") or []}
              for f in meta["ticket_fields"]}
```

- **Validate the schema once per process at prewarm** (fields, groups, forms, brands exist; every value-map tag is
  among the field's current options), not per call: per call it is several API calls, and in production it pushed
  submission past job teardown, so tickets were silently never created while dry-run looked fine.
- **Idempotency**: one external id per call (the session id). Before creating, look up by `external_id`
  (`GET /api/v2/tickets?external_id=...`) and **update** if it exists (enrichment, reconciliation after an uncertain
  write); also send an `Idempotency-Key` header on create (Zendesk supports it on ticket creation; VERIFY the key
  expiry window in current docs). Retry 408/409/429/5xx honouring `Retry-After` with capped backoff.
- On an assignee rejection, retry immediately without the assignee and say why in the internal note.
- **Two allowlists drift**: the extractor's field list and the sink's semantic-field -> field-id mapping. A field
  added to one only raised `Unknown semantic fields` and lost 6 of 8 tickets. Keep one list, and test every
  extractable field end to end through every guard. Fields with no CRM field go into the comment as "Additional
  collected details".
- Choose the ticket form by type (query vs complaint) through the value map.
- **Dry-run sink by default**; enable writes with one secret.
- **Never lose a ticket**: on final failure persist the full ticket payload (outbox row or dead-letter bucket) with
  the external id, alert, and replay later; the external id makes replay safe.
- **Read-back verification with a marked test ticket**: before go-live and after any mapping or credential change,
  file one clearly fake ticket ("TEST - field check, please ignore", fake PII, a dedicated tag), read it back by id,
  and diff sent fields vs landed custom fields; print "sent but empty". Get the CRM owner's approval before writing
  to a shared instance, and close the test ticket afterwards.

```python
t = client.get(f"/tickets/{ticket_id}.json").json()["ticket"]
landed = {names[f["id"]]: f["value"] for f in t["custom_fields"] if f["id"] in names and f["value"] not in (None, "", [])}
print("sent but empty in CRM:", sorted(set(sent_fields) - set(landed)))
```

## 12. Security and PII

- **Data residency**: Gemini Live 3.8 is served from `us`, `eu` and `us-central1` only (no India region), so caller
  audio is processed abroad; LiveKit Cloud observability may process in the US. Raise this explicitly with the data
  owner (e.g. India's DPDP Act; `references/12-india-telephony-compliance.md`).
- **Disclosure**: the agent says it is automated and never claims to be human; announce recording where required.
- **PII classes and stores**: caller content (audio, transcripts, extracted fields, caller number) only in restricted
  stores with their own IAM; analytics events are PII-free by validation (section 7). Mask numbers in logs
  (`***1234`).
- **Redaction**: redact transcripts before they reach broad datasets (numbers, emails, addresses); keep the
  unredacted recording only where governance allows, with retention set on the bucket (lifecycle rule).
- **Secrets**: never in code, images, prompts or logs; rotate on staff changes; validate end to end after rotation.
- **Prompt content**: no secrets or internal URLs in the system prompt - assume a caller can get the model to repeat
  parts of it.
- **Ingress auth**: SIP trunks with digest auth or allowed numbers/IPs and TLS/SRTP where available
  (`references/06-sip-trunking-providers.md`); WS streams with signature validation
  (`references/07-websocket-media-streams.md`); carrier fraud alerts and call-rate limits on.
- **Tool safety**: every side effect (transfer, ticket, hang-up) is a tool with flat, validated arguments; never let
  free text from the caller reach an API unchecked.

## 13. Cost monitoring

**Gemini Live is billed on the whole context every turn.** Vertex pricing: "Tokens from past turns are re-processed
and billed in every new turn", and context caching is not supported for 3.8 (`cached_content_token_count` came back
null). Prices on 2026-10-21 (Vertex PayGo and Gemini API paid tier, per 1M tokens): input text $0.75, input audio
$3.00, output text (incl. transcription) $4.50, output audio $12.00 (https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing,
https://ai.google.dev/gemini-api/docs/pricing). Output audio runs ~25 tokens/s of speech.

Per-call estimate:

```text
text_in   = sum over turns of (system prompt tokens + accumulated text context)        x $0.75/M
audio_in  = sum over turns of (accumulated caller+agent audio tokens in context)       x $3.00/M
audio_out = agent speech seconds x ~25 tokens/s                                        x $12.00/M
text_out  = transcription + thinking tokens                                            x $4.50/M
+ LiveKit agent minutes (~$0.01/min after plan allowance) + SIP minutes (~$0.003-0.004/min) + carrier per-minute
```

Example: a 20k-token prompt over 30 turns is 600k text-input tokens, ~$0.45 per call for the prompt alone. Measured
`input_tokens` per turn grew 61 -> 456 -> 719 -> 823 -> 1256 over five turns with a small prompt; ~21k text tokens per
turn with an 88k-character prompt. Cutting that prompt to ~38k characters roughly halved per-turn input.

Controls: keep the prompt lean (it also costs latency), enable `context_window_compression` with a modest target
(64k used), cap call duration (900 s used), and keep streamed silence (10 min of silence cost ~120-145 tokens - cheap).

Monitoring: emit `model_usage` per call from the **typed** usage object (the dict form reported zero cost for every
call); keep the audio/text split; chart cost per call and per contained call; alert on p95 cost per call and on
daily spend. Check LiveKit and carrier invoices monthly against your minute counts.

## 14. SLOs and alerts to start with

| SLO | Starting target |
|---|---|
| Call start success | >= 99.5% per 15 min |
| P95 first audio after caller stops (`turn_latency.e2e_latency_ms`) | < 1.8 s; alert if > 2.5 s for 15 min |
| Runtime errors (`provider_error` non-recoverable) | < 0.5% of calls |
| Ticket success on complaint calls | >= 99% |
| Analytics outbox oldest undelivered | < 5 min |
| Greeting prerendered | ~100% outside deploy windows (a drop means prewarm is failing) |
| `extraction_failed` tickets | alert on any |

Measured realtime reference points: end of speech -> first audio 1.2-1.6 s typical (occasional 2-3.5 s);
mouth-to-ear estimate 1.6-2.2 s with an India agent and US model endpoint.
