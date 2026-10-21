# 05. Self-hosting LiveKit for phone agents

Use this file when LiveKit itself runs on your own VMs or Kubernetes instead of LiveKit Cloud: what to deploy (livekit-server, Redis, the `livekit/sip` service, optional egress, your agent workers), the config files, ports, TLS and TURN, NAT and firewall rules for SIP and RTP, Docker Compose and Kubernetes notes, how to create trunks and dispatch rules against your own server with `lk`, what you lose compared with Cloud, and sizing. The decision of whether to self-host at all is in `01-architecture-and-decisions.md` section 4. Ready-to-edit files are in `templates/selfhost/`. Carrier-side trunk setup is in `06-sip-trunking-providers.md`.

Versions current on 2026-10-21: livekit-server v1.13.10, sip v1.20.0, egress v1.15.0, lk CLI 2.19.1, livekit-agents 1.8.8. SIP config keys below were read from `livekit/sip` `pkg/config/config.go` (main branch), server keys from `livekit/livekit` `config-sample.yaml`, egress keys from the `livekit/egress` README, all fetched 2026-10-21. **Nothing in this file was run against a live self-hosted deployment for this skill.** The production deployment behind this skill used LiveKit Cloud.

## Contents

1. [When self-hosting is worth it](#1-when-self-hosting-is-worth-it)
2. [Topology](#2-topology)
3. [livekit-server config](#3-livekit-server-config)
4. [livekit/sip service](#4-livekitsip-service)
5. [Ports and firewall](#5-ports-and-firewall)
6. [NAT and firewall gotchas](#6-nat-and-firewall-gotchas)
7. [Egress (recording)](#7-egress)
8. [Docker Compose and Kubernetes](#8-docker-compose-and-kubernetes)
9. [Agent workers against your server](#9-agent-workers-against-your-server)
10. [Trunks and dispatch rules with lk](#10-trunks-and-dispatch-rules-with-lk)
11. [What you lose vs Cloud](#11-what-you-lose-vs-cloud)
12. [Sizing](#12-sizing)
13. [Bring-up and verification checklist](#13-bring-up-and-verification-checklist)

---

## 1. When self-hosting is worth it

Self-host when one of these is true:
- A data-residency or on-premises mandate requires SIP and media to terminate on infrastructure you control. Note that Gemini Live still runs in Google's `us`/`eu` regions, so self-hosting LiveKit alone does not keep caller audio in-country.
- Volume is high enough that LiveKit Cloud's per-minute agent ($0.01) and SIP ($0.003-0.004) fees dominate model and carrier costs.
- The network is private or air-gapped (operator SIP over MPLS, SBC in a data centre).

Otherwise, use LiveKit Cloud. A common middle ground is **self-hosted agent workers against LiveKit Cloud transport**: you keep Cloud SIP, region pinning and BVC noise cancellation and run the Python workers yourself.

## 2. Topology

| Component | Image / binary | Needs Redis | Public ports |
|---|---|---|---|
| livekit-server (SFU, signalling, room API, agent dispatch) | `livekit/livekit-server`, `brew install livekit`, or `curl -sSL https://get.livekit.io \| bash` | Optional for one node; **required** for multi-node and for SIP, egress and ingress (they talk over Redis psrpc) | 7880 HTTP/WS behind TLS; 7881/tcp ICE; 50000-60000/udp ICE; TURN 3478/udp and TURN/TLS 5349 or 443 |
| livekit/sip | `livekit/sip` (host networking) | **Yes**, the same Redis as the server | 5060 udp+tcp SIP; 5061 tcp TLS (optional); **10000-20000/udp RTP** |
| livekit/egress | `livekit/egress` | **Yes** | None inbound; outbound to the server and storage |
| Agent workers (your Python) | Your image, `python agent.py start` | No | None inbound; outbound WSS to the server, WebRTC media to the SFU ports; health on 8081 (private) |
| Redis | `redis` | n/a | Private only |

```text
                     Internet / carrier
      SIP 5060 udp+tcp, 5061 tls  |  RTP 10000-20000/udp
                                  v
  +------------------- VM or node with public IP (host networking) --------------------+
  |  livekit/sip  --psrpc-->  Redis  <--psrpc--  livekit-server  <--psrpc--  egress      |
  |      |  joins rooms as a participant (WebRTC to the SFU)      ^ 7880 via TLS proxy   |
  +------|----------------------------------------------------------|-------------------+
         v                                                          |
     room: SIP participant  <---- WebRTC (50000-60000/udp or 7881/tcp) ---- agent workers
                                                                   (outbound only) --wss--> Gemini Live
```

Docs: https://docs.livekit.io/transport/self-hosting.md, https://docs.livekit.io/transport/self-hosting/ports-firewall.md

## 3. livekit-server config

Sources: https://docs.livekit.io/transport/self-hosting/deployment.md, https://github.com/livekit/livekit/blob/master/config-sample.yaml

```yaml
port: 7880
log_level: info
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true        # discover the public IP via STUN (cloud VMs with 1:1 NAT)
  # node_ip: <public-ip>       # explicit alternative; set use_external_ip: false for it to apply
redis:
  address: redis:6379          # required for SIP, egress, multi-node
keys:
  APIxxxxxxxxxxxx: <long-random-secret>    # api_key: api_secret (generate: openssl rand -base64 32)
turn:
  enabled: true
  domain: turn.example.com     # must match the TURN TLS certificate
  tls_port: 5349               # must be 443 if there is no L4 load balancer in front
  udp_port: 3478
  # external_tls: true         # when an L4 load balancer terminates TLS
  # cert_file / key_file       # when LiveKit terminates TURN/TLS itself
# webhook: { api_key: APIxxxxxxxxxxxx, urls: ["https://app.example.com/livekit/webhook"] }
# room: { empty_timeout: 300, departure_timeout: 20 }
# prometheus: { port: 6789 }
```

- **TLS:** clients (agent workers, `lk`, browsers) connect to `wss://livekit.example.com`. Terminate TLS at a reverse proxy or load balancer with a CA-signed certificate; self-signed certificates fail. TURN/TLS needs its own domain and certificate.
- Config comes from `--config <file>` or the `LIVEKIT_CONFIG` env var (the whole YAML body).
- `livekit-server --dev` uses keys `devkey`/`secret` and binds `127.0.0.1:7880`; add `--bind 0.0.0.0` for LAN access. Never expose dev keys.
- **TURN for phone-only deployments:** SIP callers and agent workers do not use TURN. You need it only for browser or app clients behind restrictive firewalls. A telephony-only deployment can leave `turn.enabled: false` if workers reach the ICE ports directly.
- **VM quick start:** `docker run --rm -it -v$PWD:/output livekit/generate` produces `docker-compose.yaml`, `livekit.yaml`, `caddy.yaml` (automatic Let's Encrypt), `redis.conf` and a cloud-init script (systemd unit `livekit-docker`). It does not add SIP; add the `sip` service from `templates/selfhost/docker-compose.yml`. Source: https://docs.livekit.io/transport/self-hosting/vm.md
- Template: `templates/selfhost/livekit.yaml`.

## 4. livekit/sip service

Docs: https://docs.livekit.io/transport/self-hosting/sip-server.md, README https://github.com/livekit/sip

```yaml
api_key: APIxxxxxxxxxxxx           # or env LIVEKIT_API_KEY
api_secret: <secret>               # or env LIVEKIT_API_SECRET
ws_url: ws://127.0.0.1:7880        # or env LIVEKIT_WS_URL; server reachable from the SIP host
redis:
  address: 127.0.0.1:6379          # MUST be the same Redis as livekit-server
sip_port: 5060                     # UDP + TCP signalling (default 5060)
rtp_port: 10000-20000              # RTP media range (default 10000-20000)
use_external_ip: true              # advertise the public IP in SDP/Contact (via STUN)
# nat_1_to_1_ip: 203.0.113.10      # explicit public IP instead; CANNOT be combined with use_external_ip
# media_nat_1_to_1_ip / media_use_external_ip   # separate public IP for media only
# local_net: 10.0.0.0/16           # private network to treat as local
# listen_ip: 0.0.0.0
health_port: 8080
prometheus_port: 9090
logging:
  level: info
# tls:                             # SIP over TLS (default port 5061)
#   port: 5061
#   certs:
#     - cert_file: /certs/sip.crt
#       key_file: /certs/sip.key
# sip_hostname: sip.example.com    # hostname used in SIP headers
# media_timeout: 15s / media_timeout_initial: 30s   # drop calls with no RTP (VERIFY defaults against config.go)
# max_active_calls / max_cpu_utilization (default 0.9)   # routing across several SIP instances
# sip_trunk_ids: [ST_xxx]          # pin an instance to specific trunks
# codecs: { G722: false }          # enable/disable codecs (VERIFY key names against your version)
```

- Run with host networking (the RTP range is too large to publish through Docker's proxy): `docker run --rm --network host -e SIP_CONFIG_BODY="$(cat sip.yaml)" livekit/sip`, or natively `livekit-sip --config=sip.yaml`. `SIP_CONFIG_FILE` also works.
- `use_external_ip` and `nat_1_to_1_ip` are mutually exclusive; the service refuses to start with both (`config.go` validation). The same applies to `media_use_external_ip` and `media_nat_1_to_1_ip`.
- **SIP URI for the carrier:** `sip:<public-ip-or-dns>:5060;transport=tcp` (or `:5061;transport=tls`). There is no `*.sip.livekit.cloud` hostname when self-hosting.
- Upgrades: server, sip and egress share the Redis psrpc bus. Roll them together. Enabling `psrpc.compression` requires every node (server, sip, egress, ingress, agent workers) on psrpc v0.7.6+ first, or messages are silently dropped.
- Template: `templates/selfhost/sip.yaml`.

## 5. Ports and firewall

| Port | Proto | Service | Open to |
|---|---|---|---|
| 443 | tcp | TLS proxy -> livekit-server 7880 (signalling, API) | Agent workers, `lk`, clients, your backend |
| 7881 | tcp | livekit-server ICE/TCP fallback | Agent workers and clients |
| 50000-60000 | udp | livekit-server ICE media | Agent workers and clients |
| 3478 | udp | TURN (optional) | Browser/app clients only |
| 5349 or 443 | tcp | TURN/TLS (optional) | Browser/app clients only |
| 5060 | udp **and** tcp | livekit/sip signalling | Carrier signalling IPs |
| 5061 | tcp | livekit/sip TLS (optional) | Carrier signalling IPs |
| 10000-20000 | udp | livekit/sip RTP | Carrier **media** IPs (often a different pool from signalling) |
| 6379 | tcp | Redis | Private network only |
| 8080 / 9090 / 8081 | tcp | health / Prometheus / agent health | Private network only |

Allowlist carrier IP ranges rather than opening SIP to the world. Examples from `06`: Exotel signalling from `35.154.174.161`, `98.130.5.99`, `129.154.231.198` with media on UDP 10000-40000 from listed pools; Twilio, Telnyx and Plivo publish their own ranges. An open 5060 attracts SIP scanners within hours; rely on trunk auth (`auth_username`/`auth_password` or `allowed_addresses`, which self-hosted SIP enforces itself without a support ticket) as well as the firewall.

## 6. NAT and firewall gotchas

1. **INVITE succeeds, no audio or one-way audio:** the SDP advertised a private IP, or the RTP range is blocked. Set `use_external_ip: true` or `nat_1_to_1_ip`, and open 10000-20000/udp from the carrier's media IPs. Check with `tcpdump -n udp portrange 10000-20000` during a call.
2. **Call drops after ~30 s, or 15 s after audio stops:** RTP timeout. Usually NAT or firewall dropping media (the carrier stops receiving, or you do). Exotel's own troubleshooting lists the same symptom.
3. **No bridge networking.** Run `livekit/sip` (and ideally livekit-server) with host networking. Docker's userland proxy cannot publish 10,000 UDP ports sanely, and an extra NAT layer breaks SDP addresses.
4. **No private Kubernetes clusters with extra NAT** for SIP or the SFU. Use `hostNetwork: true` on nodes with public IPs.
5. **TCP vs UDP:** open 5060 on both. Many carriers (Twilio origination with `;transport=tcp`, Exotel which has no UDP at all) use TCP.
6. **Keep the RTP ranges apart:** SIP 10000-20000 and the SFU 50000-60000 do not collide by default; keep it that way if both run on one host.
7. **Agent workers must reach the SFU's ICE ports**, not just 443. A worker that connects signalling over 443 but cannot reach 50000-60000/udp or 7881/tcp joins the room and then hears nothing. If workers run in the same VPC and cannot hairpin to the node's public IP, look at `rtc.advertise_internal_ip` (advertises internal candidates alongside the mapped external IP) or a private `node_ip`. VERIFY on your network.
8. **REGISTER-only carriers:** LiveKit SIP does not register. Put Asterisk or FreeSWITCH in front (see `06`).
9. **Clock and DNS:** TLS trunks and LiveKit tokens fail on skewed clocks. Run NTP.
10. **SIP ALG:** disable SIP ALG on any firewall or router in the path; it rewrites SDP and breaks media.

## 7. Egress

Source: https://docs.livekit.io/transport/self-hosting/egress.md, README https://github.com/livekit/egress

```yaml
api_key: APIxxxxxxxxxxxx
api_secret: <secret>
ws_url: ws://127.0.0.1:7880
redis:
  address: 127.0.0.1:6379
health_port: 8080
prometheus_port: 9091           # exposes availability for autoscaling
storage:                        # one provider; can be overridden per request
  gcp:
    credentials_json: ""        # empty -> GOOGLE_APPLICATION_CREDENTIALS
    bucket: my-call-recordings
  # s3: { access_key, secret, region, endpoint, bucket }
  # azure: { account_name, account_key, container_name }
```

- Run: `docker run --rm -e EGRESS_CONFIG_FILE=/out/egress.yaml -v $PWD:/out livekit/egress` (or `EGRESS_CONFIG_BODY`). Helm: `helm install egress livekit/egress -n livekit -f values.yaml`.
- Conflict between sources: older docs say Chrome-based egress needs `--cap-add SYS_ADMIN` (since v1.7.6). The current README says Room Composite and Web egress run with Chrome sandboxing **disabled** by default, and sandboxing can be re-enabled with `enable_chrome_sandbox: true` plus a seccomp profile. Audio-only SDK egress (the stereo call recording in `04` section 13.2) does not launch Chrome. If Chrome egress fails to start, add `SYS_ADMIN` and check the current docs.
- The new `StartEgress` API needs livekit-server v1.13.5 or newer.
- Give each instance about 4 CPU / 4 GB. Room composite with Chrome uses 2-6 CPU and records one room per instance; audio-only SDK egress is light.
- Error `"no response from egress service"` means the server and egress do not share the same Redis.
- Alternative with no egress: the agent-side recorder (`session.start(record=...)`) is a LiveKit Cloud observability feature; when self-hosting, record inside the agent yourself or use egress.

## 8. Docker Compose and Kubernetes

### 8.1 Local development (server + SIP + Redis)

From https://github.com/livekit/sip/blob/main/docker-compose.yaml: Redis, `livekit-server --dev --redis-host localhost:6379` and `livekit/sip` with `SIP_CONFIG_BODY`, all with `network_mode: host`. Dev keys `devkey`/`secret`. Point a softphone or a carrier test number at `sip:<your-ip>:5060`.

### 8.2 Single-VM production

`templates/selfhost/docker-compose.yml` runs Redis, livekit-server, livekit/sip, Caddy for TLS and, under a profile, egress and an agent worker. Host networking for server and SIP. Use a compute-optimised VM in the carrier's region (for India, `asia-south1` or `ap-south-1`) with a static public IP.

### 8.3 Kubernetes

Source: https://docs.livekit.io/transport/self-hosting/kubernetes.md

- `helm repo add livekit https://helm.livekit.io` then `helm install lk livekit/livekit-server -n livekit -f values.yaml` (template `server-sample.yaml` in livekit/livekit-helm). Charts exist for `livekit-server`, `egress` and `ingress`.
- The server chart uses host networking, one LiveKit pod per node, and a long `terminationGracePeriodSeconds` (5 h) so rooms drain.
- Supported: GKE, EKS (with ALB), DOKS. **Not supported:** serverless (Fargate, Autopilot-style) and private clusters with extra NAT.
- **There is no official SIP chart.** Run `livekit/sip` as your own DaemonSet or Deployment with `hostNetwork: true`, `dnsPolicy: ClusterFirstWithHostNet`, pinned to nodes with public IPs, and the config in a Secret mounted as `SIP_CONFIG_BODY`.
- Agent workers are an ordinary Deployment (no inbound ports). Autoscale on CPU or the worker load metric, scale up early (0.5 when the worker threshold is 0.7), scale down slowly, and give `terminationGracePeriodSeconds` of 10+ minutes so calls finish.

### 8.4 Multi-node and multi-region

Redis is the bus. Use `node_selector: {kind: regionaware, sysload_limit: 0.5, regions: [...]}` with geo-DNS. A room must fit on one node. SIGTERM drains a node. Source: https://docs.livekit.io/transport/self-hosting/distributed.md

## 9. Agent workers against your server

```bash
export LIVEKIT_URL=wss://livekit.example.com LIVEKIT_API_KEY=APIxxxxxxxxxxxx LIVEKIT_API_SECRET=...
python agent.py download-files     # once, at image build
python agent.py start              # production: prewarmed idle processes, drains on SIGTERM, health on :8081
python agent.py dev                # auto-reload, debug logs
python agent.py console            # local mic/speaker test with no server
```

- The same `templates/livekit/agent_gemini_live.py` runs unchanged; set `NOISE_CANCELLATION=none` because BVCTelephony authenticates against LiveKit Cloud and does not work on a self-hosted room.
- `AgentServer` knobs (verified in 1.7.1 and 1.8.8): `num_idle_processes` (production default: CPU count), `load_fnc`/`load_threshold` (default CPU load, 0.7), `initialize_process_timeout=10.0`, `shutdown_process_timeout`, `session_end_timeout`, `drain_timeout`, `job_memory_warn_mb`, `job_memory_limit_mb`, `host`, `port` (8081 in production), `prometheus_port`.
- Workers need no inbound ports, but they do need outbound access to the SFU's ICE ports (section 6, item 7) and to Gemini (`aiplatform.us.rep.googleapis.com:443`).
- Google credentials: a mounted service-account file or workload identity; the template's `resolve_google_credentials()` searches the usual mount paths.

## 10. Trunks and dispatch rules with lk

The same JSON and commands as Cloud (`templates/sip/`), pointed at your server:

```bash
lk project add selfhost --url https://livekit.example.com --api-key APIxxxxxxxxxxxx --api-secret ... --default
lk sip inbound create templates/sip/inbound_trunk.json
lk sip dispatch create templates/sip/dispatch_rule.json        # room_config.agents for explicit dispatch
lk sip outbound create templates/sip/outbound_trunk.json --auth-user "$SIP_USER" --auth-pass "$SIP_PASS"
lk sip inbound list ; lk sip dispatch list ; lk room list
```

- `destination_country` and region-pinned hostnames are Cloud routing features. On self-hosted, outbound calls leave from your SIP host; set `address` to the carrier's regional edge.
- `allowed_addresses` and digest auth on inbound trunks are enforced by your SIP service; no support ticket is needed.
- `krisp_enabled` on trunks does nothing useful without Cloud.

## 11. What you lose vs Cloud

| Capability | Self-hosted | Cloud |
|---|---|---|
| Managed agent hosting (`lk agent create/deploy`, rollback, secrets, log drains) | No; run your own k8s or VMs | Yes |
| LiveKit Inference (`inference.RealtimeModel("google/gemini-3.8-live")`, STT/TTS/LLM gateway) | No; use the Google plugin with your Vertex project (this skill's default anyway) | Yes |
| Agent observability (recordings, transcripts, traces, Agent Insights, simulations) | DIY via OpenTelemetry hooks and your own recorder or egress | Yes, 30-day retention |
| Enhanced noise cancellation (Krisp NC/BVC/BVCTelephony) | Only with your own Krisp SDK license (`krisp.auth.krisp_license(...)` in `livekit-plugins-krisp`, needs a proprietary `krisp-audio` wheel) or ai-coustics with your own key. Telephony NC is listed as N/A | Included, metered |
| Phone numbers, trunk-side Krisp, region-pinned SIP endpoints, static IP ranges, Twilio/WhatsApp connectors, custom SIP domains | No | Yes |
| Global mesh SFU, 99.99% target, Analytics API | No (single-home SFU; a room lives on one node) | Yes |
| SIP, egress, ingress, agents framework, webhooks, E2EE | Yes (deploy SIP, egress, ingress separately; Redis required) | Yes |

Source: https://docs.livekit.io/transport/self-hosting.md (comparison tables).

What you own instead: Redis HA, TLS and TURN certificates, UDP firewall and NAT config (the most common failure source), a static SIP public IP, egress pods, agent autoscaling and draining, noise-cancellation licensing, and coordinated upgrades of server, sip and egress.

## 12. Sizing

- **livekit-server:** CPU- and bandwidth-bound. Prefer compute-optimised instances, high-bandwidth NICs and host networking. A phone call is one audio track each way, so SFU load per call is small compared with video.
- **livekit/sip:** transcodes G.711 to Opus and back per call. No official per-call CPU figure was found. Use `max_cpu_utilization` (default 0.9) and `max_active_calls` to cap an instance, load-test with your carrier's codec, and run at least two instances for redundancy (carriers support multiple origination URIs with priority).
- **Agent workers:** LiveKit's guidance is 4 cores / 8 GB per worker for 10-25 concurrent jobs; their load test ran 30 sessions on 4 cores / 8 GB at peak ~3.8 cores / 2.8 GB (https://docs.livekit.io/deploy/custom/deployments.md). A Gemini Live agent does no local STT/TTS, but Silero VAD, resampling and (on Cloud) BVC run per call. Measure your own.
- **Egress:** about 4 CPU / 4 GB per instance; one Chrome room composite per instance; audio-only SDK egress is light.
- **Redis:** small; make it highly available because SIP, egress and dispatch all depend on it.
- **Gemini quotas** are usually the real ceiling: Vertex PayGo documents 1,000 concurrent Live sessions per project (an older page says 5,000). Check the project's Quotas page.

## 13. Bring-up and verification checklist

1. DNS: `livekit.example.com` (and `turn.example.com` if used) point to the VM's static IP. Certificates issued.
2. `curl https://livekit.example.com` returns `OK`. `lk room list` works with your keys.
3. `redis-cli ping` from the server, SIP and egress hosts.
4. SIP service logs show it registered with the server over Redis. `curl localhost:8080` (SIP health) is OK.
5. From outside: SIP OPTIONS to `sip:<public-ip>:5060` gets a response (e.g. `sipsak -s sip:<public-ip>:5060` or `sipvicious`-style tools against your own host only).
6. Create the inbound trunk and dispatch rule; start an agent worker (`python agent.py dev`) and confirm it registers.
7. Place a test call. Check `lk room list` for the new `call-...` room, the SIP participant, and the agent participant.
8. Audio both ways. If one-way: SDP IP (`use_external_ip`/`nat_1_to_1_ip`), RTP range, SIP ALG.
9. Hang up from each side; confirm the room is deleted and the call ends at the carrier.
10. Load-test concurrent calls at your expected peak before go-live.
