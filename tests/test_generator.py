"""Test the MCP Foundry generator against CONTRACT.md.

Builds a hand-made capabilities list covering all 4 risk tiers and BOTH
invocations, runs generate(), then asserts:
  1. The emitted server.py compiles clean (py_compile).
  2. The destructive tool's docstring starts with "⚠️ DESTRUCTIVE —".
  3. All 5 bundle files are present and non-empty.

Run: python tests/test_generator.py   (prints PASS/FAIL per check)
"""

import os
import py_compile
import sys
import tempfile

# Make the engine importable regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from engine.generator import generate  # noqa: E402


# ---------------------------------------------------------------------------
# Hand-made capabilities: 4 risk tiers x both invocations.
# ---------------------------------------------------------------------------

CAPABILITIES = [
    # 1. HTTP GET — read
    {
        "id": "list_orders",
        "name": "list_orders",
        "summary": "List orders, optionally filtered by customer email.",
        "params": [
            {"name": "customer_email", "type": "string", "required": False,
             "default": None, "description": "filter by this customer's email"},
            {"name": "limit", "type": "integer", "required": False,
             "default": 20, "description": "max rows to return"},
        ],
        "returns": "list",
        "location": "app.py:42",
        "framework": "fastapi",
        "http": {"method": "GET", "path": "/orders"},
        "invocation": "http",
        "risk": "read",
        "recommended": True,
        "source_symbol": "list_orders",
    },
    # 2. HTTP POST — write
    {
        "id": "create_order",
        "name": "create_order",
        "summary": "Create a new order for a customer.",
        "params": [
            {"name": "customer_email", "type": "string", "required": True,
             "default": None, "description": "the customer placing the order"},
            {"name": "items", "type": "array", "required": True,
             "default": None, "description": "list of line items"},
            {"name": "note", "type": "string", "required": False,
             "default": None, "description": "optional order note"},
        ],
        "returns": "dict",
        "location": "app.py:58",
        "framework": "fastapi",
        "http": {"method": "POST", "path": "/orders"},
        "invocation": "http",
        "risk": "write",
        "recommended": False,
        "source_symbol": "create_order",
    },
    # 3. HTTP DELETE — destructive
    {
        "id": "delete_order",
        "name": "delete_order",
        "summary": "Permanently delete an order by id.",
        "params": [
            {"name": "order_id", "type": "integer", "required": True,
             "default": None, "description": "the id of the order to delete"},
        ],
        "returns": "dict",
        "location": "app.py:71",
        "framework": "fastapi",
        "http": {"method": "DELETE", "path": "/orders/{order_id}"},
        "invocation": "http",
        "risk": "destructive",
        "recommended": False,
        "source_symbol": "delete_order",
    },
    # 4. in_process plain-python — read (also exercises 'sensitive' via tier check below)
    {
        "id": "compute_summary",
        "name": "compute_summary",
        "summary": "Compute an aggregate summary over local data.",
        "params": [
            {"name": "window_days", "type": "integer", "required": False,
             "default": 30, "description": "lookback window in days"},
            {"name": "include_drafts", "type": "boolean", "required": False,
             "default": False, "description": "include draft records"},
        ],
        "returns": "dict",
        "location": "analytics/reports.py:12",
        "framework": "plain-python",
        "http": None,
        "invocation": "in_process",
        "risk": "read",
        "recommended": True,
        "source_symbol": "compute_summary",
    },
    # 5. in_process — sensitive (4th risk tier)
    {
        "id": "rotate_api_token",
        "name": "rotate_api_token",
        "summary": "Rotate the service API token and return the new value.",
        "params": [
            {"name": "service_name", "type": "string", "required": True,
             "default": None, "description": "which service's token to rotate"},
        ],
        "returns": "dict",
        "location": "secrets/manager.py:30",
        "framework": "plain-python",
        "http": None,
        "invocation": "in_process",
        "risk": "sensitive",
        "recommended": False,
        "source_symbol": "rotate_api_token",
    },
]

PROJECT = {
    "language": "python",
    "frameworks": ["fastapi", "plain-python"],
    "root": "/abs/path/to/target/repo",
}

OPTIONS = {
    "server_name": "acme-platform",
    "invocation": "http",  # default; per-cap invocation overrides
    "base_url_env": "TARGET_API_URL",
}


def main() -> int:
    results = []  # (label, passed, detail)

    bundle = generate(PROJECT, CAPABILITIES, OPTIONS)

    # --- Check 3 (run first so we have files): all 5 present and non-empty ---
    expected = {"server.py", "requirements.txt", "README.md", ".mcp.json",
                "CAPABILITIES.md"}
    present = set(bundle.keys())
    missing = expected - present
    empty = [f for f in expected if f in bundle and not bundle[f].strip()]
    files_ok = not missing and not empty
    detail = []
    if missing:
        detail.append(f"missing: {sorted(missing)}")
    if empty:
        detail.append(f"empty: {sorted(empty)}")
    results.append((
        "Check 3 — all 5 files present and non-empty",
        files_ok,
        "; ".join(detail) if detail else
        ", ".join(f"{f}={len(bundle[f])}B" for f in sorted(expected)),
    ))

    server_src = bundle.get("server.py", "")

    # --- Check 1: py_compile the emitted server.py ---
    compile_ok = False
    compile_detail = ""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(server_src)
        tmp_path = tf.name
    try:
        py_compile.compile(tmp_path, doraise=True)
        compile_ok = True
        compile_detail = "compiled clean"
    except py_compile.PyCompileError as e:
        compile_detail = str(e)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    results.append(("Check 1 — server.py compiles (py_compile)", compile_ok,
                    compile_detail))

    # --- Check 2: destructive tool docstring starts with "⚠️ DESTRUCTIVE —" ---
    # Locate the delete_order tool block and inspect its docstring opener.
    marker = "⚠️ DESTRUCTIVE —"
    destructive_ok = False
    destructive_detail = ""
    idx = server_src.find("def delete_order(")
    if idx == -1:
        destructive_detail = "delete_order tool not found in server.py"
    else:
        # The docstring opens right after the signature line.
        after = server_src[idx:]
        q = after.find('"""')
        if q == -1:
            destructive_detail = "no docstring found for delete_order"
        else:
            doc_open = after[q + 3: q + 3 + len(marker)]
            destructive_ok = doc_open == marker
            destructive_detail = f"docstring opens with: {doc_open!r}"
    results.append((
        'Check 2 — destructive docstring starts with "⚠️ DESTRUCTIVE —"',
        destructive_ok,
        destructive_detail,
    ))

    # --- Report ---
    print("=" * 70)
    print("MCP Foundry generator test")
    print("=" * 70)
    all_pass = True
    for label, passed, det in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"[{status}] {label}")
        if det:
            print(f"        {det}")
    print("=" * 70)
    print("OVERALL:", "PASS" if all_pass else "FAIL")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
