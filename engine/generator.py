"""MCP Foundry — code generator.

Turns a curated list of Capability objects (see CONTRACT.md) into a downloadable
FastMCP server bundle. This module is pure templating: it emits CODE AS STRINGS.
It imports nothing beyond the standard library and never imports a web framework.

Public API
----------
    generate(project: dict, capabilities: list, options: dict) -> dict

Returns a mapping of {filename: contents_str} for an ``mcp_server/`` bundle:
    server.py, requirements.txt, README.md, .mcp.json, CAPABILITIES.md
"""

from __future__ import annotations

import json
import keyword
import re

# ---------------------------------------------------------------------------
# Type mapping (CONTRACT.md): contract param type -> Python annotation
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "unknown": "str",
}

# Risk tiers we know about. Anything else is treated as "read" for recommendation
# purposes but rendered verbatim in docs.
_KNOWN_RISKS = {"read", "write", "destructive", "sensitive"}

_INDENT = "    "


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _py_type(contract_type) -> str:
    """Map a contract param type to a Python annotation string."""
    if not isinstance(contract_type, str):
        return "str"
    return _TYPE_MAP.get(contract_type.strip().lower(), "str")


def _sanitize_identifier(name, fallback: str) -> str:
    """Coerce an arbitrary string into a valid, non-keyword Python identifier."""
    if not isinstance(name, str):
        name = str(name) if name is not None else ""
    # Replace anything that isn't a word char with an underscore.
    cleaned = re.sub(r"\W+", "_", name).strip("_")
    if not cleaned:
        cleaned = fallback
    # Identifiers may not start with a digit.
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned


def _py_literal(value) -> str:
    """Render a Python literal for a default value (used in signatures)."""
    # repr() gives valid Python source for str/int/float/bool/None/list/dict
    # built from JSON, which is exactly what we get out of the contract.
    return repr(value)


def _docstring_lines(summary: str, risk: str) -> list[str]:
    """Build the body lines of a tool docstring (without the triple quotes)."""
    summary = (summary or "").strip()
    if risk == "destructive":
        # Contract: destructive docstrings MUST start with this exact prefix.
        prefix = "⚠️ DESTRUCTIVE — "  # "⚠️ DESTRUCTIVE — "
        body = f"{prefix}{summary}" if summary else prefix.rstrip()
    else:
        body = summary or "No description provided."
    return body.splitlines() or [""]


def _escape_docstring(text: str) -> str:
    """Make text safe to embed inside a triple-quoted docstring."""
    # Avoid prematurely closing the docstring and don't leave a trailing
    # backslash that would escape the closing quotes.
    text = text.replace('"""', '\\"\\"\\"')
    if text.endswith("\\"):
        text = text + " "
    return text


# ---------------------------------------------------------------------------
# Signature + param handling
# ---------------------------------------------------------------------------

def _build_signature_params(params: list) -> tuple[list[str], list[dict]]:
    """Return (rendered_param_strings, normalized_params).

    Required params with no default come first, then optional ones — Python
    requires non-default args before default args, regardless of contract order.
    """
    normalized = []
    used_names = set()
    for i, raw in enumerate(params or []):
        if not isinstance(raw, dict):
            continue
        py_name = _sanitize_identifier(raw.get("name"), f"arg_{i}")
        # Guarantee uniqueness within the signature.
        base = py_name
        n = 1
        while py_name in used_names:
            py_name = f"{base}_{n}"
            n += 1
        used_names.add(py_name)

        normalized.append(
            {
                "py_name": py_name,
                "orig_name": raw.get("name", py_name),
                "py_type": _py_type(raw.get("type")),
                "contract_type": raw.get("type", "unknown"),
                "required": bool(raw.get("required", False)),
                "default": raw.get("default", None),
                "has_default_key": "default" in raw,
                "description": (raw.get("description") or "").strip(),
            }
        )

    required = [p for p in normalized if p["required"]]
    optional = [p for p in normalized if not p["required"]]

    rendered = []
    for p in required:
        rendered.append(f"{p['py_name']}: {p['py_type']}")
    for p in optional:
        # Optional params get a default. Use the contract default if one was
        # supplied (even if null), otherwise None.
        default_val = p["default"] if p["has_default_key"] else None
        rendered.append(
            f"{p['py_name']}: {p['py_type']} = {_py_literal(default_val)}"
        )

    # Preserve a stable order for downstream use (required then optional).
    ordered = required + optional
    return rendered, ordered


def _return_annotation(returns) -> str:
    """Map a capability's rough return shape to a Python return annotation."""
    mapping = {
        "list": "list",
        "dict": "dict",
        "str": "str",
        "int": "int",
        "bool": "bool",
        "none": "None",
        "unknown": "dict",
    }
    if not isinstance(returns, str):
        return "dict"
    return mapping.get(returns.strip().lower(), "dict")


# ---------------------------------------------------------------------------
# Tool body emitters
# ---------------------------------------------------------------------------

def _emit_http_body(cap: dict, params: list, options: dict) -> list[str]:
    """Emit the body of an HTTP-proxy tool."""
    http = cap.get("http") or {}
    method = str(http.get("method", "GET") or "GET").upper()
    path = http.get("path", "/") or "/"

    # Path-template params (e.g. /orders/{order_id}) are interpolated into the
    # URL and must NOT also be sent as query/body params.
    path_param_names = set(re.findall(r"\{([^{}]+)\}", path))

    # Split params into query/body. For GET/DELETE -> query params; for
    # POST/PUT/PATCH -> JSON body. This mirrors the reference patterns.
    is_body_method = method in {"POST", "PUT", "PATCH"}

    lines: list[str] = []
    lines.append(f"{_INDENT}headers = dict(_AUTH_HEADERS)")

    # Build the params/json payload from the function arguments, excluding any
    # argument that is consumed by the URL path template.
    payload_params = [p for p in params if p["orig_name"] not in path_param_names]
    if payload_params:
        lines.append(f"{_INDENT}_payload = {{}}")
        for p in payload_params:
            key = json.dumps(p["orig_name"])
            lines.append(f"{_INDENT}if {p['py_name']} is not None:")
            lines.append(f"{_INDENT}{_INDENT}_payload[{key}] = {p['py_name']}")
    else:
        lines.append(f"{_INDENT}_payload = {{}}")

    # Build the URL. Only emit an f-string if the path actually contains a
    # template field we can fill from a param; otherwise use a plain concat to
    # avoid a stray brace turning into an invalid f-string expression.
    if path_param_names:
        # Map path field names to the sanitized python arg names.
        rendered_path = path
        for p in params:
            if p["orig_name"] in path_param_names:
                rendered_path = rendered_path.replace(
                    "{" + p["orig_name"] + "}", "{" + p["py_name"] + "}"
                )
        url_expr = f"f\"{{_BASE_URL}}{rendered_path}\""
    else:
        url_expr = f"_BASE_URL + {json.dumps(path)}"

    request_kwarg = "json" if is_body_method else "params"

    lines.append(f"{_INDENT}_url = {url_expr}")
    lines.append(f"{_INDENT}try:")
    lines.append(f"{_INDENT}{_INDENT}_egress_guard(_url)  # SSRF guard: only the allowed host(s)")
    lines.append(f"{_INDENT}{_INDENT}_resp = httpx.request(")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}{json.dumps(method)},")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}_url,")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}{request_kwarg}=_payload,")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}headers=headers,")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}timeout=30,")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}follow_redirects=False,  # don't let redirects escape the allowlist")
    lines.append(f"{_INDENT}{_INDENT})")
    lines.append(f"{_INDENT}{_INDENT}_resp.raise_for_status()")
    lines.append(f"{_INDENT}{_INDENT}try:")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}return _resp.json()")
    lines.append(f"{_INDENT}{_INDENT}except ValueError:")
    lines.append(f"{_INDENT}{_INDENT}{_INDENT}return {{\"result\": _resp.text}}")
    lines.append(f"{_INDENT}except httpx.HTTPStatusError as _e:")
    lines.append(
        f"{_INDENT}{_INDENT}return {{"
        f"\"error\": f\"HTTP {{_e.response.status_code}}\", "
        f"\"hint\": \"The target API returned an error status. \" "
        f"\"Check the request arguments and that the endpoint exists.\", "
        f"\"body\": _e.response.text[:500]}}"
    )
    lines.append(f"{_INDENT}except httpx.RequestError as _e:")
    lines.append(
        f"{_INDENT}{_INDENT}return {{"
        f"\"error\": f\"{{type(_e).__name__}}: {{_e}}\", "
        f"\"hint\": f\"Could not reach the target API at {{_BASE_URL}}. \" "
        f"f\"Is the {options_base_url_env(options)} env var set and the service running?\"}}"
    )
    lines.append(f"{_INDENT}except Exception as _e:")
    lines.append(
        f"{_INDENT}{_INDENT}return {{"
        f"\"error\": f\"{{type(_e).__name__}}: {{_e}}\", "
        f"\"hint\": \"Unexpected failure while proxying the request.\"}}"
    )
    return lines


def options_base_url_env(options: dict) -> str:
    return str((options or {}).get("base_url_env") or "TARGET_API_URL")


def _emit_in_process_body(cap: dict, params: list) -> list[str]:
    """Emit the body of an in-process import-and-call tool."""
    source_symbol = cap.get("source_symbol") or cap.get("name")
    alias = f"_impl_{_sanitize_identifier(cap.get('name'), 'tool')}"

    lines: list[str] = []
    lines.append(f"{_INDENT}try:")
    if params:
        # Call with keyword arguments mapped to the original param names.
        call_args = ", ".join(
            f"{p['orig_name']}={p['py_name']}"
            for p in params
            if _sanitize_identifier(p["orig_name"], p["py_name"]) == p["orig_name"]
        )
        # If any original name isn't a valid kwarg identifier, fall back to
        # positional-by-order for those; build a robust call.
        safe_kwargs = []
        positional = []
        for p in params:
            if _is_valid_kwarg(p["orig_name"]):
                safe_kwargs.append(f"{p['orig_name']}={p['py_name']}")
            else:
                positional.append(p["py_name"])
        parts = positional + safe_kwargs
        call_args = ", ".join(parts)
        lines.append(f"{_INDENT}{_INDENT}_out = {alias}({call_args})")
    else:
        lines.append(f"{_INDENT}{_INDENT}_out = {alias}()")
    lines.append(f"{_INDENT}{_INDENT}return _out")
    lines.append(f"{_INDENT}except Exception as _e:")
    lines.append(
        f"{_INDENT}{_INDENT}return {{"
        f"\"error\": f\"{{type(_e).__name__}}: {{_e}}\", "
        f"\"hint\": \"The underlying function raised. Check arguments and \" "
        f"\"that import-time env/config for the target repo is set.\"}}"
    )
    return lines, alias, source_symbol


def _is_valid_kwarg(name) -> bool:
    return (
        isinstance(name, str)
        and name.isidentifier()
        and not keyword.iskeyword(name)
    )


# ---------------------------------------------------------------------------
# Module-level imports for in_process tools
# ---------------------------------------------------------------------------

def _module_from_location(location) -> str | None:
    """Best-effort: derive a dotted module path from a 'file.py:line' location."""
    if not isinstance(location, str) or not location:
        return None
    file_part = location.split(":", 1)[0].strip()
    if not file_part.endswith(".py"):
        return None
    file_part = file_part[:-3]
    # Normalize separators to dots and drop leading ./
    file_part = file_part.lstrip("./")
    dotted = re.sub(r"[\\/]+", ".", file_part).strip(".")
    # Drop a leading package-y noise we can't resolve; keep it simple.
    return dotted or None


# ---------------------------------------------------------------------------
# server.py
# ---------------------------------------------------------------------------

_HTTP_RUNNER = '''

def _serve_http():
    """Run as a networked MCP server (Streamable HTTP) for remote / hosted use.

    Set MCP_SERVER_TOKEN to require `Authorization: Bearer <token>` on every request
    (strongly recommended when exposed publicly). HOST/PORT configure the listener; the
    MCP endpoint is served at /mcp.
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    app = mcp.streamable_http_app()
    token = os.environ.get("MCP_SERVER_TOKEN", "").strip()
    if token:
        class _BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("authorization", "") != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)
        app.add_middleware(_BearerAuth)
    else:
        import sys as _sys
        _sys.stderr.write("[mcp] WARNING: MCP_SERVER_TOKEN unset -- endpoint is UNAUTHENTICATED\\n")
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    # Transport switch: stdio (default; Claude Desktop/Code) or http (networked / hosted).
    if os.environ.get("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http", "streamable_http"):
        _serve_http()
    else:
        mcp.run()
'''


def _render_server(project: dict, capabilities: list, options: dict) -> str:
    options = options or {}
    server_name = str(options.get("server_name") or "generated-mcp-server")
    default_invocation = options.get("invocation")
    base_url_env = options_base_url_env(options)

    has_http = False
    in_process_imports: list[str] = []  # rendered import statements
    import_seen: set[str] = set()
    tool_blocks: list[str] = []

    repo_root = (project or {}).get("root") or "."

    for idx, cap in enumerate(capabilities or []):
        if not isinstance(cap, dict):
            continue
        invocation = cap.get("invocation") or default_invocation or "http"
        risk = cap.get("risk", "read")
        tool_name = _sanitize_identifier(cap.get("name") or cap.get("id"), f"tool_{idx}")

        rendered_params, ordered_params = _build_signature_params(cap.get("params"))
        ret_anno = _return_annotation(cap.get("returns"))

        sig_args = ", ".join(rendered_params)

        block: list[str] = []
        block.append("@mcp.tool()")
        block.append(f"def {tool_name}({sig_args}) -> {ret_anno}:")

        # Docstring
        doc_lines = _docstring_lines(cap.get("summary", ""), risk)
        doc_lines = [_escape_docstring(line) for line in doc_lines]
        # Append a parameter reference block for the agent.
        param_doc = []
        for p in ordered_params:
            desc = _escape_docstring(p["description"]) if p["description"] else ""
            req = "required" if p["required"] else "optional"
            line = f"  - {p['orig_name']} ({p['contract_type']}, {req})"
            if desc:
                line += f": {desc}"
            param_doc.append(line)

        block.append(f'{_INDENT}"""{doc_lines[0]}')
        for extra in doc_lines[1:]:
            block.append(f"{_INDENT}{extra}")
        if param_doc:
            block.append("")
            block.append(f"{_INDENT}Parameters:")
            for pl in param_doc:
                block.append(f"{_INDENT}{pl}")
        block.append(f'{_INDENT}"""')

        if invocation == "in_process":
            body, alias, source_symbol = _emit_in_process_body(cap, ordered_params)
            block.extend(body)
            # Register the import for this symbol.
            module = _module_from_location(cap.get("location"))
            sym = source_symbol if _is_valid_kwarg(source_symbol) else None
            if module and sym:
                stmt = f"from {module} import {sym} as {alias}"
            elif sym:
                # No usable module path: import will be by symbol from repo top.
                stmt = (
                    f"# NOTE: module path for '{sym}' could not be derived from "
                    f"location {cap.get('location')!r}; adjust this import to point "
                    f"at the real module.\nfrom {sym} import {sym} as {alias}"
                )
            else:
                stmt = (
                    f"# NOTE: source_symbol for tool '{tool_name}' was not a valid "
                    f"identifier; wire this import manually.\n"
                    f"def {alias}(*_a, **_k):\n"
                    f"    raise RuntimeError('import for {tool_name} not wired')"
                )
            if stmt not in import_seen:
                import_seen.add(stmt)
                in_process_imports.append(stmt)
        else:
            has_http = True
            block.extend(_emit_http_body(cap, ordered_params, options))

        tool_blocks.append("\n".join(block))

    # ---- Assemble the module ----
    header = [
        '"""MCP server generated by MCP Foundry.',
        "",
        f"Server name: {server_name}",
        "Transport: stdio by default (Claude Code / Desktop); set MCP_TRANSPORT=http for a",
        "networked Streamable-HTTP server (remote / hosted) with bearer-token auth.",
        "",
        "This file was emitted from a curated set of capabilities. Each @mcp.tool()",
        "below proxies an HTTP endpoint or calls an imported function in-process.",
        "See README.md and CAPABILITIES.md for env vars, run, and the tool catalog.",
        '"""',
        "",
        "import os",
    ]

    if has_http:
        header.append("import httpx")
    if in_process_imports:
        header.append("import sys")

    header.append("")
    header.append("from mcp.server.fastmcp import FastMCP")
    header.append("")
    header.append(f"mcp = FastMCP({json.dumps(server_name)})")
    header.append("")

    config_lines: list[str] = []
    if has_http:
        config_lines.append("# --- HTTP proxy configuration (read from the environment) ---")
        config_lines.append(
            f"_BASE_URL = os.environ.get({json.dumps(base_url_env)}, "
            f'"http://localhost:8000").rstrip("/")'
        )
        config_lines.append("# Optional API credential. Set whichever your target API expects.")
        config_lines.append('_API_KEY = os.environ.get("TARGET_API_KEY")')
        config_lines.append(
            '_AUTH_HEADERS = {"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}'
        )
        config_lines.append("")
        config_lines.append("# --- egress allowlist (SSRF defense-in-depth: only reach the target host) ---")
        config_lines.append("# Defaults to the target host; extend with MCP_EGRESS_ALLOW=host1,host2.")
        config_lines.append("from urllib.parse import urlparse as _urlparse  # noqa: E402")
        config_lines.append(
            '_EGRESS_ALLOW = ([h.strip().lower() for h in os.environ.get("MCP_EGRESS_ALLOW", "")'
            '.split(",") if h.strip()] or [(_urlparse(_BASE_URL).hostname or "").lower()])'
        )
        config_lines.append("")
        config_lines.append("")
        config_lines.append("def _egress_guard(url):")
        config_lines.append(f'{_INDENT}_h = (_urlparse(url).hostname or "").lower()')
        config_lines.append(f"{_INDENT}if _EGRESS_ALLOW and _h not in _EGRESS_ALLOW:")
        config_lines.append(
            f'{_INDENT}{_INDENT}raise RuntimeError(f"egress blocked: {{_h}} not in {{_EGRESS_ALLOW}}")'
        )
        config_lines.append("")

    if in_process_imports:
        config_lines.append("# --- In-process imports: add the target repo to sys.path ---")
        config_lines.append(
            "# The target repo root is read from TARGET_REPO_ROOT (falls back to the"
        )
        config_lines.append("# path captured at generation time).")
        config_lines.append(
            f"_REPO_ROOT = os.environ.get(\"TARGET_REPO_ROOT\", {json.dumps(repo_root)})"
        )
        config_lines.append("if _REPO_ROOT and _REPO_ROOT not in sys.path:")
        config_lines.append(f"{_INDENT}sys.path.insert(0, _REPO_ROOT)")
        config_lines.append("")
        config_lines.extend(in_process_imports)
        config_lines.append("")

    parts = ["\n".join(header)]
    if config_lines:
        parts.append("\n".join(config_lines))
    parts.append("\n\n\n".join(tool_blocks) if tool_blocks else "# No capabilities selected.")
    parts.append(_HTTP_RUNNER.strip("\n"))

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------

def _render_requirements(capabilities: list, options: dict) -> str:
    options = options or {}
    default_invocation = options.get("invocation")
    needs_httpx = False
    for cap in capabilities or []:
        if not isinstance(cap, dict):
            continue
        invocation = cap.get("invocation") or default_invocation or "http"
        if invocation == "http":
            needs_httpx = True
            break
    lines = ["mcp>=1.2.0"]
    if needs_httpx:
        lines.append("httpx>=0.27")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# .mcp.json  (Claude Code config block)
# ---------------------------------------------------------------------------

def _render_mcp_json(project: dict, capabilities: list, options: dict) -> str:
    options = options or {}
    server_name = str(options.get("server_name") or "generated-mcp-server")
    base_url_env = options_base_url_env(options)
    default_invocation = options.get("invocation")

    has_http = False
    has_in_process = False
    for cap in capabilities or []:
        if not isinstance(cap, dict):
            continue
        invocation = cap.get("invocation") or default_invocation or "http"
        if invocation == "http":
            has_http = True
        elif invocation == "in_process":
            has_in_process = True

    env: dict[str, str] = {}
    if has_http:
        env[base_url_env] = "http://localhost:8000"
        env["TARGET_API_KEY"] = ""
    if has_in_process:
        env["TARGET_REPO_ROOT"] = (project or {}).get("root") or "."

    server_entry: dict = {
        "command": "python",
        "args": ["mcp_server/server.py"],
    }
    if env:
        server_entry["env"] = env

    config = {"mcpServers": {server_name: server_entry}}
    return json.dumps(config, indent=2) + "\n"


# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------

def _render_readme(project: dict, capabilities: list, options: dict) -> str:
    options = options or {}
    server_name = str(options.get("server_name") or "generated-mcp-server")
    base_url_env = options_base_url_env(options)
    default_invocation = options.get("invocation")

    has_http = False
    has_in_process = False
    for cap in capabilities or []:
        if not isinstance(cap, dict):
            continue
        invocation = cap.get("invocation") or default_invocation or "http"
        if invocation == "http":
            has_http = True
        elif invocation == "in_process":
            has_in_process = True

    n_tools = sum(1 for c in (capabilities or []) if isinstance(c, dict))

    env_rows = []
    if has_http:
        env_rows.append(
            f"| `{base_url_env}` | Base URL of the target API the tools proxy "
            f"(e.g. `http://localhost:8000`). | yes (http tools) |"
        )
        env_rows.append(
            "| `TARGET_API_KEY` | Bearer token sent as `Authorization: Bearer <key>` "
            "if your API needs auth. | optional |"
        )
    if has_in_process:
        env_rows.append(
            "| `TARGET_REPO_ROOT` | Absolute path to the target repo so its modules "
            "import. Defaults to the path captured at generation time. | yes (in-process tools) |"
        )

    env_table = ""
    if env_rows:
        env_table = (
            "| Variable | Purpose | Required |\n"
            "| --- | --- | --- |\n" + "\n".join(env_rows) + "\n"
        )
    else:
        env_table = "_No environment variables are required for this bundle._\n"

    lines = []
    lines.append(f"# {server_name}")
    lines.append("")
    lines.append(
        f"A [Model Context Protocol](https://modelcontextprotocol.io) server generated by "
        f"**MCP Foundry**, exposing **{n_tools} tool"
        f"{'s' if n_tools != 1 else ''}** from your codebase to MCP clients "
        f"(Claude Code, Claude Desktop, Cursor)."
    )
    lines.append("")
    lines.append("## Layout")
    lines.append("")
    lines.append("```")
    lines.append("mcp_server/")
    lines.append("  server.py          # the FastMCP server (one @mcp.tool() per capability)")
    lines.append("  requirements.txt   # Python dependencies")
    lines.append("  README.md          # this file")
    lines.append("  .mcp.json          # ready-to-use Claude Code config block")
    lines.append("  CAPABILITIES.md    # the tool catalog (params, invocation, risk)")
    lines.append("```")
    lines.append("")
    lines.append("## Environment variables")
    lines.append("")
    lines.append(env_table.rstrip())
    lines.append("")
    lines.append("## Install & run")
    lines.append("")
    lines.append("```bash")
    lines.append("python -m venv .venv")
    lines.append("source .venv/bin/activate        # Windows: .venv\\Scripts\\activate")
    lines.append("pip install -r mcp_server/requirements.txt")
    lines.append("")
    if has_http:
        lines.append(f"export {base_url_env}=http://localhost:8000   # point at your running API")
        lines.append("export TARGET_API_KEY=...                      # only if the API needs auth")
        lines.append("# export MCP_EGRESS_ALLOW=host1,host2          # restrict outbound (default: target host)")
    if has_in_process:
        lines.append("export TARGET_REPO_ROOT=/abs/path/to/your/repo # so the target modules import")
    lines.append("")
    lines.append("python mcp_server/server.py      # starts on stdio")
    lines.append("```")
    lines.append("")
    lines.append(
        "The server runs on **stdio** by default — the transport Claude Code and Claude "
        "Desktop launch. It will appear idle; that's correct, it's waiting for an MCP client."
    )
    lines.append("")
    lines.append("## Remote / hosted (Streamable HTTP)")
    lines.append("")
    lines.append("To run as a networked MCP server (for remote use or hosting) instead of stdio:")
    lines.append("")
    lines.append("```bash")
    lines.append("export MCP_TRANSPORT=http")
    lines.append("export MCP_SERVER_TOKEN=$(openssl rand -hex 16)   # require Bearer auth (recommended)")
    lines.append("export PORT=8080")
    lines.append("python mcp_server/server.py        # serves the MCP endpoint at /mcp")
    lines.append("```")
    lines.append("")
    lines.append(
        "Clients connect to `http://<host>:$PORT/mcp` with header "
        "`Authorization: Bearer $MCP_SERVER_TOKEN`. If `MCP_SERVER_TOKEN` is unset the endpoint "
        "is **unauthenticated** — only do that on a trusted network."
    )
    lines.append("")
    lines.append("## Deploy (self-host)")
    lines.append("")
    lines.append("This bundle includes a `Dockerfile` and `fly.toml` to run the server on your own host")
    lines.append("(it runs in HTTP mode — see above). Set a token; also set the env vars from the run")
    lines.append("section above if the server proxies an API.")
    lines.append("")
    lines.append("```bash")
    lines.append("# Docker (any host):")
    lines.append("docker build -t my-mcp .")
    lines.append("docker run -p 8080:8080 -e MCP_SERVER_TOKEN=$(openssl rand -hex 16) my-mcp")
    lines.append("")
    lines.append("# Fly.io (scale-to-zero):")
    lines.append("fly launch --copy-config --no-deploy")
    lines.append("fly secrets set MCP_SERVER_TOKEN=$(openssl rand -hex 16)")
    lines.append("fly deploy")
    lines.append("```")
    lines.append("")
    lines.append("Railway / Render / Cloud Run: point them at the `Dockerfile`; set `MCP_SERVER_TOKEN`")
    lines.append("(and any API env vars) as secrets. The MCP endpoint is then `https://<host>/mcp`.")
    lines.append("")
    lines.append("## Register with Claude Code")
    lines.append("")
    lines.append(
        "Copy `.mcp.json` into your project root (Claude Code auto-discovers it), or merge its "
        "`mcpServers` block into your existing config. Then in Claude Code:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append("claude mcp list        # confirm the server is registered")
    lines.append("```")
    lines.append("")
    lines.append("Adjust the `env` values in `.mcp.json` to match your environment before launching.")
    lines.append("")
    lines.append("## Tool catalog")
    lines.append("")
    lines.append("See [CAPABILITIES.md](./CAPABILITIES.md) for every tool, its parameters, how it is")
    lines.append("invoked, and its risk tier. **Destructive** tools are prefixed with ⚠️ in their")
    lines.append("descriptions so the calling agent is warned before use.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CAPABILITIES.md
# ---------------------------------------------------------------------------

_RISK_LABEL = {
    "read": "read (safe)",
    "write": "write (mutates data)",
    "destructive": "destructive (irreversible)",
    "sensitive": "sensitive (handles secrets)",
}


def _render_capabilities_md(capabilities: list, options: dict) -> str:
    options = options or {}
    server_name = str(options.get("server_name") or "generated-mcp-server")
    default_invocation = options.get("invocation")

    lines = []
    lines.append(f"# Capabilities — {server_name}")
    lines.append("")
    caps = [c for c in (capabilities or []) if isinstance(c, dict)]
    lines.append(
        f"This server exposes **{len(caps)} tool{'s' if len(caps) != 1 else ''}**. "
        "Risk tiers: `read` (safe), `write` (mutates data), `destructive` (irreversible), "
        "`sensitive` (handles secrets)."
    )
    lines.append("")

    if not caps:
        lines.append("_No capabilities selected._")
        lines.append("")
        return "\n".join(lines)

    for idx, cap in enumerate(caps):
        tool_name = _sanitize_identifier(cap.get("name") or cap.get("id"), f"tool_{idx}")
        risk = cap.get("risk", "read")
        risk_label = _RISK_LABEL.get(risk, risk)
        invocation = cap.get("invocation") or default_invocation or "http"
        summary = (cap.get("summary") or "").strip() or "_No description._"

        marker = " ⚠️" if risk == "destructive" else ""
        lines.append(f"## `{tool_name}`{marker}")
        lines.append("")
        lines.append(summary)
        lines.append("")
        lines.append(f"- **Risk:** {risk_label}")
        lines.append(f"- **Invocation:** {invocation}")
        if invocation == "http":
            http = cap.get("http") or {}
            method = str(http.get("method", "GET") or "GET").upper()
            path = http.get("path", "/") or "/"
            lines.append(f"- **Endpoint:** `{method} {path}`")
        else:
            sym = cap.get("source_symbol") or cap.get("name")
            loc = cap.get("location") or "unknown"
            lines.append(f"- **Imports:** `{sym}` (from `{loc}`)")
        ret = cap.get("returns", "unknown")
        lines.append(f"- **Returns:** {ret}")
        lines.append("")

        params = [p for p in (cap.get("params") or []) if isinstance(p, dict)]
        if params:
            lines.append("**Parameters**")
            lines.append("")
            lines.append("| Name | Type | Required | Default | Description |")
            lines.append("| --- | --- | --- | --- | --- |")
            for p in params:
                name = p.get("name", "")
                ptype = p.get("type", "unknown")
                required = "yes" if p.get("required") else "no"
                default = p.get("default", None)
                default_str = "—" if default is None else f"`{default!r}`"
                desc = (p.get("description") or "").replace("|", "\\|").strip() or "—"
                lines.append(
                    f"| `{name}` | {ptype} | {required} | {default_str} | {desc} |"
                )
            lines.append("")
        else:
            lines.append("_No parameters._")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _render_dockerfile(project: dict, capabilities: list, options: dict) -> str:
    """Container that runs the server as a networked MCP (Streamable HTTP) for self-hosting."""
    options = options or {}
    has_in_process = any(
        (c.get("invocation") or options.get("invocation")) == "in_process"
        for c in (capabilities or [])
    )
    note = ""
    if has_in_process:
        note = (
            "# NOTE: this server imports a target repo in-process. COPY that repo into the image\n"
            "# and set TARGET_REPO_ROOT — or prefer an http-proxy server for clean hosting.\n"
        )
    return (
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY server.py .\n"
        + note
        + "\n"
        "# Run as a networked MCP server (Streamable HTTP) at /mcp.\n"
        "ENV MCP_TRANSPORT=http PORT=8080 PYTHONUNBUFFERED=1\n"
        "# Set MCP_SERVER_TOKEN at deploy time for Bearer auth (without it the endpoint is open).\n"
        "EXPOSE 8080\n"
        "RUN useradd -m -u 10001 mcp && chown -R mcp /app\n"
        "USER mcp\n"
        'CMD ["python", "server.py"]\n'
    )


def _render_fly_toml(options: dict) -> str:
    """Fly.io config with scale-to-zero, for one-command self-hosting."""
    options = options or {}
    slug = re.sub(r"[^a-z0-9-]+", "-", (options.get("server_name") or "my-mcp").lower()).strip("-")
    slug = slug or "mcp-server"
    return (
        "# Fly.io config. Deploy:\n"
        "#   fly launch --copy-config --no-deploy\n"
        "#   fly secrets set MCP_SERVER_TOKEN=$(openssl rand -hex 16)\n"
        "#   fly deploy\n"
        f'app = "{slug}-mcp"\n'
        'primary_region = "iad"\n'
        "\n"
        "[build]\n"
        "\n"
        "[http_service]\n"
        "  internal_port = 8080\n"
        "  force_https = true\n"
        "  auto_stop_machines = true     # scale to zero when idle\n"
        "  auto_start_machines = true\n"
        "  min_machines_running = 0\n"
        "\n"
        "[env]\n"
        '  MCP_TRANSPORT = "http"\n'
        '  PORT = "8080"\n'
    )


def generate(project: dict, capabilities: list, options: dict) -> dict:
    """Generate an mcp_server/ bundle from curated capabilities.

    Parameters
    ----------
    project : dict
        {'language': str, 'frameworks': [str], 'root': str} (see CONTRACT.md).
    capabilities : list
        A list of Capability dicts to expose as tools.
    options : dict
        {'server_name': str, 'invocation': 'http'|'in_process',
         'base_url_env': 'TARGET_API_URL'}. A capability's own 'invocation'
        field, when present, takes precedence over options['invocation'].

    Returns
    -------
    dict
        {filename: contents_str} for server.py, requirements.txt, README.md,
        .mcp.json, and CAPABILITIES.md.
    """
    project = project or {}
    capabilities = capabilities or []
    options = options or {}

    return {
        "server.py": _render_server(project, capabilities, options),
        "requirements.txt": _render_requirements(capabilities, options),
        "README.md": _render_readme(project, capabilities, options),
        ".mcp.json": _render_mcp_json(project, capabilities, options),
        "CAPABILITIES.md": _render_capabilities_md(capabilities, options),
        "Dockerfile": _render_dockerfile(project, capabilities, options),
        "fly.toml": _render_fly_toml(options),
    }
