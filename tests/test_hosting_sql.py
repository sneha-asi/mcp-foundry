"""Tests for SqlDeploymentRegistry — persistence across 'restart', owner scoping, factory.
Run: python tests/test_hosting_sql.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import hosting  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


tmp = tempfile.mkdtemp(prefix="mcpf_hostsql_")
url = f"sqlite:///{os.path.join(tmp, 'deps.db')}"


def mk():
    return hosting.SqlDeploymentRegistry(url)


print("== create / get / owner scope ==")
r = mk()
rec = r.create(owner="u1", name="orders", endpoint="http://h/mcp", ref="c1",
               backend="local-docker", token_h="hh", now=1000.0)
check("get by owner", r.get(rec["id"], "u1") is not None)
check("other owner blocked", r.get(rec["id"], "u2") is None)
check("list scoped to owner", len(r.list("u1")) == 1 and len(r.list("u2")) == 0)
check("public() strips token_hash", "token_hash" not in hosting.public(r.get(rec["id"], "u1")))

print("== persistence across 'restart' (new instance, same DB) ==")
r2 = mk()
got = r2.get(rec["id"], "u1")
check("fresh instance still sees the deployment", got is not None and got["endpoint"] == "http://h/mcp")

print("== delete ==")
check("delete returns the record", r2.delete(rec["id"]) is not None)
check("gone after delete", r2.get(rec["id"], "u1") is None)

print("== make_registry factory ==")
os.environ["DATABASE_URL"] = url
check("DATABASE_URL set -> SQL registry", isinstance(hosting.make_registry(), hosting.SqlDeploymentRegistry))
os.environ.pop("DATABASE_URL", None)
check("unset -> in-memory registry", isinstance(hosting.make_registry(), hosting.DeploymentRegistry))

import shutil
shutil.rmtree(tmp, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
