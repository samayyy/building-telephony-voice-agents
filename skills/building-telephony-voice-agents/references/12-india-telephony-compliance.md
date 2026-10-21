# 12 — India telephony and compliance (DoT / TRAI / DPDP) for AI voice agents

Purpose: what changes when the phone numbers or callers are in India. That means media anchoring, KYC for numbers,
which providers can give you Indian DIDs over SIP or WebSocket streams (and Twilio's limits), the numbering series for
commercial calls (140 / 1600 / 1601), the TRAI TCCCPR amendments (Feb 2025, and the 2026 Third Amendment that makes AI
voice calls "A2P calls" needing advance declaration on DLT), consent and DND, recording consent under the DPDP Act,
and where Gemini Live processing actually happens.
**This is engineering guidance, not legal advice.** Regulations here change quickly. Confirm every item with the
telecom provider and counsel before launch, especially for outbound calling. Items marked **VERIFY** were not
confirmed against a primary source or have a date that must be re-checked.
Setup steps are in `references/06-sip-trunking-providers.md` (SIP) and `references/07-websocket-media-streams.md`
(streams).

Researched 2026-10-21. Primary sources are linked inline. Third-party summaries are marked *(third-party)*.

## Contents
1. [Quick checklist](#1-quick-checklist)
2. [Media anchoring: where the call must be terminated](#2-media-anchoring)
3. [Numbers, KYC and eligibility](#3-numbers-kyc-and-eligibility)
4. [Which providers give Indian DIDs (SIP or streams)](#4-which-providers-give-indian-dids)
5. [Twilio and India](#5-twilio-and-india)
6. [Commercial calls: 140 / 1600 / 1601 series, DLT, consent](#6-commercial-calls)
7. [TCCCPR Third Amendment 2026: AI calls are A2P calls](#7-tcccpr-third-amendment-2026)
8. [Recording, transcripts, DPDP, AI disclosure](#8-recording-transcripts-dpdp-ai-disclosure)
9. [Where Gemini Live processes audio (data residency)](#9-where-gemini-live-processes-audio)
10. [Open items to verify](#10-open-items-to-verify)

---

## 1. Quick checklist

**Inbound AI agent on an Indian number** (the lower-risk case):
- [ ] The number comes from a licensed operator or authorised VNO, with entity KYC done (§3).
- [ ] Media is anchored in India: LiveKit `{sub}.india.sip.livekit.cloud`, a Plivo India data-region account, the
      Exotel Mumbai edge, or an operator-anchored call flow (Airtel, Tata, Jio) (§2).
- [ ] The bridge or agent runs in an Indian region (`asia-south1` / `ap-south-1`) for latency and anchoring.
- [ ] The opening line discloses the AI and recording (§8), e.g. "This call is with an AI assistant and may be
      recorded."
- [ ] Recordings and transcripts are stored in India-region buckets with a defined retention period, and DPDP notice
      and consent are in place (§8).
- [ ] The data-residency position on Gemini processing (US/EU) is accepted by the customer (§9).

**Outbound AI calls in India** (high risk; get sign-off). All of the above, plus:
- [ ] Classify the call: promotional, service, or transactional (§6).
- [ ] Use the matching number series: 140xx (promotional), 1600xx/1601xx (service/transactional for BFSI/government),
      or a landline series for service/transactional only (§6). **No 10-digit mobile numbers for telemarketing.**
- [ ] Register the entity on DLT. **Declare A2P usage and CLI ranges in advance** to the originating operator
      (TCCCPR Third Amendment, Reg. 4) (§7).
- [ ] Keep consent records (explicit, or inferred from an existing relationship) and honour DND preferences for
      promotional calls.
- [ ] Stay within calling hours and frequency limits (**VERIFY** current TRAI limits). Keep complaint handling within
      the 7-day window, and expect carrier audits.
- [ ] Use answering-machine detection and don't leave robo-voicemails unless they're cleared.

---

## 2. Media anchoring

- Plivo: "TRAI regulations require voice traffic for India calls to be **anchored on servers located in India**." The
  SIP and media for an Indian call must terminate on infrastructure in India.
  <https://www.plivo.com/docs/voice-agents/sip-trunking/deploy/calling-in-india.md>,
  <https://www.plivo.com/docs/voice/concepts/india-calling.md>
  - The Plivo account must be in the **India data region**, which can't be changed later.
  - A violation shows up as Plivo hangup cause **4590**.
  - Plivo lists LiveKit as supported (via region pinning). It lists Vapi as **not** supported for Indian numbers.
- **LiveKit Cloud:**
  - Inbound: point the carrier at `{subdomain}.india.sip.livekit.cloud`.
  - Outbound: set `destination_country: "in"` on the trunk or the call. LiveKit's Plivo guide says Indian calls
    "will fail to connect" without this.
  - Region pinning is listed as a **Scale-plan (and above)** feature on the 2026-10-21 pricing page. **VERIFY** for
    your plan before you commit to LiveKit Cloud for Indian numbers.
  - LiveKit publishes static IPs that cover India for carrier ACLs (`references/06-sip-trunking-providers.md` §1).
- **Self-hosted** `livekit/sip` or a PBX: run it in an Indian region.
- **WebSocket streams:** the provider anchors the call in India: Exotel (AWS Mumbai/Hyderabad), carrier CPaaS products such as Airtel IQ, and
  Plivo India region. Your bridge receives the stream. Put the bridge in India too: it keeps
  latency low, and it is the conservative reading of the anchoring rule. Whether a stream to a server outside India
  breaches anchoring is **not** stated in the sources (**VERIFY** with the provider).
- **Older OSP framework context:** centralised gateways were allowed for inbound PSTN but not outbound
  (<https://khaitanco.com/sites/default/files/2021-06/Ergo_25062021_0.pdf>).
- Domestic VoIP-to-PSTN dial-out remains restricted, so PSTN legs must go through licensed operators (Airtel, Tata,
  Jio, Vi) or authorised VNOs (e.g. Exotel, a UL-VNO). *(third-party)*
  <https://edesy.in/blog/sip-trunk-providers-india-ai-calling-2026>

---

## 3. Numbers, KYC and eligibility

- **Only India-registered entities with KYC** get Indian DIDs and domestic routes. Foreign entities get international
  routes and a non-Indian CLI (per Plivo).
- **Exotel:** ExoPhones need KYC, and SIP traffic needs "prior account upgrade/KYC". Regional ExoPhones supported for a
  Mumbai SIP setup: "Veeno KA, DL, Mum, AP, GJ". The Connect applet's outgoing number is "subject to telecom
  circle/region restrictions".
- **Plivo:** India data-region account, KYC per number. **Trial accounts lapse**: callers then hear busy at the
  carrier, and the call never reaches your stack (seen in a production deployment).
- **Airtel IQ / Tata / Jio / Vi:**
  - Enterprise contract with entity KYC and often a static IP.
  - Carrier CPaaS products (e.g. Airtel IQ) usually keep the number, trunk and call-flow anchoring on the carrier's
    side. Confirm in the contract which duties stay with you: typically the bot, PII handling and recording retention.
- **Caller ID:** outbound CLI must be a number you own on that trunk: an ExoPhone mapped to the trunk, or your Plivo
  or operator number.

---

## 4. Which providers give Indian DIDs

| Provider | Indian DIDs | SIP trunk to your stack | WebSocket stream | Notes |
|---|---|---|---|---|
| **Exotel** | Yes (ExoPhones, KYC) | Yes: ExoTrunk/vSIP, TCP 5070 / TLS 443, no UDP, no REGISTER, 20 CPM default; tested with LiveKit | Yes: Voicebot applet (PCM16 8/16/24k), Connect Voice AI API | Media in AWS Mumbai/Hyderabad. Outbound auth: digest vs ACL conflict (VERIFY) |
| **Plivo** | Yes (India data region, KYC) | Yes: Zentrunk, G.711, needs LiveKit India pinning | Yes: Audio Streams (µ-law 8k or L16 8/16k) | Hangup 4590 = anchoring violation. ~Rs 0.38/min quoted by a third party, Sep 2026 (VERIFY) |
| **Airtel IQ** | Yes (carrier-owned number) | Not offered to your own SBC (VERIFY) | Yes: voice-bot streaming; protocol shared under NDA | Expect a carrier certification before go-live (VERIFY) |
| **Airtel / Tata Tele / Jio / Vi enterprise SIP** | Yes (contract) | Yes, often REGISTER or IP-peered, static IP; via Asterisk/FreeSWITCH/SBC in India | Not generally | Per-channel licensing. Configuration can be finicky *(community)* |
| **Ozonetel** | Yes | Unclear; third parties report trunk reuse (possibly REGISTER) | Stream/KooKoo integrations reported *(third-party)* | VERIFY with the vendor |
| **Knowlarity** | Yes | Its own Tata SIP backbone; no customer trunk documented | `update_wss_url` stream API reported *(third-party)* | VERIFY with the vendor |
| **Vobiz** | Mentioned *(third-party)* | ? | ? | VERIFY |
| **Twilio** | **Toll-free only (+91 800 ITFS, answered outside India)** | Not for domestic Indian service | Media Streams processed in US1/IE1/AU1 only | §5 |
| **Telnyx / Vonage** | Check current inventory (VERIFY) | Yes (global) | Yes | Not researched for Indian DIDs |

---

## 5. Twilio and India

- **Numbers:** Twilio's India inventory is **toll-free only** ("Toll-Free Prefixes +91800"), with the address
  requirement "Must be outside of the country". <https://www.twilio.com/en-us/guidelines/in/regulatory>
- The +91 800 ITFS restrictions: "**Must be answered outside of India.** May require caller to activate
  'International dialing/ISD'. Cannot be reached from TTSL (Prepaid) and Etisalat DB Telecom. **Not for outbound
  use.**" They "can only be reached domestically", dialled as `000 800 XXX XXXX`.
  <https://support.twilio.com/hc/en-us/articles/115007579027>,
  <https://support.twilio.com/hc/en-us/articles/40728263317915>
- **Outbound to India:** "Outbound calls to India can only be made from international (non-Indian) numbers. **Voice
  OTP/2FA is not permitted** and may be blocked. High volumes of short-duration calls may trigger automated carrier
  blocks." Inbound domestic: "N/A". RFC 2833 DTMF: "Yes (Not guaranteed)". Emergency calls: "No".
  <https://www.twilio.com/en-us/guidelines/in/voice>
- Media Streams are processed only in US1/IE1/AU1, with no India region.
- **Conclusion:** Twilio isn't a domestic Indian telephony option for SIP or streams. "Answered outside India" also
  conflicts with any in-India processing requirement. Use Exotel, Plivo (India region), Airtel IQ, or a licensed
  operator.

---

## 6. Commercial calls

Sources: TRAI TCCCPR 2018 and amendments; TRAI press note 10 Jul 2026
(<https://www.scconline.com/blog/post/2026/07/18/trai-clarifies-1600-and-140-series-number-framework/>); DoT
allotment of the 160 series in 2024
(<https://www.storyboard18.com/how-it-works/dot-allots-numbering-series-exclusively-for-service-and-transactional-voice-calls-33015.htm>).

| Series | Use | Notes |
|---|---|---|
| **140xx** | **Promotional** calls, all sectors | Senders register with operators; on DLT since Oct 2024. Recipients can block via DND preferences (TRAI DND app). |
| **1600xx** | **Service and transactional** calls by RBI/SEBI/IRDAI/PFRDA-regulated (BFSI) entities and government-to-citizen calls | Can't be tagged, blocked or filtered as spam. |
| **1601xx** | Designated for BFSI | As above. |
| **Landline series** (022…, 080…) | Service/transactional only, **never promotional** (per Plivo) | Using the wrong series is itself a violation. Complaints count as UCC regardless of consent. |
| 10-digit mobile numbers | **Not for telemarketing** (Feb 2025 amendment) | |

- **Feb 2025 amendment** (TRAI PR 11/2025):
  - 10-digit numbers are barred for telemarketing, and commercial calls must use 140/1600.
  - The complaint window is now 7 days.
  - Inferred consent lasts only for the duration of the relationship. Explicit consent for a transaction lasts 7 days.
  - <https://cms.trai.gov.in/sites/default/files/2025-02/PR_No.11of2025.pdf>,
    <https://www.jsalaw.com/wp-content/uploads/2025/02/JSA-Prism-Telecommunications_February-2025_TCCCPR-amendments.Final_.pdf>
- **Transactional call** (Plivo's summary): within 30 minutes of a customer-initiated transaction, no consent needed.
  **Service calls** need an existing relationship and consent rules apply. **Promotional** calls need consent and a
  DND scrub.
- **Enforcement** (Aug 2024): violators can lose *all* telecom resources and be blacklisted for up to 2 years.
  <https://www.barandbench.com/view-point/trais-crackdown-on-spam-calls-and-ai-driven-telemarketing>
- **Calling hours and frequency caps: VERIFY** the current TRAI or sector rules (e.g. the RBI fair-practice code for
  collections). Don't hard-code a window from memory.

---

## 7. TCCCPR Third Amendment 2026

**Telecom Commercial Communications Customer Preference (Third Amendment) Regulations, 2026** (No. 05 of 2026,
notified 18 Sep 2026): <https://www.trai.gov.in/sites/default/files/2026-09/Regulation_18092026.pdf>.

- **"A2P call"** means a call "initiated by an application, software system, or automated platform without direct
  human dialling … including using autodialling, robo-calls and/or pre-recorded/artificial voice technologies".
  **Outbound AI voice-agent calls fall inside this definition.**
- **Reg. 4, advance declaration:**
  - Every Sender must **declare A2P call usage in advance to the Originating Access Provider (OAP)**, with the **CLI
    ranges** it will use. The OAP records the declaration on **DLT**.
  - **Undeclared A2P calls are treated as UCC** (unsolicited commercial communication).
  - Effective dates: Reg. 4 applies 60 days after Gazette publication. If that was 18 Sep 2026, that's about mid-Nov
    2026 (**VERIFY the Gazette date**). Most other provisions apply after 30 days, and the Annexure after 90 days.
- **Reg. 35A:** the terminating operator may charge up to **Rs 0.05/min** on A2P calls. Calls from 140xx / 1600xx /
  1601xx are exempt.
- **Reg. 21A:** operators' AI/ML "UCC_Detect" systems flag suspected spam CLIs within 2 hours. CLIs from 140xx /
  1600xx / 1601xx can't be flagged as spam to recipients.
- **Enforcement context:** in April 2026, about 75% of 20,591 disconnections were SIP/PRI connections used for A2P
  calls. Expect carriers to audit AI-calling trunks. Plivo cites a 5-day response window for complaint audits and a
  2 CPS default for outbound calls in India.
- **Practical reading for builders:**
  - An **inbound** AI agent answering calls on your DID isn't an A2P *originator*.
  - **Any outbound AI call** (reminders, collections, surveys, lead qualification) needs:
    1. the declaration (Reg. 4)
    2. the right number series (§6)
    3. consent records
    4. DND handling for promotional calls
  - Build the declaration into the go-live checklist, and log CLI, consent reference and call purpose per call.
- On carrier CPaaS products (e.g. Airtel IQ), check which TRAI obligations the carrier takes on and what remains the
  sender's (your) responsibility, such as the A2P declaration and consent (**VERIFY** per contract).

---

## 8. Recording, transcripts, DPDP, AI disclosure

- **Recording consent:** India has no single statute that sets explicit one-party or two-party consent for call
  recording.
- **DPDP:** recordings, transcripts and extracted fields are **personal data** under the **DPDP Act 2023**. The **DPDP
  Rules 2025** were notified on 13 Nov 2025. Their notice, consent, security and retention obligations apply from
  about **13 May 2027** (18 months; **VERIFY**).
  <https://static.pib.gov.in/WriteReadData/specificdocs/documents/2025/nov/doc20251117695301.pdf>,
  <https://www.azbpartners.com/bank/update-indias-digital-personal-data-protection-framework-comes-into-effect/>
- **Sector regulators** (RBI, SEBI, IRDAI) separately mandate or govern recording and retention for some flows
  (**VERIFY** per client). Consent under TCCCPR doesn't satisfy DPDP.
- **What to build:**
  1. **Disclose at call start:** "This call is with an AI assistant and may be recorded for quality and training."
     - Play it as fixed audio (a pre-rendered greeting; see `references/03-conversation-mechanics.md`). Live models don't reliably speak
       mandatory lines verbatim.
     - On Exotel or Twilio flows, a `<Say>`/greeting applet *before* the bot works too.
  2. Store recordings and transcripts in **India-region** storage with a defined retention period and access control.
     Keep the governance decision about unredacted copies documented. LiveKit Cloud observability recordings are kept
     30 days and may be processed in the US.
  3. **Fail closed:** record only when recording is enabled *and* governance has approved it.
  4. Respect provider-side recording: carrier-delivered recordings (you own retention); Exotel applet `Record`;
     Twilio `<Start><Recording>` before `<Connect>`.
  5. Data minimisation: capture structured fields through tool arguments, not by keeping everything.
- **AI disclosure:** no India rule found that specifically mandates it for inbound calls. Disclosure is best practice,
  and the A2P regime implies transparency for outbound. Never let the agent claim to be human.
- **Number masking:** LiveKit `hide_phone_number` on dispatch rules or `CreateSIPParticipant` keeps the caller's number
  out of room participants. Indian cloud-telephony providers sell virtual-number masking for human hand-off.

---

## 9. Where Gemini Live processes audio

- `gemini-3.8-live` on Vertex is served only from the **`us` and `eu` multi-regions and `us-central1`**. ML processing
  happens in the `us`/`eu` multi-region. `asia-south1` and other regions return `1008 Publisher model ... not found`
  (tested in a production deployment from India). The Gemini Developer API makes no in-country processing promise.
- So an India-anchored call still sends caller audio to Gemini in the US or EU. Anchoring rules concern the
  **telecom** media path. Whether AI processing abroad is acceptable is a **data-residency and DPDP question for the
  customer and counsel** (cross-border transfer rules under DPDP; **VERIFY**).
- **Latency:** India → `us` adds about 115–140 ms each way per turn. Measured end-of-speech → first audio was 1.2–1.6 s
  typical in production (India callers, `location="us"`). Keep the SIP or bridge leg in India; the single TCP crossing
  to Gemini happens once per turn either way.
- **Vertex session resumption** stores session data for up to 24 h. For zero data retention, don't enable resumption
  (trade-off: calls longer than ~10 minutes then need a fresh session).

---

## 10. Open items to verify

1. Gazette publication date of TCCCPR Third Amendment 2026, and so the Reg. 4 effective date (about mid-Nov 2026 if
   18 Sep).
2. Current TRAI and sector calling-hour and frequency limits for service and promotional calls.
3. Whether streaming an India-anchored call's media to a bridge or AI outside India is acceptable to each provider and
   operator.
4. Exotel outbound SIP auth (digest vs mandatory IP ACL) for your account.
5. LiveKit Cloud plan needed for India region pinning.
6. DPDP Rules effective date (~13 May 2027) and cross-border transfer conditions for Gemini processing.
7. Whether a carrier CPaaS's compliance ownership covers the A2P declaration for your outbound campaigns.
8. Indian DID availability and pricing on Telnyx, Vonage, Vobiz, Ozonetel and Knowlarity.
