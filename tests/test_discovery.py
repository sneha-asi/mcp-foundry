"""Run the discovery engine against real repos and validate the output.

Usage:  MCPF_FIXTURES_DIR=/path/to/fixtures python tests/test_discovery.py
(targets that are not present are skipped; see REPOS below)
"""
from __future__ import annotations

import os
import sys

# Make `engine` importable when run from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from engine.discovery import discover  # noqa: E402

# ---------------------------------------------------------------------------
# Contract validator — checks every Capability matches CONTRACT.md's shape.
# ---------------------------------------------------------------------------
_PARAM_TYPES = {"string", "integer", "number", "boolean", "array", "object", "unknown"}
_RETURNS = {"list", "dict", "str", "int", "bool", "none", "unknown"}
_FRAMEWORKS = {"fastapi", "flask", "django", "express", "plain-python", "unknown"}
_INVOCATIONS = {"http", "in_process"}
_RISKS = {"read", "write", "destructive", "sensitive"}
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

_CAP_KEYS = {
    "id", "name", "summary", "params", "returns", "location", "framework",
    "http", "invocation", "risk", "recommended", "source_symbol",
}
_PARAM_KEYS = {"name", "type", "required", "default", "description"}


def validate_capability(cap: dict) -> list[str]:
    """Return a list of contract violations for a single capability (empty = ok)."""
    errs: list[str] = []
    missing = _CAP_KEYS - set(cap.keys())
    extra = set(cap.keys()) - _CAP_KEYS
    if missing:
        errs.append(f"missing keys: {sorted(missing)}")
    if extra:
        errs.append(f"unexpected keys: {sorted(extra)}")

    if not isinstance(cap.get("id"), str) or not cap["id"]:
        errs.append("id must be a non-empty string")
    if not isinstance(cap.get("name"), str) or not cap["name"]:
        errs.append("name must be a non-empty string")
    if not isinstance(cap.get("summary"), str):
        errs.append("summary must be a string")
    if cap.get("returns") not in _RETURNS:
        errs.append(f"returns {cap.get('returns')!r} not in {_RETURNS}")
    if not isinstance(cap.get("location"), str) or ":" not in cap.get("location", ""):
        errs.append("location must be 'file:line'")
    if cap.get("framework") not in _FRAMEWORKS:
        errs.append(f"framework {cap.get('framework')!r} not in {_FRAMEWORKS}")
    if cap.get("invocation") not in _INVOCATIONS:
        errs.append(f"invocation {cap.get('invocation')!r} not in {_INVOCATIONS}")
    if cap.get("risk") not in _RISKS:
        errs.append(f"risk {cap.get('risk')!r} not in {_RISKS}")
    if not isinstance(cap.get("recommended"), bool):
        errs.append("recommended must be bool")
    if cap.get("recommended") != (cap.get("risk") == "read"):
        errs.append("recommended must be (risk == 'read')")
    if not isinstance(cap.get("source_symbol"), str) or not cap["source_symbol"]:
        errs.append("source_symbol must be a non-empty string")

    # http: present (dict with method+path) only for HTTP routes, else null.
    http = cap.get("http")
    if http is not None:
        if not isinstance(http, dict) or set(http.keys()) != {"method", "path"}:
            errs.append("http must be {'method','path'} or null")
        else:
            if http["method"] not in _HTTP_METHODS:
                errs.append(f"http.method {http['method']!r} invalid")
            if not isinstance(http["path"], str):
                errs.append("http.path must be a string")
        if cap.get("invocation") != "http":
            errs.append("http present but invocation != 'http'")
    else:
        if cap.get("invocation") == "http":
            errs.append("invocation 'http' but http is null")

    # params
    params = cap.get("params")
    if not isinstance(params, list):
        errs.append("params must be a list")
    else:
        for i, p in enumerate(params):
            if not isinstance(p, dict):
                errs.append(f"param[{i}] not a dict")
                continue
            pmiss = _PARAM_KEYS - set(p.keys())
            if pmiss:
                errs.append(f"param[{i}] missing {sorted(pmiss)}")
            if p.get("type") not in _PARAM_TYPES:
                errs.append(f"param[{i}] type {p.get('type')!r} invalid")
            if not isinstance(p.get("required"), bool):
                errs.append(f"param[{i}] required must be bool")
            if not isinstance(p.get("name"), str) or not p["name"]:
                errs.append(f"param[{i}] name must be non-empty string")
            if not isinstance(p.get("description"), str):
                errs.append(f"param[{i}] description must be string")
    return errs


def validate_result(result: dict) -> list[str]:
    errs: list[str] = []
    if set(result.keys()) != {"project", "capabilities", "excluded"}:
        errs.append(f"top-level keys: {sorted(result.keys())}")
    proj = result.get("project", {})
    if set(proj.keys()) != {"language", "frameworks", "root"}:
        errs.append(f"project keys: {sorted(proj.keys())}")
    for i, cap in enumerate(result.get("capabilities", [])):
        for e in validate_capability(cap):
            errs.append(f"capabilities[{i}] ({cap.get('name')}): {e}")
    for i, ex in enumerate(result.get("excluded", [])):
        if set(ex.keys()) != {"name", "reason"}:
            errs.append(f"excluded[{i}] keys: {sorted(ex.keys())}")
    return errs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summarize(label: str, path: str) -> dict:
    print("=" * 78)
    print(f"REPO: {label}")
    print(f"  path: {path}")
    result = discover(path)
    proj = result["project"]
    caps = result["capabilities"]
    excluded = result["excluded"]

    print(f"  language: {proj['language']}   frameworks: {proj['frameworks']}")
    print(f"  capabilities: {len(caps)}   excluded: {len(excluded)}")

    by_risk: dict[str, int] = {}
    by_fw: dict[str, int] = {}
    for c in caps:
        by_risk[c["risk"]] = by_risk.get(c["risk"], 0) + 1
        by_fw[c["framework"]] = by_fw.get(c["framework"], 0) + 1
    print(f"  risk breakdown:      {by_risk}")
    print(f"  framework breakdown: {by_fw}")

    print("  sample capabilities:")
    for c in caps[:6]:
        http = f" {c['http']['method']} {c['http']['path']}" if c["http"] else ""
        pnames = ",".join(p["name"] for p in c["params"]) or "-"
        print(f"    - {c['name']:<28} risk={c['risk']:<11} "
              f"inv={c['invocation']:<10} fw={c['framework']:<12} "
              f"rec={str(c['recommended']):<5}{http}  params=[{pnames}]")

    if excluded:
        print("  sample excluded:")
        for e in excluded[:6]:
            print(f"    - {e['name']}  ({e['reason']})")

    errs = validate_result(result)
    if errs:
        print(f"  !! CONTRACT VIOLATIONS ({len(errs)}):")
        for e in errs[:20]:
            print(f"     {e}")
    else:
        print("  OK: output validates against the contract Capability shape.")
    print()
    return {"label": label, "result": result, "errors": errs,
            "by_risk": by_risk, "by_fw": by_fw}


# Target repos live outside this repo. MCPF_FIXTURES_DIR is a directory with
# `fastapi_orders/` and `textkit/` sample apps; MCPF_CYBERPANEL_API points at a
# checkout of CyberPanel's `api/` directory. Missing targets are skipped.
_FIXTURES = os.environ.get("MCPF_FIXTURES_DIR", "")
REPOS = [
    ("fastapi_orders", os.path.join(_FIXTURES, "fastapi_orders") if _FIXTURES else ""),
    ("textkit", os.path.join(_FIXTURES, "textkit") if _FIXTURES else ""),
    ("cyberpanel/api", os.environ.get("MCPF_CYBERPANEL_API", "")),
]


def main() -> int:
    all_ok = True
    for label, path in REPOS:
        if not path or not os.path.isdir(path):
            print(f"SKIP: {label} (target not found: {path or 'env var not set'})")
            continue
        info = summarize(label, path)
        if info["errors"]:
            all_ok = False
    print("=" * 78)
    print("ALL CONTRACT-VALID" if all_ok else "CONTRACT VIOLATIONS FOUND")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
