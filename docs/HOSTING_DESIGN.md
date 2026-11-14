# MCP Foundry — Phase 3: Managed Hosting (design)

Status: **design / not built.** This doc exists to make the decisions *before* writing the
subsystem, because managed hosting is the one remaining item that's days of work + ongoing infra
cost + real security exposure. Everything else (auth, persistence, rate-limit, private repos,
deploy) is already done.

---

## 1. Goal & user story

Today Foundry hands you generated code you run yourself. Managed hosting closes the loop:

> User curates → clicks **"Host it"** → Foundry deploys the generated MCP server to a live URL →
> hands back a one-click **Connect to Claude/Cursor** snippet. Nothing runs on the user's machine.

Success = an agent can call the hosted tools over the internet, the user never touched a terminal,
and one tenant's server can't see another's data, secrets, or network.

## 2. What makes this hard (the real constraints)

1. **It may run untrusted code.** A generated *in-process* server imports the user's repo — i.e.
   we'd execute arbitrary code on our infra. That's the single biggest risk and it drives the whole
   architecture. (An *http-proxy* server only makes outbound calls — far safer. See §3.3.)
2. **Transport.** Generated servers use **stdio** (local). Hosting needs a **network** transport
   (MCP **Streamable HTTP**). The generator needs a "remote mode."
3. **Secrets.** Hosted servers need the target's credentials (e.g. `CYBERPANEL_ADMIN_PASS`). Must be
   encrypted at rest, injected at runtime, never logged, scoped to one server.
4. **Endpoint auth.** A public MCP URL needs its own auth (bearer/OAuth) — otherwise anyone who
   learns the URL can drive the user's API.
5. **Lifecycle & cost.** Deploy, start, **scale-to-zero when idle**, redeploy on repo change, logs,
   limits. Without scale-to-zero, every hosted server burns money 24/7.
6. **Egress abuse.** A hosted server that proxies arbitrary URLs is an SSRF/abuse vector from inside
   our network — outbound must be locked to the declared target.

## 3. Key architectural decisions

### 3.1 Transport: stdio → Streamable HTTP
Add a **remote transport mode** to `engine/generator.py`: emit a server that runs FastMCP with
`transport="streamable-http"` (Python) / the HTTP transport (TS), exposing `/mcp`. Claude and Cursor
connect to a remote MCP **URL** (with a token / OAuth). This is the smallest, safest first step and
is fully testable locally — **do it first (Phase 3.0).**

### 3.2 Runtime unit: container-per-server, scale-to-zero
| Option | Isolation | Scale-to-zero | Ops | Verdict |
|---|---|---|---|---|
| Shared in-process (many servers in one process) | ❌ none | n/a | easy | **reject** (untrusted code) |
| **Container per server** (Fly Machines / Cloud Run) | ✅ strong | ✅ yes | medium | **recommended** |
| MicroVM per server (Firecracker) | ✅✅ strongest | ✅ | hard | overkill for v1 |
| Serverless fn per call | partial | ✅ | awkward fit for MCP sessions | later, maybe |

**Recommendation: one container per hosted server on a scale-to-zero platform** (Fly Machines or
Google Cloud Run). Idle servers cost ~nothing; isolation is per-container; ops are manageable.

### 3.3 The big fork: in-process vs http-proxy hosting (security)
This is the most important decision and it *simplifies everything*:

- **Host http-proxy servers only (v1).** These just make outbound HTTP calls to the user's API — we
  are **not executing their repo code**. Isolation burden drops to "lock egress + store secrets."
- **In-process servers → don't host in v1** (they execute arbitrary repo code). Offer them as
  *download-and-self-host* only, or host later behind a hardened sandbox (gVisor/Firecracker, no
  secrets, no network beyond target).

→ **v1 managed hosting = http-proxy servers + a locked-down container.** This is dramatically more
tractable and safe than "run anyone's code."

## 4. Security model
- **Isolation:** one container per server; no shared FS; no access to the control plane or other
  tenants. Non-root, read-only rootfs, CPU/mem/time limits.
- **Egress allow-list:** outbound restricted to the server's declared target host(s). Blocks
  using our infra for SSRF/abuse.
- **Secrets:** stored encrypted (platform secret store / KMS), injected as env at boot, never in
  logs or images. One secret scope per server.
- **Endpoint auth:** each hosted MCP gets its own bearer token (rotatable); the connect snippet
  embeds it. Optionally OAuth for Claude connectors.
- **Abuse controls:** per-server rate limits + concurrency caps; audit log of tool calls.

## 5. Data model & control plane
Reuse the existing Postgres. New table `deployments`:
`id, owner, analysis_id, transport, status, endpoint_url, secret_ref, token_hash, target_hosts[],
created_at, updated_at, last_active_at`.

Control-plane API (authenticated, owner-scoped):
`POST /api/host` (deploy an analysis) · `GET /api/host/{id}` (status) · `GET /api/host/{id}/logs` ·
`POST /api/host/{id}/rotate-token` · `POST /api/host/{id}/redeploy` · `DELETE /api/host/{id}`.

## 6. Lifecycle & ops
- **Deploy:** build/start a container from a generic runner image + the generated server + injected
  secrets; register the endpoint + token.
- **Scale-to-zero:** stop on idle (`last_active_at`), cold-start on first call.
- **Redeploy:** on user action or a source-repo webhook (regenerate → roll the container).
- **Observability:** per-server logs + basic metrics (calls, errors, latency); health checks.
- **Versioning:** keep the last-good image; one-click rollback.

## 7. Cost model (rough)
- **Idle:** ~$0 with true scale-to-zero (Fly Machines stopped / Cloud Run min-instances=0).
- **Active:** per-second compute (a tiny 256MB container is cheap); egress; secret storage.
- **Control plane:** the existing app + Postgres.
- Watch-items: cold-start latency vs. keep-warm cost; a runaway agent looping calls (mitigated by
  per-server rate limits + budget caps).

## 8. Buy vs build
Hosting remote MCP servers is increasingly a solved commodity — consider **renting the runtime**
instead of building a control plane on day one:
- **Cloud Run / Fly Machines** — you still build the control plane, but get scale-to-zero + isolation.
- **Remote-MCP platforms** (e.g. Cloudflare's remote-MCP support, Smithery, mcp.run, Vercel) — may
  host the server for you; least to build, least control, a dependency.
- **Build-your-own orchestrator** — most control, most work; only if hosting *is* the product.

**Recommendation:** start on **Cloud Run or Fly Machines** (rent the runtime, own the thin control
plane). Re-evaluate a dedicated orchestrator only if scale demands it.

## 9. Phased plan (shippable milestones)
- **3.0 — Remote transport (small, do first).** Generator emits a Streamable-HTTP server +
  bearer-token auth; verify locally with a remote MCP client. *No hosting yet — just makes servers
  hostable. Low risk, high leverage.*
- **3.1 — "Bring your own host."** Per-server Dockerfile + a one-click "Deploy to Fly/Railway/Cloud
  Run" (config + button). User hosts on their account; we don't run code yet. Ships value, zero
  custodial risk.
- **3.2 — Managed hosting (the real thing).** Control plane + container-per-server on Cloud
  Run/Fly, secret storage, endpoint auth, egress lock-down, scale-to-zero. **http-proxy servers
  only.**
- **3.3 — Polish.** Logs/metrics UI, redeploy-on-change, token rotation, in-process sandboxing
  (if ever), multi-region.

## 10. Risks & open questions
- **Untrusted code (in-process):** deferred by hosting http-proxy only in v1. Revisit with a real
  sandbox before ever hosting in-process.
- **Egress abuse:** must lock outbound per-server from day one of 3.2.
- **Cost runaway:** scale-to-zero + per-server budget/rate caps are non-optional.
- **Cold starts:** acceptable for tool calls? measure in 3.2.
- **Open:** Cloud Run vs Fly Machines (latency/scale-to-zero behaviour, secret model)? OAuth vs
  bearer for endpoint auth? per-user host quotas / billing tie-in?

## 11. Recommendation
1. **Build 3.0 now** (remote-transport generator mode) — small, safe, testable, and it's the
   prerequisite for everything else. It also immediately lets power users self-host a *networked*
   MCP.
2. **Then 3.1** (one-click deploy to the user's own host) — real value, no custodial code-exec risk.
3. **Treat 3.2 (full managed) as a project of its own** — pick Cloud Run vs Fly, build the thin
   control plane, **http-proxy servers only**, lock egress, scale-to-zero.

The throughline: **don't run untrusted code to start.** Ship remote transport → self-host →
managed http-proxy hosting, in that order, and the scary parts stay deferred until they're worth it.
