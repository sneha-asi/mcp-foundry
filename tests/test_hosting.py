"""Tests for the managed-hosting control plane — the parts verifiable without live infra:
http-proxy-only guard, token gen, deployer factory, docker arg construction, registry.
(The actual deploy() round-trip needs Docker/Fly and is out of scope here.)
Run: python tests/test_hosting.py
"""
import os
import sys

os.environ["HOSTING_BACKEND"] = "local-docker"  # exercise the enabled path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import hosting  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


HTTP = [{"id": "a", "invocation": "http"}, {"id": "b", "invocation": "http"}]
MIXED = [{"id": "a", "invocation": "http"}, {"id": "b", "invocation": "in_process"}]

print("== http-proxy-only guard (hosting safety: no untrusted code-exec) ==")
check("all-http -> hostable", hosting.http_proxy_only(HTTP, {"invocation": "http"}))
check("any in_process -> NOT hostable", not hosting.http_proxy_only(MIXED, {"invocation": "http"}))
check("default invocation respected", not hosting.http_proxy_only([{"id": "a"}], {"invocation": "in_process"}))

print("== tokens ==")
t1, t2 = hosting.new_server_token(), hosting.new_server_token()
check("tokens are unique", t1 != t2)
check("tokens are non-trivial", len(t1) >= 24)
check("hash deterministic + differs", hosting.token_hash(t1) == hosting.token_hash(t1) != hosting.token_hash(t2))

print("== deployer factory ==")
check("enabled when backend set", hosting.HOSTING_ENABLED)
check("local-docker -> LocalDockerDeployer", isinstance(hosting.make_deployer(), hosting.LocalDockerDeployer))
hosting.HOSTING_BACKEND = "fly"
check("fly -> FlyDeployer", isinstance(hosting.make_deployer(), hosting.FlyDeployer))
hosting.HOSTING_BACKEND = ""
check("disabled -> None", hosting.make_deployer() is None)
hosting.HOSTING_BACKEND = "local-docker"

print("== docker command construction (no execution) ==")
d = hosting.LocalDockerDeployer()
build = d.build_cmd("/tmp/wd", "mcpf/x:latest")
run = d.run_cmd("mcpf/x:latest", "x", 8080, {"MCP_SERVER_TOKEN": "tok", "MCP_TRANSPORT": "http"})
check("build cmd is docker build", build[:3] == ["docker", "build", "-t"] and build[-1] == "/tmp/wd")
check("run maps the port", "-p" in run and "8080:8080" in run)
check("run injects the token env", "MCP_SERVER_TOKEN=tok" in run)
check("run is detached", "-d" in run and run[-1] == "mcpf/x:latest")

print("== deployment registry (owner-scoped) ==")
r = hosting.DeploymentRegistry()
rec = r.create(owner="u1", name="x", endpoint="http://h/mcp", ref="c1", backend="local-docker",
               token_h="hh", now=1000.0)
check("get by owner", r.get(rec["id"], "u1") is not None)
check("other owner blocked", r.get(rec["id"], "u2") is None)
check("list scoped to owner", len(r.list("u1")) == 1 and len(r.list("u2")) == 0)
check("public() strips token_hash", "token_hash" not in hosting.public(rec))
check("delete removes it", r.delete(rec["id"]) is not None and r.get(rec["id"], "u1") is None)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
