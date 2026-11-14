# MCP Foundry — roadmap

## v1 (this build) — the core loop, runnable locally
- [x] Discovery engine (FastAPI / Flask / Django / plain-Python) — `engine/discovery.py`
- [x] FastMCP code generator (http-proxy + in-process) — `engine/generator.py`
- [x] FastAPI backend: `/api/analyze` (GitHub URL or zip upload), `/api/generate`, `/api/download`
- [x] Single-page UI: input → curate (risk-flagged, destructive opt-in) → generate → download + connect snippet

Single-tenant, no auth. Generates code you run yourself.

## Hardening pass (done) — `app/guards.py`, `app/store.py`, `tests/test_guards.py`
- [x] SSRF-safe clone — blocks localhost / private / link-local / cloud-metadata (live-tested)
- [x] Zip-bomb + zip-slip safe extraction; upload-size + extracted-size + file-count limits
- [x] Repo-size limit after clone; clone timeout (env-tunable)
- [x] Temp-dir leak fixed: bounded, TTL'd analysis store with on-evict cleanup
- [x] Clean generated tool names (`get_health`, not `health_health_get`)
- [x] Guard/store test suite (20 checks) + e2e regression verified

## Phase 2 — public product
- [x] Accounts + auth — Supabase, gated (open in dev, enforced when configured); JWT-verified
      route protection + owner-scoped store + magic-link login overlay — `app/auth.py`
- [x] Persistent store — SQL-backed (`DATABASE_URL` → Postgres, else in-memory); survives
      restarts, owner-scoped, multi-worker-safe for analyze/generate/download — `app/store.py`
      (`SqlAnalysisStore`). Report stays node-local (reads cloned repo). Per-user *history UI* still TODO.
- [x] Private repos (token) — per-request token or server `GITHUB_TOKEN`; token kept out of the
      clone URL logs and scrubbed from errors — `app/guards.py` (with_token/redact) + UI token field
- [x] "Sign in with GitHub" OAuth — gated; CSRF state; GitHub token stored server-side (never to
      the browser); flow verified live (login → 302 to GitHub w/ state). Activate by setting
      GITHUB_CLIENT_ID/SECRET + SESSION_SECRET — `app/github_oauth.py`. (Callback code-exchange
      needs a real registered app to exercise end-to-end.)
- [x] Rate-limiting middleware — token-bucket per client IP on analyze/generate/download/report
      (429 + Retry-After), env-tunable — `app/ratelimit.py` (per-node; Redis → global is a later step)

## Phase 3 — managed hosting  (design: `docs/HOSTING_DESIGN.md`)
- [x] **3.0 Remote transport** — generated servers run over **Streamable HTTP** (`MCP_TRANSPORT=http`)
      with **bearer-token auth** (`MCP_SERVER_TOKEN`); endpoint at `/mcp`. Verified with a live remote
      MCP client (initialize + tools/list). Still stdio by default. — `engine/generator.py`
- [x] **3.1 Self-host** — every bundle now ships a `Dockerfile` + `fly.toml` (scale-to-zero); deploy
      to Docker / Fly / Railway / Render / Cloud Run in one command (HTTP mode + bearer token). README
      documents it. Zero custodial code-exec risk — the user runs it on their own host.
- [x] 3.2 Managed hosting — **control plane + local-docker deploy VERIFIED LIVE** (`app/hosting.py`):
      gating, **http-proxy-only guard** (no untrusted code-exec), pluggable `Deployer`, per-server
      bearer token (stored hashed), owner-scoped registry, `/api/host` deploy/list/destroy.
      **Full round-trip proven end-to-end:** analyze → host (real `docker build` + `docker run`) →
      remote MCP `tools/list` over the hosted endpoint (401 without token) → DELETE removes the
      container. 19/19 unit tests + live e2e.
- [x] Deployment registry **persisted to SQL** (`SqlDeploymentRegistry`; `DATABASE_URL`→Postgres,
      else in-memory; survives restarts so containers aren't orphaned; owner-scoped) — 9/9 tests
      incl. cross-restart persistence.
- [x] Egress lock-down (app-layer) — generated http-proxy servers enforce a host **allowlist**
      (`MCP_EGRESS_ALLOW`, default = target host) + `follow_redirects=False` before every outbound
      call; threaded through `/api/host`. Runtime-verified: blocks non-allowed hosts, permits the
      target — `engine/egress.py`, 9/9 tests.
- [ ] 3.2b Fly path live (per-server image build/push via remote builder + Machines API — needs a
      Fly account); container/network-level egress policy (the infra complement to the app guard).
- [ ] 3.3 Logs/metrics UI, redeploy-on-change, token rotation, in-process sandboxing

## Phase 4 — reach + quality
- [x] OpenAPI/Swagger ingestion (the deterministic "seconds" path) — `engine/openapi_discovery.py`
- [ ] More inputs: live API endpoint, Node/TS, CLI tools
- [ ] LLM-assisted discovery for messy/undocumented code (better summaries, param inference)
- [ ] Billing + plans

## Known v1 limitations (honest)
- Discovery is AST-based: Django/Flask JSON request bodies aren't deep-parsed (path/url params only); FastAPI Pydantic bodies are.
- `tools/list` is verifiable without the target running; live tool calls need the target API reachable / the code importable.
- One generation `invocation` mode per server (http OR in_process), not mixed per-tool yet.
