"""Drive the managed-hosting round-trip: analyze a fixture -> POST /api/host (real docker
build+run) -> record {id, endpoint, token} to /tmp/host_result.json. Stdlib only.
Env: MCPF_BASE (Foundry server), MCPF_FIXTURES_DIR (dir containing fastapi_orders/).
Run with the Foundry venv.
"""
import io
import os
import json
import zipfile
import urllib.request

BASE = os.environ["MCPF_BASE"]
# Fixture repo to upload. Set MCPF_FIXTURES_DIR to a directory containing a
# `fastapi_orders/` sample app; the script skips when it isn't present.
FIX = os.path.join(os.environ.get("MCPF_FIXTURES_DIR", ""), "fastapi_orders")
if not os.environ.get("MCPF_FIXTURES_DIR") or not os.path.isdir(FIX):
    print(f"SKIP: fixture not found (set MCPF_FIXTURES_DIR; looked for {FIX!r})")
    raise SystemExit(0)


def _zip(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(path):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, path))
    return buf.getvalue()


def analyze():
    b = "----mcpfb"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"f.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + _zip(FIX) + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/analyze", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def post(path, payload, timeout):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


res = analyze()
caps = res["capabilities"]
read_ids = [c["id"] for c in caps if c["risk"] == "read"]
print(f"analyzed: {len(caps)} caps; hosting {len(read_ids)} read tools: {read_ids}")

host = post("/api/host", {
    "analysis_id": res["analysis_id"], "server_name": "orders", "invocation": "http",
    "selected_ids": read_ids, "base_url_env": "ORDERS_API_URL",
    "target_env": {"ORDERS_API_URL": "http://example.invalid"},
}, timeout=420)  # docker build can take a couple minutes

out = {"id": host["id"], "endpoint": host["endpoint"], "token": host["token"], "backend": host["backend"]}
json.dump(out, open("/tmp/host_result.json", "w"))
print(f"HOSTED: {host['endpoint']}  (backend={host['backend']}, id={host['id']})")
