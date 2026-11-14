# MCP Foundry

Turn any codebase into a working MCP (Model Context Protocol) server. Paste a GitHub repo URL or
upload code → Foundry discovers the capabilities → you curate which to expose (risk-flagged,
destructive opt-in) → it generates a runnable FastMCP server + connect config.

This is the web platform built on top of the `codebase-to-mcp` skill's engine logic.

## Deep-understanding report

Before (or instead of) generating a server, Foundry can **read the repo in depth and explain it**: after analyzing, click **"Generate deep report"** for a narrative breakdown — overview, architecture, how it works, key modules, and where to start building on top.

- With `ANTHROPIC_API_KEY` set, **Claude** writes the narrative, grounded only in the repo's files/README/capabilities (no fabrication).
- Without a key, it falls back to a **structural report** built straight from the discovery data.

Set the key (optional) via a local `.env` (see `.env.example`) — it's server-side, never shipped to the browser.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload --port 8099
# open http://127.0.0.1:8099
```

## Layout

```
mcp-foundry/
├── engine/
│   ├── discovery.py    # scan a repo -> capabilities (FastAPI/Flask/Django/plain-python, AST-based)
│   ├── generator.py    # capabilities -> FastMCP server bundle (http-proxy or in-process)
│   └── analyzer.py     # repo + discovery -> deep-understanding report (Claude or structural)
├── app/
│   ├── main.py         # FastAPI: /api/analyze, /api/report, /api/generate, /api/download
│   └── templates/index.html   # single-page UI (input -> curate -> generate)
├── tests/
│   ├── test_discovery.py / test_generator.py   # engine unit checks
│   └── e2e_test.py     # end-to-end against the running server
├── CONTRACT.md         # the shared spec (Capability shape + API)
└── ROADMAP.md          # v1 scope + phases 2-4 (auth, hosting, more inputs)
```

## Status

The core loop works end-to-end: upload/clone → analyze → curate → generate → download, with the
emitted `server.py` compiling (`cli.py` also accepts an OpenAPI spec). On top of that, all
optional and off unless configured (see the docstrings in `app/` and `ROADMAP.md`):

- **Auth** — Supabase JWT auth (`SUPABASE_JWT_SECRET`); open dev mode when unset. Optional
  "Sign in with GitHub" OAuth for private repos (`GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`).
- **Persistence** — analyses and hosted deployments are stored in SQL when `DATABASE_URL` is set
  (Postgres); otherwise an in-memory TTL store.
- **Managed hosting** — `/api/host` deploys a generated http-proxy server with a per-server bearer
  token (`HOSTING_BACKEND=local-docker` verified end-to-end; the Fly backend is not yet live).
- **Hardening** — SSRF-safe cloning, safe zip extraction and size limits, per-IP rate limiting,
  and an egress allowlist in generated servers.

Still single-node: rate-limit buckets and the GitHub token vault are in-process, so with several
replicas they are per-node.

## Test

```bash
.venv/bin/python tests/test_discovery.py
.venv/bin/python tests/test_generator.py
.venv/bin/python tests/e2e_test.py     # requires the server running on :8099
```

`test_discovery.py`, `e2e_test.py` and `host_e2e.py` run against sample repos kept outside this
repo: set `MCPF_FIXTURES_DIR` to a directory containing `fastapi_orders/` and `textkit/` (and
optionally `MCPF_CYBERPANEL_API` to a CyberPanel `api/` checkout). Targets that are not present
are skipped. The other `tests/test_*.py` scripts are self-contained.
