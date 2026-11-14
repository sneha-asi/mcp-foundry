# MCP Foundry — architecture

**What it is:** a web platform that turns a codebase (or an OpenAPI spec) into a working MCP
server — discover its capabilities, curate which to expose, generate a runnable server, and run it
locally, remotely, self-hosted, or fully managed.

---

## The pipeline (the spine)

```
input ─▶ ANALYZE ─▶ CURATE ─▶ GENERATE ─▶ { download | self-host | managed host } ─▶ connect to Claude
        (discover)  (human)   (bundle)
```

1. **Input** — a GitHub URL (+ optional token for private), a code upload (.zip), or an OpenAPI spec.
2. **Analyze** — clone/extract (behind safety guards) → discover capabilities → persist the result.
3. **Curate** — the user picks which capabilities become tools (risk-grouped; writes/destructive opt-in).
4. **Generate** — emit a self-contained MCP server bundle.
5. **Run** — download & run yourself, self-host (Docker/Fly), or have Foundry host it.

---

## Components by layer

### Frontend — `app/templates/index.html` (single-page, vanilla JS)
Input → curate (risk-grouped checkboxes) → generate/preview/download. Optional **Supabase login
gate**, **"Connect GitHub"**, and **deep-report** view. Talks only to the API below.

### API / web backend — `app/main.py` (FastAPI)
- **Middleware:** signed-cookie sessions · token-bucket **rate limiting**.
- **Endpoints:** `/api/analyze` · `/api/generate` · `/api/download` · `/api/report` · `/api/config`
  · `/api/github/status` + `/auth/github/*` · `/api/host` (deploy/list/destroy) · `/api/health`.
- **Cross-cutting (gated, env-driven):** auth · guards · store · hosting (below).

### Engine — pure logic, no web framework (reusable; also the `codebase-to-mcp` skill's core)
| Module | Job |
|---|---|
| `engine/discovery.py` | AST scan (FastAPI / Flask / Django / plain-Python) → capabilities |
| `engine/openapi_discovery.py` | OpenAPI/Swagger spec → capabilities (deterministic path) |
| `engine/generator.py` | capabilities → MCP server **bundle** (see below) |
| `engine/analyzer.py` | repo → deep-understanding report (Claude when keyed, else structural) |
| `engine/egress.py` | SSRF egress allowlist guard (inlined into generated servers) |

The **Capability** object (`CONTRACT.md`) is the spine between discovery and generation:
`{id, name, summary, params, returns, http, invocation, risk, ...}`.

### Data & infra (each picks backend by env; in-memory default → SQL when `DATABASE_URL` set)
- **Analyses** — `app/store.py`: `AnalysisStore` (in-memory, TTL+cleanup) | `SqlAnalysisStore` (Postgres).
- **Hosting** — `app/hosting.py`: pluggable `Deployer` (LocalDocker | Fly Machines) +
  `DeploymentRegistry` (in-memory | SQL). Control plane for managed hosting.
- **Auth** — `app/auth.py` (Supabase JWT verify) + `app/github_oauth.py` (OAuth + server-side token vault).
- **Guards** — `app/guards.py`: SSRF-safe clone, zip-bomb/zip-slip-safe extract, size limits, token redaction.

---

## The generated artifact (the product's output) — a `mcp_server/` bundle
`server.py` · `requirements.txt` · `README.md` · `.mcp.json` · `CAPABILITIES.md` · `Dockerfile` · `fly.toml`

The server itself:
- **Transports:** **stdio** by default (Claude Desktop/Code); **Streamable HTTP** + bearer auth when
  `MCP_TRANSPORT=http` (remote / hosted), endpoint at `/mcp`.
- **Invocation per tool:** **in-process** import · **HTTP proxy** to a target API · **subprocess** (CLI).
- **Safety:** one tool per curated capability, typed inputs, docstring descriptions, ⚠️ on destructive,
  secrets from env, defensive error handling, and an **egress allowlist** (`MCP_EGRESS_ALLOW`,
  default = target host) + no-redirect on every outbound call.

---

## Run / deploy topology
| Mode | How | Who runs it |
|---|---|---|
| **Local** | stdio | user (Claude Desktop/Code) |
| **Remote** | `MCP_TRANSPORT=http` + bearer token | user, anywhere |
| **Self-host** | bundle's `Dockerfile` / `fly.toml` → Docker/Fly/Railway/Render/Cloud Run | user's own host |
| **Managed** | `POST /api/host` → Foundry builds + runs a container per server (scale-to-zero), bearer-token endpoint | Foundry (http-proxy servers only — no untrusted code-exec) |

See `docs/HOSTING_DESIGN.md` for the managed-hosting design and `ROADMAP.md` for build status.

---

## Security model (summary)
SSRF-safe cloning · zip-bomb/zip-slip-safe uploads · size + rate limits · gated Supabase auth with
owner-scoped data · GitHub tokens stored server-side · per-hosted-server bearer token (stored hashed)
· **http-proxy-only** managed hosting · app-layer egress allowlist. Everything is **gated by env** —
unset = open dev mode; set = enforced.
