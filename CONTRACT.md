# MCP Foundry — internal build contract (v1)

This file is the shared spec the engine modules and the API must agree on. Don't change a
field name without updating every consumer.

## What the product does
Web app: user gives a **GitHub repo URL** or **uploads code** → backend **discovers** the code's
capabilities → user **curates** which to expose (risk-flagged; destructive opt-in) → backend
**generates** a downloadable FastMCP server + connect config. Hosting/auth/billing are later phases.

## The Capability object (the spine of the whole app)
Both discovery (produces it) and generation (consumes it) use exactly this shape:

```json
{
  "id": "list_orders",                     // stable slug, unique within a project
  "name": "list_orders",                   // tool name (snake_case)
  "summary": "List orders, optionally filtered by customer email.",
  "params": [
    {"name": "customer_email", "type": "string", "required": false, "default": null,
     "description": "filter by this customer's email"}
  ],
  "returns": "list",                        // rough shape: list|dict|str|int|bool|none|unknown
  "location": "app.py:42",                  // file:line in the source repo
  "framework": "fastapi",                   // fastapi|flask|django|express|plain-python|unknown
  "http": {"method": "GET", "path": "/orders"},  // present only for HTTP routes, else null
  "invocation": "http",                     // "http" (proxy an endpoint) | "in_process" (import a function)
  "risk": "read",                           // read|write|destructive|sensitive
  "recommended": true,                      // default-checked in the curation UI (true for read, false otherwise)
  "source_symbol": "list_orders"            // the real function/symbol name to import (in_process)
}
```

Param `type` ∈ {string, integer, number, boolean, array, object, unknown}. Map Python hints:
str→string, int→integer, float→number, bool→boolean, list→array, dict→object, missing→unknown.

Risk heuristics: GET / getter / "list_/get_/fetch_/verify_" → read; POST/PUT/PATCH or
"create_/update_/add_/submit_/change_" → write; DELETE or "delete_/drop_/purge_/remove_" →
destructive; name contains key/secret/password/token/ssh → sensitive.

## Engine module APIs (pure Python, no web framework imports)

### engine/discovery.py
```python
def discover(repo_path: str) -> dict:
    """Return {
      'project': {'language': str, 'frameworks': [str], 'root': str},
      'capabilities': [Capability, ...],   # included candidates
      'excluded': [{'name': str, 'reason': str}, ...]
    }"""
```
Discover: FastAPI/Flask/Django routes (via AST + decorator scan), and public top-level functions
in plain-Python modules (skip `_`-prefixed, tests, migrations, dunders). Best-effort param/type
extraction from signatures. Never raise on a single bad file — skip and continue.

### engine/generator.py
```python
def generate(project: dict, capabilities: list, options: dict) -> dict:
    """options = {'server_name': str, 'invocation': 'http'|'in_process',
                  'base_url_env': 'TARGET_API_URL'}  # for http proxy
       Return {filename: file_contents_str, ...} for an mcp_server/ bundle:
         server.py, requirements.txt, README.md, .mcp.json, CAPABILITIES.md
    """
```
- Python output uses FastMCP (`from mcp.server.fastmcp import FastMCP`), one `@mcp.tool()` per cap,
  typed args from params, docstring = summary (+ "⚠️ DESTRUCTIVE —" prefix for destructive risk).
- `http` invocation → tools POST/GET to `{os.environ[base_url_env]}<path>`, creds from env, httpx,
  structured error on failure. `in_process` → import `source_symbol` from the repo and call it.
- No stubs, no fabricated returns. Defensive try/except per tool.

## HTTP API (app/main.py, FastAPI)
- `GET /` → serves the SPA (app/templates/index.html).
- `POST /api/analyze` → body `{"repo_url": "..."}` OR multipart file upload (zip). Clones/extracts to
  a temp dir, calls `discovery.discover`, returns its dict + an `analysis_id` (temp dir kept for generate).
- `POST /api/generate` → body `{"analysis_id": str, "server_name": str, "invocation": str,
  "selected_ids": [str], "base_url_env": str}` → calls `generator.generate` with the selected caps,
  zips the files, returns the zip (application/zip) for download.
- Keep analyses in an in-memory dict for v1 (note: not multi-process safe — fine for v1).

## Frontend (single page, vanilla JS, polished — no build step)
Two-step flow on one page: (1) input (repo URL or drag-drop upload) → calls /api/analyze;
(2) curation table grouped by risk with checkboxes (destructive unchecked + ⚠️), server-name +
invocation controls, Generate button → downloads the zip and shows the connect snippet.
Clean, flat, no fake data. Match a modern product aesthetic.
