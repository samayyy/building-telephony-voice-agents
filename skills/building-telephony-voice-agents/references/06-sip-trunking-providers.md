# 06 — SIP trunking providers (carrier → LiveKit SIP → Gemini Live agent)

Purpose: get a phone number's calls into a LiveKit room (and place calls out) over a SIP trunk, for each common
provider, and debug it when it fails. It covers just enough SIP to make sensible choices: codecs, transports,
SRTP/TLS, digest vs IP auth, REFER, and DTMF. It also covers the LiveKit trunk and dispatch objects, with step-by-step
setups for Plivo Zentrunk, Twilio Elastic SIP, Exotel ExoTrunk/vSIP, Telnyx and Vonage. Indian enterprise carriers
(Airtel, Tata, Jio) and register-only trunks go through a PBX bridge (Asterisk/FreeSWITCH). Then come outbound
calling, transfer, DTMF, a test checklist, and how to tell whether a provider does SIP at all.
For WebSocket media streams (no SIP), see `references/07-websocket-media-streams.md`. For LiveKit Cloud and
self-hosted specifics (plans, deployment, `livekit/sip` server), see `references/04-livekit-cloud.md` and
`references/05-livekit-self-hosted.md`. For India rules (media anchoring, number series), see
`references/12-india-telephony-compliance.md`.

Verified on 2026-10-21 against the LiveKit docs (served as `.md`), livekit-api 1.2.2 / livekit-protocol 1.1.27
(livekit-agents 1.8.8 current, 1.7.1 in production), `lk` CLI 2.18.5, and the provider docs linked inline.
**VERIFY** marks items that are not confirmed or where the sources disagree.

## Contents
1. [SIP basics a voice-agent builder needs](#1-sip-basics-a-voice-agent-builder-needs)
2. [LiveKit side: inbound trunk, dispatch rule, outbound trunk](#2-livekit-side)
3. [Plivo Zentrunk](#3-plivo-zentrunk)
4. [Twilio Elastic SIP Trunking (+ `<Dial><Sip>` alternative)](#4-twilio-elastic-sip-trunking)
5. [Exotel ExoTrunk / vSIP](#5-exotel-exotrunk--vsip)
6. [Telnyx](#6-telnyx)
7. [Vonage](#7-vonage-sip-trunking)
8. [Airtel IQ / Tata / Jio / Vi enterprise SIP](#8-indian-enterprise-carriers-airtel-tata-jio-vi)
9. [Generic PBX bridge (Asterisk / FreeSWITCH)](#9-generic-pbx-bridge-asterisk--freeswitch)
10. [Outbound calling](#10-outbound-calling)
11. [Transfer (REFER) and DTMF](#11-transfer-refer-and-dtmf)
12. [Testing and debugging checklist](#12-testing-and-debugging-checklist)
13. [How to tell whether a provider supports SIP trunking](#13-how-to-tell-whether-a-provider-supports-sip-trunking)

---

## 1. SIP basics a voice-agent builder needs

**Signalling vs media.** SIP (INVITE / 100 / 180 / 200 / ACK / BYE, plus REFER and OPTIONS) sets up the call. RTP
carries the audio as separate UDP packets, at addresses and ports negotiated in the SDP. Many "it rings but there is
no audio" bugs happen because signalling works while RTP is blocked or NATed wrongly.

**Codecs** (negotiated in SDP; G.711 is the common denominator):

| Codec | SDP name / PT | Rate | Notes for a Gemini agent |
|---|---|---|---|
| G.711 µ-law | `PCMU/8000`, PT 0 | 8 kHz narrowband, 64 kbps | Default in North America/Japan. Every carrier supports it. |
| G.711 A-law | `PCMA/8000`, PT 8 | 8 kHz narrowband | Default in India/Europe. Exotel recommends PCMA as primary. |
| G.722 | `G722/8000` (clock rate is 8000 in SDP for historic reasons; audio is 16 kHz) | 16 kHz wideband | On by default in LiveKit. **Twilio Elastic SIP and Plivo Zentrunk do not offer it**, and the PSTN is G.711 anyway. It only helps SIP-to-SIP (PBX, softphone). |
| AMR-WB | `AMR-WB/16000` | 16 kHz | Opt-in in LiveKit (`media.codecs`). Mobile-network codec. |
| Opus | `opus/48000/2` | up to 48 kHz | **Not in LiveKit's SIP codec list.** Telnyx release notes claim Opus works to LiveKit with SRTP, but a community post says it is not wired up. Twilio lists Opus as "Limited Availability … not supported end to end". **VERIFY** before promising Opus over SIP. |
| `telephone-event/8000` | dynamic PT (often 101) | — | RFC 4733 DTMF. Always keep it in the offer. |

Source for LiveKit codecs: <https://docs.livekit.io/reference/telephony/codecs-negotiation.md>. The defaults are PCMU,
PCMA and G722, and `SIPCodec.name` also accepts `AMR-WB`. `media.codecs` *adds* to the defaults unless
`only_listed_codecs: true`. Precedence is dispatch rule > inbound trunk > defaults (inbound), and
`CreateSIPParticipant` > outbound trunk > defaults (outbound).
**Practical rule:** leave codecs at the defaults. Use `only_listed_codecs` only when a carrier mis-negotiates. In a
production deployment on Plivo Zentrunk (G.711 only), codec settings were left untouched and the call negotiated
G.711.
Gemini's 24 kHz voice is band-limited to about 3.4 kHz before the caller hears it; see
`references/08-audio-formats-and-conversion.md` §10.

**Transports.** SIP runs over UDP 5060, TCP 5060, or TLS 5061.
- LiveKit supports all three. Every LiveKit provider guide appends `;transport=tcp` to the origination URI;
  forgetting it is a listed common failure.
- **Exotel does not support UDP** (TCP 5070, TLS 443).
- Twilio originates over UDP by default unless the URI says `;transport=tcp|tls`.
- Prefer TCP or TLS. Large INVITEs with many headers fragment over UDP.
- LiveKit outbound trunk `transport`: `SIP_TRANSPORT_AUTO` (0), `UDP` (1), `TCP` (2), `TLS` (3).

**Secure trunking (TLS + SRTP).** TLS encrypts signalling and SRTP encrypts media. They are configured separately but
usually enabled together.
- LiveKit: `media_encryption` = `SIP_MEDIA_ENCRYPT_DISABLE` (default) | `ALLOW` | `REQUIRE`. The top-level field is
  deprecated in favour of `media.encryption`, but the docs still show it.
  Source: <https://docs.livekit.io/telephony/features/secure-trunking.md>
- Twilio Secure Trunking rejects any unencrypted call once enabled. It offers only `AES_CM_128_HMAC_SHA1_80` on
  origination and does not support the `sips:` scheme (use `sip:` + `;transport=tls`).
- Exotel's TLS on 443 uses `RTP/SAVP` with `a=crypto:1 AES_CM_128_HMAC_SHA1_80`.
- **Rule: both ends must agree.** If the provider trunk is "secure", the LiveKit trunk needs TLS transport plus
  `ALLOW`/`REQUIRE`. A mismatch shows up as a 488 Not Acceptable Here or one-way/no audio.

**Authentication.** There are two directions, and they are configured independently.

| Direction | Who authenticates whom | Options |
|---|---|---|
| Inbound (carrier → LiveKit) | LiveKit checks the carrier | Number match (`numbers`), digest (`auth_username`/`auth_password`; the carrier must answer the 401/407 challenge), IP allow-list (`allowed_addresses`; **on Cloud this must be enabled by LiveKit support**), `allowed_numbers` (caller filter). A trunk with empty `numbers` **must** have digest or `allowed_addresses`. |
| Outbound (LiveKit → carrier) | Carrier checks LiveKit | Digest credentials (recommended: LiveKit Cloud egress IPs are not a small fixed list) or the carrier's IP ACL. LiveKit publishes static ranges `143.223.88.0/21`, `161.115.160.0/19`, `153.57.128.0/18` for CA/EU/India/Japan/US (<https://docs.livekit.io/deploy/admin/regions/endpoints.md>). |

- **Twilio Elastic SIP cannot send digest on origination.** Use number matching (or the `<Dial><Sip username password>`
  path, §4.3).
- If both an ACL and credentials are configured on Twilio termination, **both** are enforced.

**No SIP REGISTER in LiveKit.** LiveKit SIP cannot register to a provider as a user agent (also no SIPREC, no video).
Source: <https://docs.livekit.io/telephony.md>.
- If a carrier only offers *registration-based* trunks (the PBX logs in with a username and password and the carrier
  sends calls to wherever it registered), put Asterisk or FreeSWITCH in front: the PBX registers to the carrier and
  peers with LiveKit by digest or IP (§9).
- This is common with Indian PBX-style trunks and some SMB VoIP providers.

**Media timeouts.** LiveKit drops the call if no RTP arrives within 30 s, or within 15 s after the first packet
(`media.media_timeout`). A "call drops at ~30 s" bug is RTP not arriving (NAT or firewall), not a SIP problem.
Source: <https://docs.livekit.io/reference/telephony/sip-primer.md>.

---

## 2. LiveKit side

Full LiveKit detail (Cloud plans, `lk` CLI auth, self-hosted `livekit/sip`) lives in `references/04-livekit-cloud.md`
and `references/05-livekit-self-hosted.md`. Template JSON files: `templates/sip/`. This is the minimum per provider.

**SIP URI / endpoint.**
- The project URI is `sip:<subdomain>.sip.livekit.cloud`, where the subdomain is the project ID without `p_`
  (`lk project list --json`). The "SIP endpoint" is the same value without `sip:`.
- Region-pinned endpoints use `<subdomain>.<region>.sip.livekit.cloud`. Outbound pinning is set with
  `destination_country` (ISO-2, `"in"`).
- Region lists differ between pages (`aus, eu, india, japan, sa, uk, us` vs `eu, india, sa, us, japan, aus, canada`);
  **VERIFY** on the region-pinning page.
- **Region pinning is a Scale-plan (and above) feature on LiveKit Cloud** (pricing page, 2026-10-21; **VERIFY** for
  your plan). Indian numbers on Plivo *require* India pinning (§3). Self-hosted `livekit/sip` is simply wherever you
  run it.
- Sources: <https://docs.livekit.io/telephony/start/sip-trunk-setup.md>,
  <https://docs.livekit.io/telephony/features/region-pinning.md>

**Inbound trunk** (`lk sip inbound create inbound-trunk.json`; the CLI file wraps the body in `{"trunk": {...}}`):
```json
{
  "trunk": {
    "name": "in-+91XXXXXXXXXX",
    "numbers": ["+91XXXXXXXXXX"],
    "headers_to_attributes": { "X-Exotel-CallSid": "exotel.call_sid" },
    "media_encryption": "SIP_MEDIA_ENCRYPT_ALLOW"
  }
}
```
- Fields (protobuf `SIPInboundTrunkInfo`): `numbers, allowed_addresses, allowed_numbers, auth_username, auth_password,
  auth_realm, headers, headers_to_attributes, attributes_to_headers, include_headers, ringing_timeout,
  max_call_duration, krisp_enabled, media_encryption, media, metadata`.
- **`numbers` must equal exactly what the carrier puts in the To / Request-URI user part**, usually `+E.164`. A
  mismatch gives 404/503. Telnyx needs "Destination Number Format = +E.164"; an Exotel 503 was fixed by cleaning up
  `numbers` (<https://community.livekit.io/t/inbound-sip-calls-from-exotel-not-reaching-agent-503-response/468>).
- `krisp_enabled` is trunk-side noise cancellation. When an agent handles the call, prefer agent-side `BVCTelephony`,
  and never run both ("only use one noise cancellation method").
- Passing a JSON file to `lk sip inbound update` **replaces** the whole trunk. Use flags for single-field edits.

**Dispatch rule** (`lk sip dispatch create dispatch-rule.json`). There is no `--agent` flag; put `room_config.agents`
in the JSON:
```json
{
  "dispatch_rule": {
    "name": "inbound-to-my-agent",
    "trunk_ids": ["ST_xxxxxxxx"],
    "rule": { "dispatch_rule_individual": { "room_prefix": "call-" } },
    "hide_phone_number": false,
    "room_config": { "agents": [ { "agent_name": "my-agent", "metadata": "{\"flow\":\"inbound\"}" } ] }
  }
}
```
- `dispatch_rule_individual` gives one room per call, which is what you want for an agent. `dispatch_rule_direct`
  sends everyone to one room; `dispatch_rule_callee` names the room after the dialled number.
- `agent_name` must equal the worker's registered agent name (explicit dispatch). A rule pointing at a retired agent
  name reaches nothing, and the caller hears ringing until `ringing_timeout`.
- `hide_phone_number: true` removes `sip.phoneNumber` from participant attributes, so the agent can't read the caller
  ID.
- In the agent, read `sip.phoneNumber` (caller), `sip.trunkPhoneNumber` (the DID dialled; not the caller), and
  `sip.callID`. Custom X-headers arrive asynchronously as attributes.

**Outbound trunk** (`lk sip outbound create outbound-trunk.json`):
```json
{
  "trunk": {
    "name": "out-provider",
    "address": "<provider-termination-host>",
    "numbers": ["+91XXXXXXXXXX"],
    "transport": "SIP_TRANSPORT_TCP",
    "auth_username": "<user>",
    "auth_password": "<pass>",
    "media_encryption": "SIP_MEDIA_ENCRYPT_DISABLE",
    "destination_country": "in"
  }
}
```
- `address` is a host or IP (optionally `:port`), **not** a SIP URI.
- `numbers` are the caller IDs you are allowed to present; the carrier enforces them.
- Other fields: `headers, headers_to_attributes, attributes_to_headers, include_headers, from_host, metadata`.
- The API reference lists `destination_country` as required, but older examples omit it. **VERIFY** for your CLI
  version.

---

## 3. Plivo Zentrunk

Source: <https://docs.livekit.io/sip/quickstarts/configuring-plivo-trunk.md>. API base
`https://api.plivo.com/v1/Account/$PLIVO_AUTH_ID/`, Basic auth with Auth ID : Auth Token.
Codecs: **G.711 PCMU/PCMA only** (no G.722, Opus or AMR-WB).

**Inbound (PSTN → LiveKit)**
1. Buy an Indian number on an **India data-region account** (KYC; the region can't be changed later). A trial
   account that lapses makes callers hear **busy** at the carrier, and the call never reaches LiveKit.
2. Create the origination URI:
   ```bash
   curl -u "$PLIVO_AUTH_ID:$PLIVO_AUTH_TOKEN" -H 'Content-Type: application/json' \
     "https://api.plivo.com/v1/Account/$PLIVO_AUTH_ID/Zentrunk/URI/" \
     -d '{"name":"LiveKit SIP","uri":"<subdomain>.india.sip.livekit.cloud;transport=tcp"}'
   ```
   Save `uri_uuid`. Use `;transport=tls` if secure trunking is on.
   - Indian numbers **must** use `.india.` region pinning; the LiveKit guide says calls "will fail to connect"
     otherwise.
   - In one production deployment, the pinned and unpinned hostnames resolved to the same India IPs at the time. Use
     the pinned form anyway.
3. Create the inbound trunk:
   ```bash
   curl ... ".../Zentrunk/Trunk/" -d '{"name":"LK inbound","trunk_direction":"inbound","primary_uri_uuid":"<uri_uuid>"}'
   ```
   Save `trunk_id`.
4. Attach the number (E.164 without `+`): `curl ... ".../Number/91XXXXXXXXXX/" -d '{"app_id":"<trunk_id>"}'` → 202.
   In the console: Phone Numbers → number → Application Type: SIP Trunk → select the trunk.
5. LiveKit: an inbound trunk with `numbers: ["+91XXXXXXXXXX"]` (nothing else is needed) plus a dispatch rule (§2).

**Outbound (LiveKit → PSTN)**
1. Credential: `curl ... ".../Zentrunk/Credential/" -d '{"name":"LK","username":"<5-20 alnum>","password":"<5-20 chars, 1 special>"}'`.
2. Outbound trunk: `curl ... ".../Zentrunk/Trunk/" -d '{"name":"LK outbound","trunk_direction":"outbound","credential_uuid":"<uuid>","secure":false}'`.
   An IP ACL (`ipacl_uuid`) is the alternative, but the LiveKit guide only documents credentials.
3. `GET .../Zentrunk/Trunk/<trunk_id>/` → `trunk_domain` = `<trunk_id>.zt.plivo.com`.
4. LiveKit outbound trunk with `address: "<trunk_id>.zt.plivo.com"`, the same username and password, and
   `destination_country: "in"`. If Plivo has `secure: true`, LiveKit needs `SIP_TRANSPORT_TLS` + `ALLOW`/`REQUIRE`.

**Caveats**
- Plivo hangup cause **4590** means a media-anchoring violation: media wasn't terminated in India.
  <https://www.plivo.com/docs/voice-agents/sip-trunking/deploy/calling-in-india.md>
- REFER transfer to PSTN: `transfer_to` must be `sip:+91XXXXXXXXXX@<trunk_id>.zt.plivo.com` (Plivo requires the SIP
  form).
- Before production: evaluate TLS/SRTP, source restrictions, call-rate limits and fraud alerts. Never commit Plivo
  auth tokens.

---

## 4. Twilio Elastic SIP Trunking

Sources: <https://docs.livekit.io/telephony/start/providers/twilio.md>, <https://www.twilio.com/docs/sip-trunking.md>.
**India:** Twilio can't give you a domestic Indian DID. It only sells +91 800 ITFS numbers, which "must be answered
outside of India"; see `references/12-india-telephony-compliance.md`. Use Twilio for US, EU and other markets.

### 4.1 Facts that matter
- **Termination URI** (you → PSTN) is `<name>.pstn.twilio.com`, or a localized form
  `<name>.pstn.{ashburn|umatilla|dublin|frankfurt|singapore|tokyo|sao-paulo|sydney}.twilio.com`. Each resolves to 3–4
  IPs, and Twilio fails over after 4 s.
- **Termination auth:** at least one of IP ACL or Credential List is required, and both are enforced if both are set.
  Use a **Credential List** for LiveKit Cloud.
- Caller ID must be a Twilio DID or Verified Caller ID. Numbers need E.164 **with `+`**, or you get 400.
- **Origination URIs** (PSTN → you): up to 10, each with `priority` (lower wins) and `weight` (load-balances within a
  priority).
  - Default transport is UDP, so add `;transport=tcp` or `;transport=tls`.
  - `;edge=<edge>` picks the egress edge (`roaming` isn't available for trunking).
  - X-headers can be appended as `?X-foo=bar`.
  - There is no failover on 2xx/400/404/405/410/416/482/484/486/6xx.
- **Codecs GA:** PCMU, PCMA. G.729, Opus and AMR-NB are Limited Availability via sales. There is **no G.722**.
- **REFER:** blind transfer only, enabled per trunk. `TransferMode` is `disable-all|enable-all|sip-only`;
  `TransferCallerId` is `from-transferee|from-transferor`. The PSTN target must be `tel:+1…` or
  `sip:+1…@<trunk>.pstn.twilio.com`. Early media isn't supported with transfers.
- **Recording:** per trunk (do not record / from ringing / from answer / dual). There's also a Disaster Recovery URL
  (TwiML fallback), and Extended Call Duration raises the limit from 4 h to 24 h.

### 4.2 Step by step
```bash
# 1. trunk
twilio api trunking v1 trunks create --friendly-name "LK" --domain-name "my-lk.pstn.twilio.com"
# 2. inbound: origination → LiveKit (TCP); optional regional fallback at priority 2
twilio api trunking v1 trunks origination-urls create --trunk-sid TKxxx --friendly-name "LiveKit" \
  --sip-url "sip:<subdomain>.sip.livekit.cloud;transport=tcp" --weight 1 --priority 1 --enabled
twilio api trunking v1 trunks origination-urls create --trunk-sid TKxxx --friendly-name "LiveKit US" \
  --sip-url "sip:<subdomain>.us.sip.livekit.cloud;transport=tcp" --weight 1 --priority 2 --enabled
# 3. outbound: Console → Voice → Credential lists → create; Trunk → Termination → Authentication → attach it
# 4. number
twilio api trunking v1 trunks phone-numbers create --trunk-sid TKxxx --phone-number-sid PNxxx
# 5. allow REFER (agent-initiated cold transfer)
twilio api trunking v1 trunks update --sid TKxxx --transfer-mode enable-all --transfer-caller-id from-transferee
```
6. LiveKit inbound trunk: `numbers: ["+1XXXXXXXXXX"]` with **no auth fields** (Twilio can't answer a digest challenge
   on origination), plus a dispatch rule.
7. LiveKit outbound trunk: `address: "my-lk.pstn.twilio.com"`, the credential-list username and password,
   `numbers: ["+1…"]`.
8. Secure: Twilio trunk `Secure=true` + origination `;transport=tls`. LiveKit `media_encryption: ALLOW|REQUIRE` on
   both trunks + outbound `transport: SIP_TRANSPORT_TLS`.

Self-hosted `livekit/sip`: use `sip:<your-sip-host>:5060;transport=tcp` (or `:5061;transport=tls`) as the
origination URI.

### 4.3 Alternative: Programmable Voice `<Dial><Sip>` (inbound only)
<https://docs.livekit.io/telephony/accepting-calls/inbound-twilio.md>, <https://www.twilio.com/docs/voice/twiml/sip.md>
```xml
<Response>
  <Start><Recording track="both"/></Start>          <!-- optional: compliance prompt / recording BEFORE the bot -->
  <Dial action="https://app.example.com/after-bot">
    <Sip username="lk_in_user" password="change-me">sip:+1XXXXXXXXXX@<subdomain>.sip.livekit.cloud;transport=tcp</Sip>
  </Dial>
</Response>
```
- LiveKit: `lk sip inbound create inbound-trunk.json --auth-user lk_in_user --auth-pass change-me`.
- "This method doesn't support SIP REFER or outbound calls." Workaround for transfer: after the bot hangs up the SIP
  leg, `<Dial action>` can return `<Dial>+1…</Dial>` to route to a human.
- `<Sip>` limits: URI under 255 chars; X-headers after `?` under 1024 chars. `transport=udp|tcp|tls`.
  `region=us1|us2|ie1|de1|sg1|jp1|br1|au1` picks the SIP-out region (default Virginia).
- Billing is Programmable Voice + SIP-out, not trunk minutes.
- A third route is the **LiveKit Twilio Connector** (Media Streams into a room, Cloud only): see
  `references/07-websocket-media-streams.md` §2.8.

---

## 5. Exotel ExoTrunk / vSIP

Sources: <https://docs.exotel.com/exotel-agentstream/connect-exotel-sip-trunk-to-livekit.md>,
<https://docs.exotel.com/dynamic-sip-trunking/overview.md>,
<https://docs.exotel.com/dynamic-sip-trunking/network-and-firewall-configuration.md>,
<https://developer.exotel.com/docs/sip-trunking/dynamic-sip-trunking.md>.
Exotel is a UL-VNO with Indian DIDs (ExoPhones), and LiveKit lists it as a tested provider.

**Network**
- **Transport:** TCP **5070** (default) or TLS **443** with SRTP. **UDP isn't supported, and neither is
  registration-based auth.** Always set `transport` explicitly on LiveKit trunks.
- **Edges (outbound):** `edge.mum.in.exotel.com` (primary) and `edge.hyd.in.exotel.com` (secondary); configure both
  for failover. Alternatives are `edge.in.exotel.com`, or `in.voip.exotel.com` ("specifically used for SIP Auth"). The
  LiveKit guide warns: "Configure IP:port as Exotel assigns — not an assumed carrier hostname."
- **Inbound signalling sources:** `35.154.174.161` (AWS Mumbai), `98.130.5.99` (AWS Hyderabad), `129.154.231.198`
  (OCI legacy).
- **RTP:** UDP **10000–40000**. "If calls connect but audio is missing or one-way, this port range is usually
  blocked."
- **Codec:** the SDP offers PCMA 8, PCMU 0, `telephone-event/8000`, `a=ptime:20`. "Validate G.711 codec support with
  PCMA as preferred. Avoid configuring unnecessary codecs."

**Limits:** default **20 calls per minute per trunk** (raise via your CSM); API 200 req/min. KYC and an account upgrade
are mandatory before SIP traffic.

**API sequence.** Base URL `https://api.in.exotel.com/v2/accounts/<sid>`, Basic auth with API key : API token. The order
is: create trunk → map numbers → credentials and/or IPs → destination URIs.
```bash
# 1. trunk  (response: trunk_sid, auth_type)
POST /trunks                     {"trunk_name":"lk","nso_code":"ANY-ANY","domain_name":"<sid>.pstn.exotel.com"}
# 2. DID → trunk ("pstn") or → Exotel flows/Voicebot ("flow")
POST /trunks/<trunk_sid>/phone-numbers   {"phone_number":"+91XXXXXXXXXX","mode":"pstn"}
# 3a. digest credentials (must equal the LiveKit outbound trunk's auth_username/auth_password)
POST /trunks/<trunk_sid>/credentials     {"user_name":"SIP_USER","password":"...","friendly_name":"livekit"}
# 3b. and/or IP ACL: one /32 per POST, no CIDR ranges
POST /trunks/<trunk_sid>/whitelisted-ips {"ip":"x.x.x.x","mask":32}
# 4. inbound destination (mandatory on the trunk "even if the call later goes to Flow"); 5061 = LiveKit's TLS port
POST /trunks/<trunk_sid>/destination-uris {"destinations":[{"destination":"<subdomain>.india.sip.livekit.cloud:5061;transport=tls"}]}
```
- Inbound routing is **DID → Flow (Connect applet) → trunk**, with the Connect applet's "Dial whom" =
  `sip:<trunk_sid>` (the trunk SID, "not a full SIP URI").
- Older path: email hello@exotel.com with your LiveKit FQDN and the DID, and support maps them
  (<https://support.exotel.com/support/solutions/articles/3000134145-livekit-and-exotel-sip-trunking-integration-guide>).

**LiveKit outbound trunk for Exotel** (the Exotel guide shows camelCase `authUsername`; the LiveKit API uses
snake_case):
```json
{ "trunk": { "name": "exotel-out", "address": "<EXOTEL_EDGE_IP_OR_FQDN>:443",
  "transport": "SIP_TRANSPORT_TLS", "media_encryption": "SIP_MEDIA_ENCRYPT_ALLOW",
  "numbers": ["+91XXXXXXXXXX"], "auth_username": "SIP_USER", "auth_password": "SIP_PASS",
  "destination_country": "in" } }
```
For plain TCP use `:5070`, `SIP_TRANSPORT_TCP` and `SIP_MEDIA_ENCRYPT_DISABLE`. The caller ID must be an ExoPhone
mapped to the trunk. Regional ExoPhones supported for a Mumbai SIP setup: "Veeno KA, DL, Mum, AP, GJ".

**Outbound auth conflict (VERIFY per account).** The newer page "When to use Credentials vs ACL" says digest is
"recommended for Voice AI direct trunking … Livekit". The older FAQ, the TCP/TLS India pages and the Call Directions
page still say "Outbound calls require IP-based whitelisting". Ask Exotel to enable digest-only on your trunk. If they
insist on ACL, you need a static egress IP: self-hosted SIP, a PBX bridge (§9), or LiveKit's published ranges if Exotel
accepts them, one /32 at a time.
(<https://docs.exotel.com/dynamic-sip-trunking/when-to-use-credentials-vs-acl-whitelisted-ips.md> vs
<https://docs.exotel.com/dynamic-sip-trunking/faqs.md>)

**Headers and troubleshooting.**
- Inbound INVITEs carry `X-Exotel-CallSid` (mandatory), `X-Exotel-LegSid` and `X-Exotel-TrunkSid`. Map them with
  `headers_to_attributes` so logs can be joined.
- 403 or 4001: digest, ACL or edge IP:port is wrong.
- "Rings, no bot": no agent is publishing in the room.
- "Drops at 30 s": RTP or NAT problem.

---

## 6. Telnyx

Source: <https://developers.telnyx.com/docs/voice/sip-trunking/livekit-configuration-guide>.
1. Mission Control → Real-Time Communications → Voice → SIP Trunking → **Add SIP Connection**. Set type **FQDN** with
   the LiveKit SIP host, and **Destination Number Format = +E.164** so the To user matches LiveKit `numbers`.
2. Outbound Calls Authentication: method **Credentials**. Note the username and password.
3. Outbound tab: attach an **Outbound Voice Profile** that allows your destinations.
4. My Numbers: set each number's connection to this SIP connection.
5. LiveKit outbound trunk: `"address": "sip.telnyx.com"` plus the credentials. Add
   `"headers_to_attributes": {"X-Telnyx-Username": "<telnyx username>"}` as the guide shows (shared IPs). The guide
   mixes `authUsername` and `auth_username`; the API uses snake_case.
6. TLS/SRTP: Inbound tab → transport TLS, media SRTP, FQDN port **5061**.

Debug with the Telnyx SIP Call Flow Tool (portal).

---

## 7. Vonage SIP trunking

Source: <https://developer.vonage.com/en/sip/technical-details>. There's no official LiveKit+Vonage guide.
- **Trunk:** created in the SIP Dashboard. It gets a regional domain such as `<company>.sip-us.vonage.com`.
- **Auth:** digest credential list and/or IP ACL (no longer the account API key and secret).
- **Transport:** UDP 5060, TCP 5060, or TLS 5061 (TLS 1.2+). SRTP uses `AES_CM_128_HMAC_SHA1_80`.
- **Codecs:** PCMA, PCMU, iLBC, G.729, G.722, Speex16. DTMF is RFC 4733. E.164 in From/To.
- **LiveKit:**
  - Outbound trunk `address` = the trunk domain, with the same credentials.
  - Inbound: point the trunk's inbound destination at `sip:<subdomain>.sip.livekit.cloud;transport=tcp`.
    **VERIFY** the field name and URI format in the Vonage SIP Dashboard.

---

## 8. Indian enterprise carriers (Airtel, Tata, Jio, Vi)

- **Airtel IQ** (the CPaaS) offers voice-bot integration as a **WebSocket stream** rather than a SIP trunk to your
  own SBC; the carrier keeps the number and the trunk. Its protocol guide is shared under NDA at onboarding: map it
  with `references/07-websocket-media-streams.md` §5 and §9.
- **Airtel / Tata Tele Business / Jio / Vi enterprise SIP trunks** (or PRI) are sold under contract. Expect:
  - entity KYC
  - a **static public IP** or leased line / MPLS to your premises or DC
  - often registration-based or IP-peered trunks
  - G.711 (PCMA)
  - per-channel (concurrent-call) licensing
  - no public self-serve docs

  Airtel SIP configuration is reported to be finicky
  (<https://www.3cx.com/community/threads/airtel-india-sip-trunk-configuration.121458/>, *community*).
- **Recommended pattern:** carrier SIP/PRI → your **Asterisk/FreeSWITCH/SBC** in an Indian DC or cloud region (a
  gateway for PRI) → **LiveKit India region**, IP-peered or digest. The PBX absorbs REGISTER, the static IP, codec
  quirks and number normalisation (§9).
- **Regulatory:** domestic VoIP-to-PSTN dial-out is restricted, so PSTN legs must go through licensed operators or
  authorised VNOs (*third-party summary*: <https://edesy.in/blog/sip-trunk-providers-india-ai-calling-2026>). See
  `references/12-india-telephony-compliance.md` for media anchoring and the A2P-call rules.
- **Ozonetel and Knowlarity:** no public SIP-to-LiveKit guide was found. Third parties report reusing their SIP trunks,
  possibly registration-based (so a PBX in front), or a stream API. Get specs from the vendor (**VERIFY**).

---

## 9. Generic PBX bridge (Asterisk / FreeSWITCH)

Use a PBX bridge when the carrier needs REGISTER, an IP ACL from a static IP, PRI/E1, or odd number formats, or when
you want IVR/recording before the bot. Both samples below are sketches built from standard options. **VERIFY against
your Asterisk/FreeSWITCH version** and test with a SIP trace.

### 9.1 Asterisk (PJSIP) — carrier registers to Asterisk's trunk, Asterisk peers with LiveKit
```ini
; /etc/asterisk/pjsip.conf  (VERIFY)
[transport-tcp]
type=transport
protocol=tcp
bind=0.0.0.0:5060
external_media_address=<PUBLIC_IP>
external_signaling_address=<PUBLIC_IP>
local_net=10.0.0.0/8

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_media_address=<PUBLIC_IP>
external_signaling_address=<PUBLIC_IP>

; ---------- carrier trunk that needs REGISTER ----------
[carrier-reg]
type=registration
transport=transport-udp
outbound_auth=carrier-auth
server_uri=sip:<carrier-registrar>
client_uri=sip:<account>@<carrier-registrar>
contact_user=<did-or-account>
retry_interval=60

[carrier-auth]
type=auth
auth_type=userpass
username=<account>
password=<carrier-password>

[carrier]
type=endpoint
transport=transport-udp
context=from-carrier
disallow=all
allow=alaw,ulaw
dtmf_mode=rfc4733
outbound_auth=carrier-auth
aors=carrier
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

[carrier]
type=aor
contact=sip:<carrier-proxy>

[carrier-identify]
type=identify
endpoint=carrier
match=<carrier-signalling-ip-or-cidr>

; ---------- LiveKit peer ----------
[livekit]
type=endpoint
transport=transport-tcp
context=from-livekit
disallow=all
allow=alaw,ulaw
dtmf_mode=rfc4733
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
aors=livekit
outbound_auth=livekit-out-auth     ; Asterisk -> LiveKit: answers LiveKit inbound-trunk digest
auth=livekit-in-auth               ; LiveKit outbound trunk -> Asterisk: LiveKit must present these

[livekit]
type=aor
contact=sip:<subdomain>.india.sip.livekit.cloud:5060\;transport=tcp

[livekit-out-auth]
type=auth
auth_type=userpass
username=lk_in_user
password=change-me

[livekit-in-auth]
type=auth
auth_type=userpass
username=lk_out_user
password=change-me-too
```
```ini
; /etc/asterisk/extensions.conf  (VERIFY)
[from-carrier]
; normalise whatever the carrier sends (e.g. 0XXXXXXXXXX or 91XXXXXXXXXX) to +91XXXXXXXXXX before LiveKit
exten => _X.,1,Set(DID=+91${EXTEN:-10})
 same => n,Dial(PJSIP/${DID}@livekit,60)
 same => n,Hangup()

[from-livekit]
; LiveKit outbound (agent dial-out or REFER target) -> carrier
exten => _+91X.,1,Set(CALLERID(num)=+91XXXXXXXXXX)
 same => n,Dial(PJSIP/0${EXTEN:3}@carrier,60)   ; carrier-specific dial format: VERIFY
 same => n,Hangup()
```
- LiveKit inbound trunk: `numbers: ["+91XXXXXXXXXX"]`, `auth_username: lk_in_user`, `auth_password: change-me`.
- LiveKit outbound trunk to the PBX: `address: "<PUBLIC_IP>:5060"`, `transport: SIP_TRANSPORT_TCP`,
  `auth_username: lk_out_user`.
- Firewall: 5060/tcp+udp, plus the RTP range (`rtp.conf` `rtpstart/rtpend`, e.g. 10000–20000/udp), from both the
  carrier and LiveKit.
- To accept REFER from LiveKit on the PBX, Asterisk handles it on the `livekit` endpoint; set `allow_transfer=yes`
  (default) and give the `from-livekit` context the dial targets.
- PBX-native streaming that skips LiveKit entirely: Asterisk `AudioSocket()` (TCP, slin 8 kHz), ARI External Media,
  and the newer Asterisk WebSocket channel driver (**VERIFY** the version; reportedly added in 20.x/22.x in 2025).

### 9.2 FreeSWITCH — gateway to LiveKit (VERIFY)
```xml
<!-- conf/sip_profiles/external/livekit.xml -->
<include>
  <gateway name="livekit">
    <param name="proxy" value="<subdomain>.india.sip.livekit.cloud"/>
    <param name="register" value="false"/>
    <param name="register-transport" value="tcp"/>
    <param name="username" value="lk_in_user"/>
    <param name="password" value="change-me"/>
    <param name="caller-id-in-from" value="true"/>
  </gateway>
</include>
```
```xml
<!-- carrier gateway that needs REGISTER (VERIFY) -->
<gateway name="carrier">
  <param name="proxy" value="<carrier-registrar>"/>
  <param name="register" value="true"/>
  <param name="username" value="<account>"/>
  <param name="password" value="<carrier-password>"/>
  <param name="extension" value="<did>"/>
</gateway>
<!-- dialplan: inbound DID -> LiveKit -->
<extension name="to-livekit">
  <condition field="destination_number" expression="^(?:\+?91)?(\d{10})$">
    <action application="set" data="absolute_codec_string=PCMA,PCMU"/>
    <action application="bridge" data="sofia/gateway/livekit/+91$1;transport=tcp"/>
  </condition>
</extension>
```
Set `ext-rtp-ip` / `ext-sip-ip` to the public IP in the external profile. FreeSWITCH-native streaming:
`mod_audio_fork` (*community module*).

---

## 10. Outbound calling

1. Create an outbound trunk (per provider above) and dispatch the agent to a fresh room with the number in metadata:
   `lk dispatch create --new-room --agent-name my-agent --metadata '{"phone_number":"+91YYYYYYYYYY"}'`.
2. The agent places the call:
   ```python
   from datetime import timedelta   # protobuf Duration fields accept timedelta (checked with livekit-api 1.2.1)
   await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
       room_name=ctx.room.name, sip_trunk_id=os.environ["SIP_OUTBOUND_TRUNK_ID"],
       sip_call_to=phone, sip_number="+91XXXXXXXXXX", participant_identity=f"sip-{phone}",
       wait_until_answered=True, ringing_timeout=timedelta(seconds=30)))   # max 80 s
   ```
   - The JSON / CLI form takes durations as strings (`"ringing_timeout": "30s"`).
   - Failure raises `api.SipCallError` (`.sip_status_code`, `.sip_status`): 486/603 rejected, 408/480 no answer, 5xx
     trunk failure. Then call `ctx.shutdown()`, because `USER_UNAVAILABLE` and `SIP_TRUNK_FAILURE` don't auto-close
     the session.
3. `wait_until_answered` also returns when **voicemail** answers (a 200 OK). Use answering-machine detection
   (`livekit.agents.AMD`, <https://docs.livekit.io/telephony/features/answering-machine-detection.md>). On WebSocket
   providers, use the provider's AMD (Twilio `MachineDetection` + `AsyncAmd=true`).
4. On an outbound call, the callee usually speaks first ("Hello?"). Don't greet into ringback. Wait for answer
   (`sip.callStatus == "active"`), then greet; see `references/03-conversation-mechanics.md`.
5. Dial-after-answer DTMF (IVR extension): `dtmf="ww1234#"` (`w` = 0.5 s pause).
6. Rate limits: Twilio 1 CPS by default, Exotel 20 CPM per trunk, Plivo India 2 CPS by default. **India outbound AI
   calls are A2P calls under TCCCPR 2026**, so declare them first (`references/12-india-telephony-compliance.md`).
7. Full-code templates: `templates/livekit/` (agent) and `templates/sip/` (trunk JSON).

---

## 11. Transfer (REFER) and DTMF

**Cold transfer (SIP REFER).**
```python
await ctx.api.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
    room_name=ctx.room.name, participant_identity=sip_identity,
    transfer_to="tel:+91XXXXXXXXXX",            # Plivo: "sip:+91XXXXXXXXXX@<trunk_id>.zt.plivo.com"
    play_dialtone=False))
```
- `ringing_timeout` defaults to 30 s. On timeout the request errors and the caller stays in the room, so tell the
  model to apologise and promise a callback.
- CLI: `lk sip participant transfer --room R --identity I --to tel:+…`.
- **The provider must allow REFER:**
  - Twilio: `--transfer-mode enable-all`.
  - Plivo: SIP target on the trunk domain.
  - Exotel: **VERIFY** REFER support on vSIP.
  - PBX: the dialplan must route the Refer-To.
- Without REFER, the caller ID shown to the target is set at the provider trunk, not per transfer.
- **Warm transfer** (consult a human, then bridge) uses a second room plus `create_sip_participant` to the human,
  followed by a move. See `references/03-conversation-mechanics.md` and LiveKit's `warm_transfer` workflow.
- Register a `transfer_to_human` tool **only when a transfer number is configured**. Otherwise the prompt must never
  promise a transfer.

**DTMF (RFC 4733 `telephone-event/8000`).**
- Receive: `@ctx.room.on("sip_dtmf_received") def _(ev: rtc.SipDTMF): ev.digit`. Forward it to Gemini as text, e.g.
  `"(caller pressed 1)"`.
- Send: `await ctx.room.local_participant.publish_dtmf(code=1, digit="1")` (`*`=10, `#`=11).
- `AgentSession(ivr_detection=True)` relays DTMF to IVRs on outbound calls. `GetDtmfTask` collects digits.
- If digits arrive as in-band tones instead (SIP INFO or audio), the carrier isn't doing RFC 4733. Ask for 4733 and
  keep `telephone-event` in the codec offer. Twilio India lists RFC 2833 DTMF as "Yes (Not guaranteed)".
- Source: <https://docs.livekit.io/telephony/features/dtmf.md>

---

## 12. Testing and debugging checklist

**Before the first call**
- [ ] The number is attached to the provider trunk, and the account isn't a lapsed trial (busy tone).
- [ ] The origination URI has `;transport=tcp|tls` and the right region host (`.india.` for Indian numbers).
- [ ] LiveKit inbound trunk `numbers` match the To format exactly (`+E.164`). The dispatch rule references that trunk
      and a **deployed** `agent_name`.
- [ ] TLS on one side ⇔ TLS + `media_encryption` ALLOW/REQUIRE on the other.
- [ ] Outbound: `address` is a host (not a URI), credentials match, `transport` is explicit (Exotel), caller ID is
      allowed by the carrier, and `destination_country` is set.
- [ ] Firewall (self-hosted / PBX): 5060 and/or 5061 plus the RTP range are open to the carrier and to LiveKit.
      `use_external_ip: true` is set on `livekit/sip`; `external_media_address` is set on Asterisk.

**Symptom → where to look**

| Symptom | Likely cause | Check |
|---|---|---|
| Busy tone; never reaches LiveKit | Trial lapsed or number not linked | Provider console / call logs |
| 404 / 503 at LiveKit | Number format mismatch, no matching trunk, or `.india.` missing for a pinned provider | LiveKit Telephony call logs; trunk `numbers` |
| 401/403/407 loops | Digest mismatch, carrier can't do digest (Twilio origination), wrong Exotel edge | Provider logs; SIP trace |
| 488 Not Acceptable | Codec or SRTP mismatch | SDP in a trace; `media_encryption` |
| Rings forever, then 486 | No agent joined or published audio (dispatch `agent_name`, worker down, cold start) | `lk sip dispatch list`, `lk agent status`, worker logs |
| Connects, no audio either way | RTP blocked or NAT (wrong public IP in SDP) | `c=` line in SDP vs public IP; RTP ports |
| One-way audio | Asymmetric NAT, `rtp_symmetric=no`, or carrier sending to a private IP | `sngrep` / pcap: are RTP packets flowing both ways? |
| Drops at ~30 s | No RTP received (media timeout), or missing ACK | RTP flow; ACK in the trace |
| Plivo hangup 4590 | Media not anchored in India | `.india.` endpoint; India-region account |
| DTMF not detected | Carrier not sending RFC 4733 | `telephone-event` in SDP; RTP PT 101 events |
| Caller hears silence after the goodbye | Agent session ended but the room (and SIP leg) is still open | `delete_room_on_close=True` / delete the room |

**Tools**
- `sngrep` or `tcpdump -i any -w call.pcap port 5060 or portrange 10000-20000` on any PBX or self-hosted SIP host, then
  open in Wireshark (Telephony → VoIP Calls → Play Streams).
- Twilio Console Debugger + Call Logs (SIP trace per call); Plivo Zentrunk call logs; Telnyx SIP Call Flow Tool;
  Exotel call logs + `X-Exotel-CallSid`.
- LiveKit Cloud dashboard → Telephony → calls (shows the SIP status and disconnect reason). Correlate with `sip.callID`
  / `sip.callIDFull`.
- `lk sip inbound list`, `lk sip outbound list`, `lk sip dispatch list`, `lk agent status`.
- **Acceptance call:**
  1. Provider log shows the call, and LiveKit Telephony shows it.
  2. An individual room exists with a SIP participant.
  3. The agent joined and published audio.
  4. Both directions are heard.
  5. DTMF is logged.
  6. A clean caller hang-up ends the room.
  7. The recording/transcript is uploaded.

  If the call fails before LiveKit, check the provider logs first. If LiveKit received it but nobody answered, check
  dispatch.

---

## 13. How to tell whether a provider supports SIP trunking

| You see in their docs | Meaning |
|---|---|
| "Elastic SIP Trunking", "Zentrunk", "vSIP/ExoTrunk", "SIP Connection", "BYOC", "SIP trunk" | SIP trunking. Usable with LiveKit if the conditions below hold. |
| "Origination URI / destination URI" | Provider → you (inbound). You give them your LiveKit SIP host. |
| "Termination URI / domain / SIP proxy" | You → provider (outbound). It becomes the LiveKit `address`. |
| "IP ACL / whitelisted IPs / credential list" | Auth options. Prefer credentials for LiveKit Cloud. |
| "Register to our server", "SIP account / extension / registrar", only username+password+registrar | **REGISTER-only.** LiveKit can't do it, so put a PBX in front (§9). |
| "Media Streams", "Audio Streaming", `<Stream>`, "Voicebot applet", NCCO `websocket`, "stream_url" | WebSocket media streaming, not SIP. See `references/07-websocket-media-streams.md`. |
| "PRI / E1 / leased line only" | Needs a media gateway + PBX (§8, §9). |

**Questions to ask the provider:**
1. Do you support IP- or FQDN-peered trunks without REGISTER?
2. Which transports (UDP, TCP, TLS) and ports?
3. Which codecs, and is PCMA or PCMU preferred?
4. Is RFC 4733 DTMF supported?
5. Do you support REFER, and to PSTN?
6. Is digest auth supported in both directions?
7. What are the limits on concurrent calls and CPS?
8. What are the media IP ranges and RTP ports?
9. For India: where is media anchored, and are numbers available after KYC?
