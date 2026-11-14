"""Tests for the Supabase auth layer — verifies enforcement with self-issued HS256 tokens
(no live Supabase needed). Sets the JWT secret BEFORE importing app.auth so AUTH is enabled.

Run: python tests/test_auth.py
"""
import os
import sys
import time

SECRET = "test-secret-123"
os.environ["SUPABASE_JWT_SECRET"] = SECRET
os.environ["SUPABASE_URL"] = "https://example.supabase.co"
os.environ["SUPABASE_ANON_KEY"] = "anon-public-key"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jwt  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app import auth  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def tok(secret=SECRET, aud="authenticated", exp_delta=3600, sub="user-1"):
    payload = {"sub": sub, "email": "a@b.com", "aud": aud, "exp": int(time.time()) + exp_delta}
    return jwt.encode(payload, secret, algorithm="HS256")


def expect_401(fn):
    try:
        fn()
        return False
    except HTTPException as e:
        return e.status_code == 401
    except Exception:
        return False


print("== auth enabled (secret set) ==")
check("AUTH_ENABLED is true", auth.AUTH_ENABLED)
u = auth.require_user("Bearer " + tok())
check("valid token -> user id extracted", u.get("id") == "user-1" and u.get("anonymous") is False)
check("missing header -> 401", expect_401(lambda: auth.require_user(None)))
check("non-bearer header -> 401", expect_401(lambda: auth.require_user("Basic abc")))
check("bad signature -> 401", expect_401(lambda: auth.require_user("Bearer " + tok(secret="wrong"))))
check("expired token -> 401", expect_401(lambda: auth.require_user("Bearer " + tok(exp_delta=-10))))
check("wrong audience -> 401", expect_401(lambda: auth.require_user("Bearer " + tok(aud="other"))))

print("== public config (no secrets leak) ==")
cfg = auth.public_config()
check("config exposes url + anon key", cfg["supabase_url"] and cfg["supabase_anon_key"])
check("config does NOT contain the jwt secret", SECRET not in str(cfg))

print("== open mode (disabled) ==")
auth.AUTH_ENABLED = False
try:
    anon = auth.require_user(None)
    check("disabled -> anonymous user, no token needed", anon.get("anonymous") is True)
finally:
    auth.AUTH_ENABLED = True

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
