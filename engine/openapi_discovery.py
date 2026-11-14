"""Discover capabilities from an OpenAPI / Swagger spec.

The deterministic input path: an OpenAPI document already declares every operation's method,
path, parameters, and descriptions — so we convert it straight into Capability objects (the
CONTRACT.md shape) with no guessing. Pair with engine.generator (invocation="http") to emit an
HTTP-proxy MCP server.

Pure stdlib (json). YAML specs should be converted to JSON before calling.
"""
import json
import re

_TYPE_MAP = {
    "string": "string", "integer": "integer", "number": "number",
    "boolean": "boolean", "array": "array", "object": "object",
}
_METHODS = ("get", "post", "put", "patch", "delete")
_VERB = {"get": "get", "post": "create", "put": "update", "patch": "update", "delete": "delete"}


def _schema_type(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "unknown"
    t = schema.get("type")
    if isinstance(t, list):  # OpenAPI 3.1 nullable unions
        t = next((x for x in t if x != "null"), None)
    return _TYPE_MAP.get(t, "unknown")


def _risk(method: str, path: str, op: dict) -> str:
    m = method.lower()
    blob = f"{path} {op.get('operationId','')} {op.get('summary','')}".lower()
    if re.search(r"key|secret|password|token|ssh|credential", blob):
        return "sensitive"
    if m == "delete" or re.search(r"\bdelete\b|\bdrop\b|\bpurge\b|\bremove\b", blob):
        return "destructive"
    if m in ("post", "put", "patch"):
        return "write"
    return "read"


def _name(method: str, path: str, op: dict, used: set) -> str:
    """Clean, readable, unique tool name derived from method + path.

    GET /pnl/wide -> get_pnl_wide ; GET /deals/{deal_id} -> get_deals_by_deal_id.
    Prefer this over the spec's operationId, which is often verbose/redundant
    (e.g. FastAPI's 'health_health_get').
    """
    verb = _VERB.get(method.lower(), method.lower())
    parts = [verb]
    for seg in [s for s in path.strip("/").split("/") if s]:
        if seg.startswith("{") and seg.endswith("}"):
            parts.append("by_" + seg.strip("{}"))
        else:
            parts.append(seg)
    raw = re.sub(r"[^0-9a-zA-Z]+", "_", "_".join(parts)).strip("_").lower()
    if not raw:
        raw = re.sub(r"[^0-9a-zA-Z]+", "_", op.get("operationId") or verb).strip("_").lower() or verb
    name, i = raw, 2
    while name in used:
        name = f"{raw}_{i}"
        i += 1
    used.add(name)
    return name


def _params(op: dict) -> list:
    out = []
    for p in op.get("parameters", []) or []:
        if not isinstance(p, dict) or p.get("in") not in ("path", "query"):
            continue
        sch = p.get("schema", {}) or {}
        out.append({
            "name": p.get("name"),
            "type": _schema_type(sch),
            "required": bool(p.get("required", p.get("in") == "path")),
            "default": sch.get("default"),
            "description": (p.get("description") or "")[:160],
        })
    return out


def _summary(method: str, path: str, op: dict) -> str:
    s = op.get("summary") or (op.get("description") or "").strip().splitlines()[0:1]
    s = op.get("summary") or (s[0] if isinstance(s, list) and s else "")
    return (s or f"{method.upper()} {path}").strip()[:200]


def discover_openapi(spec_path: str, methods=_METHODS) -> dict:
    """Parse an OpenAPI JSON spec into {project, capabilities, excluded}.

    methods: which HTTP methods to INCLUDE as capabilities (e.g. {"get"} for read-only).
    Operations whose method is excluded are recorded in `excluded`.
    """
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)

    info = spec.get("info", {})
    paths = spec.get("paths", {}) or {}
    methods = {m.lower() for m in methods}

    caps, excluded, used = [], [], set()
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _METHODS or not isinstance(op, dict):
                continue
            if method.lower() not in methods:
                excluded.append({"name": f"{method.upper()} {path}",
                                 "reason": f"{method.upper()} excluded by scope"})
                continue
            name = _name(method, path, op, used)
            caps.append({
                "id": name, "name": name,
                "summary": _summary(method, path, op),
                "params": _params(op),
                "returns": "unknown",
                "location": f"{method.upper()} {path}",
                "framework": "openapi",
                "http": {"method": method.upper(), "path": path},
                "invocation": "http",
                "risk": _risk(method, path, op),
                "recommended": _risk(method, path, op) == "read",
                "source_symbol": name,
            })
    return {
        "project": {"language": "http-api",
                    "frameworks": ["openapi"],
                    "title": info.get("title", "API"),
                    "root": spec_path},
        "capabilities": caps,
        "excluded": excluded,
    }
