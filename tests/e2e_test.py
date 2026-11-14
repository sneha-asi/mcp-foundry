"""End-to-end smoke test against the running MCP Foundry server (port 8099).

Uploads a real fixture repo as a zip -> analyze -> generate -> verify the emitted
server.py compiles. Pure stdlib (builds the multipart body by hand) so it needs no extra deps.
"""
import io
import os
import json
import zipfile
import tempfile
import py_compile
import urllib.request

BASE = os.environ.get("MCPF_BASE", "http://127.0.0.1:8099")
# Fixture repo to upload. Set MCPF_FIXTURES_DIR to a directory containing a
# `fastapi_orders/` sample app; the script skips when it isn't present.
FIX = os.path.join(os.environ.get("MCPF_FIXTURES_DIR", ""), "fastapi_orders")
if not os.environ.get("MCPF_FIXTURES_DIR") or not os.path.isdir(FIX):
    print(f"SKIP: fixture not found (set MCPF_FIXTURES_DIR; looked for {FIX!r})")
    raise SystemExit(0)


def _zip_dir(path: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(path):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, path))
    return buf.getvalue()


def analyze(zip_bytes: bytes) -> dict:
    boundary = "----mcpfoundryboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="fixture.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + zip_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/analyze", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


def post_json(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


def main():
    res = analyze(_zip_dir(FIX))
    print("project:", res["project"])
    caps = res["capabilities"]
    print(f"capabilities: {len(caps)}  excluded: {len(res['excluded'])}")
    for c in caps:
        http = f"{c['http']['method']} {c['http']['path']}" if c.get("http") else c["location"]
        print(f"   [{c['risk']:11}] {c['name']:16} {http}")

    # select EVERYTHING (incl. the destructive delete) to exercise the full path
    ids = [c["id"] for c in caps]
    gen = post_json("/api/generate", {
        "analysis_id": res["analysis_id"], "server_name": "orders-mcp",
        "invocation": "http", "selected_ids": ids, "base_url_env": "ORDERS_API_URL",
    })
    files = gen["files"]
    print("\ngenerated files:", list(files))

    dump = "/tmp/foundry_gen_server.py"
    with open(dump, "w") as f:
        f.write(files["server.py"])
    py_compile.compile(dump, doraise=True)
    print("server.py compiles: OK  (saved to", dump + ")")

    tool_decorators = sum(
        1 for ln in files["server.py"].splitlines() if ln.strip().startswith("@mcp.tool(")
    )
    print(f"@mcp.tool() decorators: {tool_decorators}   (selected: {len(ids)})")
    assert tool_decorators == len(ids), f"expected {len(ids)} tools, got {tool_decorators}"
    assert "⚠️ DESTRUCTIVE" in files["server.py"], "destructive label missing"
    print("destructive label present: OK")
    print("\nE2E PASS")


if __name__ == "__main__":
    main()
