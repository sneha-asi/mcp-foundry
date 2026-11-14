# Deploying MCP Foundry

A container image is provided. It runs the FastAPI app with `git` installed (needed by the
analyze endpoint) as a non-root user.

## Build & run (Docker)
```bash
docker build -t mcp-foundry .
docker run --rm -p 8099:8099 mcp-foundry
# open http://127.0.0.1:8099
```

## Or with compose
```bash
docker compose up --build
```

## Configuration (env vars)
| Var | Default | Purpose |
|---|---|---|
| `MCPF_MAX_UPLOAD_MB` | 50 | max uploaded zip size |
| `MCPF_MAX_EXTRACTED_MB` | 200 | zip-bomb cap (uncompressed) |
| `MCPF_MAX_REPO_MB` | 200 | max cloned repo size |
| `MCPF_MAX_FILES` | 20000 | max files in an upload |
| `MCPF_CLONE_TIMEOUT` | 120 | `git clone` timeout (s) |
| `MCPF_ANALYSIS_TTL` | 3600 | analysis retention (s) before temp-dir cleanup |
| `MCPF_MAX_ANALYSES` | 200 | max concurrent analyses kept in memory |
| `ANTHROPIC_API_KEY` | — | optional; enables the AI deep-report mode |
| `SUPABASE_URL` | — | enables auth; your Supabase project URL (public) |
| `SUPABASE_ANON_KEY` | — | Supabase anon/public key (public, sent to browser) |
| `SUPABASE_JWT_SECRET` | — | **secret**; Supabase JWT secret — server-side only. Setting this turns auth ON |
| `DATABASE_URL` | — | Postgres connection string; setting it turns on persistent, multi-worker storage |
| `WEB_CONCURRENCY` | 1 | uvicorn workers; raise only when `DATABASE_URL` is set |
| `MCPF_RL_ANALYZE` | 10/60 | rate limit for `/api/analyze` — `count/seconds` |
| `MCPF_RL_API` | 60/60 | rate limit for generate / download / report |
| `MCPF_TRUST_PROXY` | — | set `1` behind a proxy/LB to trust `X-Forwarded-For` for the client IP |
| `GITHUB_TOKEN` | — | optional server-wide token to clone private repos (users can also paste one per request) |
| `GITHUB_CLIENT_ID` | — | enables "Sign in with GitHub" (OAuth app client id) |
| `GITHUB_CLIENT_SECRET` | — | **secret**; OAuth app client secret — server-side only |
| `GITHUB_REDIRECT_URI` | (derived) | OAuth callback; defaults to `<origin>/auth/github/callback` |
| `HOSTING_BACKEND` | — | managed hosting: `local-docker` or `fly` (unset = disabled, `/api/host` → 503) |
| `FLY_API_TOKEN` / `FLY_ORG` | — | Fly Machines deploys (when `HOSTING_BACKEND=fly`) |
| `MCPF_RUNNER_IMAGE` | — | built/pushed per-server image for Fly deploys (the image-build step) |
| `SESSION_SECRET` | (random/dev) | signs the session cookie; **set in prod** so sessions survive restarts/replicas |

## "Sign in with GitHub" (OAuth)
Off until `GITHUB_CLIENT_ID` + `GITHUB_CLIENT_SECRET` are set. To enable: GitHub → Settings →
Developer settings → **OAuth Apps** → New → callback `<origin>/auth/github/callback` → copy the
Client ID + generate a secret. Set those + `SESSION_SECRET`. The user's GitHub token is stored
**server-side** (a per-process vault keyed by the signed session cookie) — never sent to the
browser. Verified: with creds set, `/auth/github/login` redirects to GitHub with a CSRF `state`;
without creds it 404s and the manual token field is used instead.

## Auth (Supabase)
Auth is **off by default** (open mode) and turns **on** when `SUPABASE_JWT_SECRET` is set: every
API route then requires a valid Supabase access token, and the browser shows a magic-link login.
To enable: create a Supabase project → copy URL + anon key + JWT secret (Settings → API) into the
env above. Verified: with the secret set, unauthenticated `/api/analyze|generate|report` return
401; `/api/health` stays public.

## Persistence & workers
The store auto-selects (`app/store.py`):
- **No `DATABASE_URL`** → in-memory (single-node). Run **one worker** (`WEB_CONCURRENCY=1`, the default).
- **`DATABASE_URL` set** (Postgres — e.g. the one your Supabase project provides) → analyses
  persist across restarts and are shared across workers/replicas. `analyze` / `generate` /
  `download` are then multi-worker-safe; raise `WEB_CONCURRENCY` (and run multiple replicas).
  *Caveat:* the deep-report reads the cloned repo files, which live on the node that ran
  `analyze`, so report stays best-effort across nodes.

Set `DATABASE_URL` to your Postgres connection string (the app converts `postgres://` /
`postgresql://` to the psycopg driver automatically).

## Hosting
Any container host works (Fly.io, Railway, Render, a VM, ECS). The app listens on `$PORT`
(default 8099). Put it behind TLS + a reverse proxy in production. There is **no auth yet** —
don't expose it publicly until Phase 2 (accounts/auth) lands.
