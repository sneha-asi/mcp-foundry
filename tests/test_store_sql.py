"""Tests for SqlAnalysisStore against SQLite (same SQLAlchemy code path as Postgres).

Covers put/get, owner scoping, TTL expiry + workdir cleanup, capacity eviction, and — the key
win — persistence across a "restart" (a fresh store instance on the same DB still sees the row).
Run: python tests/test_store_sql.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import store  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


tmp = tempfile.mkdtemp(prefix="mcpf_sqltest_")
db_url = f"sqlite:///{os.path.join(tmp, 'analyses.db')}"
RESULT = {"project": {"language": "python"}, "capabilities": [{"id": "x", "name": "x"}], "excluded": []}


def workdir():
    return tempfile.mkdtemp(prefix="mcpf_wd_")


print("== put / get / owner ==")
s = store.SqlAnalysisStore(db_url, ttl_seconds=100, max_entries=3)
wd_a = workdir()
s.put("a", repo_path=wd_a + "/repo", result=RESULT, workdir=wd_a, now=1000.0, owner="u1")
got = s.get("a", now=1000.0)
check("get returns the stored result", got and got["result"]["capabilities"][0]["id"] == "x")
check("get returns repo_path + workdir", got["repo_path"].endswith("/repo") and got["workdir"] == wd_a)
check("owner u1 can read", s.get("a", now=1000.0, owner="u1") is not None)
check("owner u2 CANNOT read", s.get("a", now=1000.0, owner="u2") is None)

print("== persistence across 'restart' (new instance, same DB file) ==")
s2 = store.SqlAnalysisStore(db_url, ttl_seconds=100, max_entries=3)
check("fresh store instance still sees the row", s2.get("a", now=1050.0) is not None)

print("== TTL expiry + workdir cleanup ==")
check("expired get returns None", s2.get("a", now=2000.0) is None)
check("expired workdir deleted", not os.path.exists(wd_a))

print("== capacity eviction (max_entries=3) ==")
s3 = store.SqlAnalysisStore(db_url, ttl_seconds=10000, max_entries=3)
wds = {}
for i, k in enumerate(["b", "c", "d", "e"]):
    wds[k] = workdir()
    s3.put(k, repo_path=wds[k], result=RESULT, workdir=wds[k], now=1000.0 + i)
check("len capped at max_entries", len(s3) == 3)
check("oldest (b) evicted", s3.get("b", now=1004.0) is None)
check("oldest workdir deleted", not os.path.exists(wds["b"]))
check("newest (e) survives", s3.get("e", now=1004.0) is not None)

import shutil
for wd in wds.values():
    shutil.rmtree(wd, ignore_errors=True)
shutil.rmtree(tmp, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
