"""Tests for the GitHub OAuth module — sets fake creds before import so ENABLED is true.
Verifies the parts that don't need a live GitHub (authorize URL, redirect URI, token vault).
Run: python tests/test_github_oauth.py
"""
import os
import sys

os.environ["GITHUB_CLIENT_ID"] = "fake-client-id"
os.environ["GITHUB_CLIENT_SECRET"] = "fake-secret"
os.environ.pop("GITHUB_REDIRECT_URI", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import github_oauth as gh  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


print("== enabled + authorize URL ==")
check("ENABLED when client id + secret set", gh.ENABLED)
u = gh.authorize_url("https://app.example/auth/github/callback", "STATE123")
check("points at github authorize endpoint", u.startswith("https://github.com/login/oauth/authorize?"))
check("carries the client_id", "client_id=fake-client-id" in u)
check("carries the state (CSRF)", "state=STATE123" in u)
check("carries a scope", "scope=" in u)
check("carries the redirect_uri", "redirect_uri=" in u and "callback" in u)

print("== redirect_uri ==")


class Req:
    base_url = "http://127.0.0.1:8099/"


check("derived from request origin", gh.redirect_uri(Req()) == "http://127.0.0.1:8099/auth/github/callback")
os.environ["GITHUB_REDIRECT_URI"] = "https://prod.example/auth/github/callback"
check("env override wins", gh.redirect_uri(Req()) == "https://prod.example/auth/github/callback")
os.environ.pop("GITHUB_REDIRECT_URI", None)

print("== token vault (server-side, never to browser) ==")
gh.VAULT.set("sid-1", "gho_secret_token")
check("get returns the token", gh.VAULT.get("sid-1") == "gho_secret_token")
check("has() true for known sid", gh.VAULT.has("sid-1"))
check("has() false for unknown sid", not gh.VAULT.has("sid-x"))
check("get(None) is None", gh.VAULT.get(None) is None)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
