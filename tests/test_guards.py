"""Tests for the hardening guards (SSRF, zip-bomb/zip-slip, repo size) and the TTL store.

Stdlib only; deterministic (no network, no real sleep — time is injected into the store).
Run: python tests/test_guards.py
"""
import os
import sys
import zipfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import guards, store  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{PASS if cond else FAIL}] {name}")


def expect_guard(fn):
    try:
        fn()
        return False
    except guards.GuardError:
        return True
    except Exception:
        return False


print("== SSRF clone-URL guard ==")
check("rejects non-http scheme", expect_guard(lambda: guards.validate_clone_url("ssh://git@h/r.git")))
check("rejects file:// scheme", expect_guard(lambda: guards.validate_clone_url("file:///etc/passwd")))
check("rejects localhost", expect_guard(lambda: guards.validate_clone_url("http://localhost/r")))
check("rejects loopback IP", expect_guard(lambda: guards.validate_clone_url("http://127.0.0.1/r")))
check("rejects private 10.x", expect_guard(lambda: guards.validate_clone_url("http://10.0.0.5/r")))
check("rejects private 192.168.x", expect_guard(lambda: guards.validate_clone_url("http://192.168.1.1/r")))
check("rejects cloud-metadata 169.254.169.254",
      expect_guard(lambda: guards.validate_clone_url("http://169.254.169.254/latest/meta-data/")))
try:
    ok = guards.validate_clone_url("https://8.8.8.8/owner/repo.git")  # public literal IP, no DNS
    check("allows a public host", ok.startswith("https://"))
except Exception as e:
    check(f"allows a public host (got {type(e).__name__})", False)

print("== zip-slip / zip-bomb guard ==")
with tempfile.TemporaryDirectory() as td:
    # zip-slip
    slip = os.path.join(td, "slip.zip")
    with zipfile.ZipFile(slip, "w") as z:
        z.writestr("../escape.txt", "pwned")
    check("rejects zip-slip (../ path)", expect_guard(lambda: guards.safe_extract(slip, os.path.join(td, "out1"))))
    # too many files
    many = os.path.join(td, "many.zip")
    with zipfile.ZipFile(many, "w") as z:
        z.writestr("a.txt", "x")
        z.writestr("b.txt", "y")
    check("rejects too-many-files", expect_guard(lambda: guards.safe_extract(many, os.path.join(td, "out2"), max_files=1)))
    # too large (uncompressed)
    big = os.path.join(td, "big.zip")
    with zipfile.ZipFile(big, "w") as z:
        z.writestr("data.txt", "A" * 5000)
    check("rejects over-size extraction", expect_guard(lambda: guards.safe_extract(big, os.path.join(td, "out3"), max_total=1000)))
    # normal extracts fine
    good = os.path.join(td, "good.zip")
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("pkg/m.py", "def f():\n    return 1\n")
    out = os.path.join(td, "out4")
    try:
        guards.safe_extract(good, out)
        check("extracts a normal zip", os.path.exists(os.path.join(out, "pkg", "m.py")))
    except Exception as e:
        check(f"extracts a normal zip (got {type(e).__name__})", False)

print("== repo-size guard ==")
with tempfile.TemporaryDirectory() as td:
    with open(os.path.join(td, "f.bin"), "wb") as f:
        f.write(b"0" * 4096)
    check("rejects oversized repo", expect_guard(lambda: guards.enforce_repo_size(td, max_bytes=1024)))
    try:
        guards.enforce_repo_size(td, max_bytes=10 * 1024 * 1024)
        check("allows normal repo size", True)
    except Exception:
        check("allows normal repo size", False)

print("== TTL store + temp-dir cleanup ==")
wd_a = tempfile.mkdtemp(prefix="mcpf_test_")
wd_b = tempfile.mkdtemp(prefix="mcpf_test_")
# TTL expiry deletes the workdir
s = store.AnalysisStore(ttl_seconds=100, max_entries=10)
s.put("a", repo_path=wd_a, result={"x": 1}, workdir=wd_a, now=1000.0)
check("get within TTL returns entry", s.get("a", now=1050.0) is not None)
check("get past TTL returns None", s.get("a", now=2000.0) is None)
check("expired workdir is deleted", not os.path.exists(wd_a))
# capacity eviction deletes the oldest workdir
s2 = store.AnalysisStore(ttl_seconds=10000, max_entries=1)
s2.put("b", repo_path=wd_b, result={}, workdir=wd_b, now=1000.0)
wd_c = tempfile.mkdtemp(prefix="mcpf_test_")
s2.put("c", repo_path=wd_c, result={}, workdir=wd_c, now=1001.0)
check("over-capacity evicts oldest", s2.get("b", now=1001.0) is None)
check("evicted workdir is deleted", not os.path.exists(wd_b))
check("newest entry survives", s2.get("c", now=1001.0) is not None)
import shutil
shutil.rmtree(wd_c, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
