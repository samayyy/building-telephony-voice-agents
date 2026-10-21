# SIP templates (LiveKit Cloud or self-hosted)

Copy-and-edit JSON for the LiveKit side of a SIP trunk, plus an outbound-call script. The carrier side (Plivo, Twilio, Exotel, Telnyx, operator SBCs) is in `references/06-sip-trunking-providers.md`. Concepts and field meanings are in `references/04-livekit-cloud.md` sections 7-10. All three JSON files were parsed successfully into `CreateSIPInboundTrunkRequest`, `CreateSIPDispatchRuleRequest` and `CreateSIPOutboundTrunkRequest` with livekit-api 1.2.2 (protobuf JSON accepts snake_case or camelCase field names). They were not applied to a live project for this skill.

| File | What it creates |
|---|---|
| `inbound_trunk.json` | Inbound trunk that accepts calls to your DID(s) |
| `dispatch_rule.json` | One room per caller, explicit dispatch of `my-agent` |
| `outbound_trunk.json` | Outbound trunk to the carrier (credentials passed on the command line) |
| `create_participant_example.py` | Places an outbound call, via the agent or directly |

JSON cannot hold comments, so the notes are here.

## 1. Point lk at the project

```bash
# LiveKit Cloud
lk cloud auth && lk project set-default "<project-name>"
# Self-hosted
lk project add selfhost --url https://livekit.example.com --api-key APIxxxxxxxxxxxx --api-secret ... --default
```

## 2. Inbound

1. Edit `inbound_trunk.json`:
   - `numbers`: your DIDs in E.164 with `+`, exactly as the carrier sends them in the To/Request-URI. Several numbers can share one trunk.
   - Auth: number matching alone is fine when `numbers` is set. With `numbers: []` you must add `auth_username`/`auth_password` or `allowed_addresses` (on Cloud, `allowed_addresses` must be enabled by LiveKit support). Twilio Elastic SIP cannot send digest auth on origination.
   - `krisp_enabled`: leave `false` when the agent applies BVCTelephony. Use one noise-cancellation method only.
   - `headers_to_attributes`: maps carrier `X-*` headers (e.g. `X-Exotel-CallSid`) to participant attributes. They arrive asynchronously.
   - `ringing_timeout`: how long the caller rings waiting for the agent before `486 Busy Here`. Cold starts on the Build plan take 10-20 s, so do not set it too short.
2. Create it and note the `ST_...` ID:
   ```bash
   lk sip inbound create inbound_trunk.json
   lk sip inbound list
   ```
3. Edit `dispatch_rule.json`:
   - `trunk_ids`: the `ST_...` from step 2 (omit the field to match all inbound trunks).
   - `room_config.agents[].agent_name`: must equal the worker's `@server.rtc_session(agent_name=...)` / `AGENT_NAME`.
   - `hide_phone_number: false` lets the agent read the caller's number (`sip.phoneNumber`). The number also appears in the room name (`call-<number>_<random>`); set `true` if that is not acceptable and have the agent ask.
   - `metadata` reaches the agent as `ctx.job.metadata`; `attributes` are inherited by the SIP participant.
   - Optional `"deployment": "staging"` inside the agent entry targets a non-production deployment.
4. Create it:
   ```bash
   lk sip dispatch create dispatch_rule.json
   lk sip dispatch list
   ```
   There is no `--agent` flag; the agent must be in the JSON.
5. Point the carrier's origination URI at LiveKit:
   - Cloud: `sip:<subdomain>.sip.livekit.cloud;transport=tcp`. Region-pinned for India: `sip:<subdomain>.india.sip.livekit.cloud;transport=tcp` (LiveKit's Plivo guide says India numbers fail without it). TLS: `;transport=tls` on port 5061.
   - Self-hosted: `sip:<public-ip-or-dns>:5060;transport=tcp`.
   - `<subdomain>` is the project ID without `p_` (`lk project list --json`).

Updating:
```bash
lk sip inbound update --id ST_xxx --numbers +91XXXXXXXXXX     # field-level update
lk sip dispatch update --id SDR_xxx --trunks ST_xxx           # field-level update
# Passing a JSON file to `update` REPLACES the whole object; re-include every field.
lk sip inbound delete ST_xxx ; lk sip dispatch delete SDR_xxx
```

## 3. Outbound

1. Edit `outbound_trunk.json`:
   - `address`: the carrier's SIP host or IP with optional `:port`, not a SIP URI (Twilio `<trunk>.pstn.twilio.com`, Plivo `<id>.zt.plivo.com`, Exotel the edge IP:port Exotel assigns).
   - `numbers`: caller IDs this trunk may present (or `["*"]` and pass `sip_number` per call).
   - `transport`: `SIP_TRANSPORT_AUTO|UDP|TCP|TLS`. Set it explicitly for carriers without UDP (Exotel: TCP 5070 or TLS 443).
   - `destination_country`: ISO 3166-1 alpha-2 for outbound region pinning on Cloud (docs show both `US` and `in`). `IN` keeps Indian calls in the India region.
   - `media_encryption`: `SIP_MEDIA_ENCRYPT_ALLOW` or `REQUIRE` with TLS trunks.
2. Create it with credentials on the command line (keeps secrets out of the file):
   ```bash
   lk sip outbound create outbound_trunk.json --auth-user "$SIP_USER" --auth-pass "$SIP_PASS"
   lk sip outbound list
   ```
3. Set `SIP_OUTBOUND_TRUNK_ID=ST_...` for the agent and/or the script.
4. Place a call:
   ```bash
   # Recommended: the agent dials (handles busy/no answer, talks once the callee says hello)
   lk dispatch create --new-room --agent-name my-agent --metadata '{"phone_number": "+91XXXXXXXXXX"}'
   python create_participant_example.py +91XXXXXXXXXX --agent my-agent
   # Backend dials, agent joins after answer
   python create_participant_example.py +91XXXXXXXXXX --agent my-agent --mode direct --caller-id +91YYYYYYYYYY
   # Raw CLI dial (no agent)
   lk sip participant create participant.json
   ```

Outcomes: 486/603 -> `SipCallError` (`USER_REJECTED`); 408/480 -> `USER_UNAVAILABLE`; 5xx -> `SIP_TRUNK_FAILURE`. The last two do not auto-close an agent session, so call `ctx.shutdown()`. Voicemail answers with 200 OK; use answering-machine detection (`livekit.agents.AMD`). `ringing_timeout` is at most 80 s. India outbound rules (A2P declaration, 140/160 series, consent, DND, calling hours): `references/12-india-telephony-compliance.md`.

## 4. Transfers and DTMF

```bash
lk room participants list call-xyz        # find the SIP participant's identity
lk sip participant transfer --room call-xyz --identity <sip-participant-identity> --to tel:+91ZZZZZZZZZZ
```
The carrier must allow SIP REFER (Twilio: trunk "Call Transfer (SIP REFER)" + "Enable PSTN Transfer", or `twilio api trunking v1 trunks update --sid TKxxx --transfer-mode enable-all`). Plivo wants `sip:+E164@<id>.zt.plivo.com` as the target. In the agent, set `TRANSFER_TO` to enable the `transfer_to_human` tool.

## 5. Acceptance call

1. Call the DID. The carrier's logs should show the call reaching LiveKit.
2. `lk room list` shows `call-...`; the dashboard (Cloud) shows the SIP participant and the agent.
3. The greeting plays; you can interrupt it; the agent answers.
4. Say goodbye; the agent ends the call and the carrier shows a normal hang-up.
5. Hang up first on a second call; the room disappears.
6. Failure before LiveKit: carrier logs first (number not linked, trial expired, wrong URI or transport). LiveKit received it but no agent: dispatch rule trunk and `agent_name`, then worker logs (`lk agent logs`).
