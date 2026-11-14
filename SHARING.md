# Make an MCP server from your product

Someone shared this with you so you can turn **your** product into an MCP server (a set of tools
an AI agent can call) and send it back to them. No account, no web UI — one command.

## You need
- `python3` and `git`
- Your product's code (a folder) **or** its `openapi.json`

## Do this
```bash
# 1. clone this tool
git clone <this-repo-url> && cd <this-repo>

# 2. point it at YOUR product (a path, a GitHub URL, or an OpenAPI spec)
python cli.py /path/to/your/product            # safe default: read-only tools
#   python cli.py https://github.com/you/yourproduct --all
#   python cli.py ./openapi.json --risk read,write

# 3. it prints what it found and writes a server to ./mcp_server/
```

`--risk read` (default) exposes only read-only tools. Add `--risk read,write` or `--all` to include
writes/deletes — those are off by default on purpose (an agent shouldn't get a "delete" button
unless you mean it).

## Send it back
Zip the **`mcp_server/`** folder (or commit it) and share it. That folder is the MCP server —
it has `server.py`, a README, and a `.mcp.json` connect snippet. It runs two ways:
- **stdio** (local): `python server.py`
- **networked**: `MCP_TRANSPORT=http MCP_SERVER_TOKEN=… python server.py` → endpoint at `/mcp`

If your product is an HTTP API, the tools proxy to it — the person running the server sets the
target URL via the env var named in `mcp_server/README.md`. Nothing is sent anywhere during
generation; it only reads your code.
