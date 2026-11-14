"""Tests for private-repo token cloning helpers (guards.with_token / guards.redact).
Run: python tests/test_private_repo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import guards  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


TOK = "ghp_SECRET_TOKEN_VALUE_123"

print("== with_token ==")
u = guards.with_token("https://github.com/owner/repo.git", TOK)
check("injects token as x-access-token creds", u == f"https://x-access-token:{TOK}@github.com/owner/repo.git")
check("preserves path", u.endswith("/owner/repo.git"))
check("empty token -> url unchanged", guards.with_token("https://github.com/o/r", "") == "https://github.com/o/r")
check("non-http (ssh) left unchanged", guards.with_token("git@github.com:o/r.git", TOK) == "git@github.com:o/r.git")
up = guards.with_token("https://ghe.example.com:8443/o/r", TOK)
check("keeps custom port", "@ghe.example.com:8443/" in up)

print("== redact (no token leaks) ==")
err = f"fatal: could not read from https://x-access-token:{TOK}@github.com/o/r: 403"
red = guards.redact(err, TOK)
check("token scrubbed from error text", TOK not in red and "***" in red)
check("redact tolerates empty/None", guards.redact("", TOK) == "" and guards.redact("hi", "") == "hi")

print("== SSRF guard still passes a public github host ==")
try:
    guards.validate_clone_url("https://github.com/owner/repo")
    check("github.com validates (public)", True)
except Exception:
    check("github.com validates (public)", False)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
