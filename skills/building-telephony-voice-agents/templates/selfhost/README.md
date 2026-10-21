# Self-hosted LiveKit for phone agents (single VM)

Docker Compose bundle for running LiveKit yourself with SIP: Redis, `livekit-server`, `livekit/sip`, Caddy for TLS, and optional egress and agent-worker profiles. Background, trade-offs and Kubernetes notes are in `references/05-livekit-self-hosted.md`; read its section 1 before choosing self-hosting over LiveKit Cloud.

Status: config keys were checked against upstream sources and image tags against Docker Hub on 2026-10-21, and `docker compose config` validates the file. The bundle has **not** been run end to end for this skill. Follow the verification checklist below on first bring-up.

| File | Purpose |
|---|---|
| `docker-compose.yml` | Services; host networking for everything that carries media |
| `livekit.yaml` | livekit-server: ports, external IP, Redis, API keys, TURN off by default |
| `sip.yaml` | livekit/sip: SIP 5060 udp+tcp, RTP 10000-20000, public IP advertisement |
| `egress.yaml` | Optional recording service with GCS/S3 storage |

## 1. VM and network

- One compute-optimised VM in the carrier's region (India: GCP `asia-south1` or AWS `ap-south-1`), Ubuntu/Debian, Docker with the compose plugin.
- A **static public IP** (the carrier allowlists it, and SDP must advertise it).
- DNS: `livekit.example.com` -> the static IP (Caddy obtains the certificate). Replace `livekit.example.com` in `docker-compose.yml`.
- Disable SIP ALG on any firewall or router in the path.
- NTP running (TLS trunks and tokens fail with clock skew).

## 2. Firewall

| Port | Proto | Purpose | Source |
|---|---|---|---|
| 80, 443 | tcp | Caddy (ACME + wss to livekit-server) | Anywhere (or your workers/backends) |
| 7881 | tcp | ICE/TCP fallback | Agent workers, WebRTC clients |
| 50000-60000 | udp | ICE media | Agent workers, WebRTC clients |
| 5060 | udp **and** tcp | SIP signalling | Carrier signalling IPs only |
| 5061 | tcp | SIP over TLS (if enabled) | Carrier signalling IPs only |
| 10000-20000 | udp | SIP RTP media | Carrier media IPs only (often a different pool) |
| 6379, 7880, 8080-8082, 9090-9091 | tcp | Redis, raw API, health, metrics | **Closed** (localhost/private only) |

GCP example:
```bash
gcloud compute firewall-rules create lk-web   --allow tcp:80,tcp:443 --target-tags=livekit
gcloud compute firewall-rules create lk-ice   --allow tcp:7881,udp:50000-60000 --target-tags=livekit
gcloud compute firewall-rules create lk-sip   --allow tcp:5060,udp:5060,tcp:5061 --source-ranges=<carrier-signalling-cidrs> --target-tags=livekit
gcloud compute firewall-rules create lk-rtp   --allow udp:10000-20000 --source-ranges=<carrier-media-cidrs> --target-tags=livekit
```
Carrier ranges: see `references/06-sip-trunking-providers.md` (for example, Exotel signalling from `35.154.174.161`, `98.130.5.99`, `129.154.231.198` and media on UDP 10000-40000 from listed pools).

## 3. Configure

1. Generate one API key pair and put it in `livekit.yaml` (`keys`), `sip.yaml` and `egress.yaml`:
   ```bash
   echo "API$(openssl rand -hex 6)"; openssl rand -base64 32
   ```
2. `sip.yaml`: keep `use_external_ip: true`, or set `nat_1_to_1_ip: <static-ip>` instead (never both).
3. Optional egress: set the bucket in `egress.yaml`, mount the service-account key in `docker-compose.yml`.
4. Optional agent: `cp ../livekit/env.example ../livekit/.env.local` and fill in Google settings and `AGENT_NAME`. The compose file overrides `LIVEKIT_URL`, sets `NOISE_CANCELLATION=none` (BVC needs LiveKit Cloud) and `RECORD_SESSION=0` (the agent recorder is a Cloud observability feature; use egress here). Put the same `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` in `.env.local`.

## 4. Start

```bash
docker compose up -d
docker compose --profile egress up -d            # optional
docker compose --profile agent up -d --build     # optional; workers can also run elsewhere
docker compose logs -f sip livekit
```

## 5. Trunks and dispatch rule

```bash
lk project add selfhost --url https://livekit.example.com --api-key APIxxxxxxxxxxxx --api-secret <secret> --default
lk sip inbound create ../sip/inbound_trunk.json
lk sip dispatch create ../sip/dispatch_rule.json
lk sip outbound create ../sip/outbound_trunk.json --auth-user "$SIP_USER" --auth-pass "$SIP_PASS"   # optional
```
Carrier origination URI: `sip:<static-ip-or-dns>:5060;transport=tcp` (or `:5061;transport=tls`). There is no `*.sip.livekit.cloud` hostname or `destination_country` routing when self-hosting.

## 6. Verification checklist

1. `curl -s https://livekit.example.com` returns `OK`; `lk room list` succeeds.
2. `docker compose exec redis redis-cli ping` returns `PONG`.
3. `curl -s localhost:8080` (SIP health) succeeds and the SIP logs show no Redis or auth errors.
4. From another machine, send SIP OPTIONS to your own host (`sipsak -s sip:<static-ip>:5060`) and get a response.
5. The agent worker logs show it registered (`registered worker`), and `live warmup: ok=True` appears.
6. Call the DID. `lk room list` shows `call-...`; `lk room participants list call-...` shows the SIP participant and the agent.
7. Two-way audio. One-way or none: check the SDP IP (`use_external_ip`/`nat_1_to_1_ip`), the RTP firewall rule, SIP ALG; capture with `sudo tcpdump -ni any udp portrange 10000-20000`.
8. Call drops after about 30 s, or 15 s after audio stops: RTP timeout, almost always NAT or firewall.
9. Hang up from each side; the room disappears and the carrier shows a normal hang-up.
10. Load-test concurrent calls at expected peak (SIP CPU, worker CPU, Gemini quota) before go-live.

## 7. Going further

- Kubernetes: official Helm charts exist for `livekit-server`, `egress` and `ingress`, not for SIP. Run `livekit/sip` as a DaemonSet with `hostNetwork: true` on public-IP nodes. Details in `references/05-livekit-self-hosted.md` section 8.3.
- High availability: two SIP instances (carrier origination URIs with priorities), Redis with replication, workers on separate VMs autoscaled on CPU with a 10+ minute termination grace period.
- Upgrades: server, sip and egress share the Redis psrpc bus; roll them together and read release notes for psrpc changes.
