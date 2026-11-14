"""Optional Supabase auth.

Disabled (open dev mode) unless SUPABASE_JWT_SECRET is set — so local dev and the existing
tests keep working with zero config. When the env is set, the protected API routes require a
valid Supabase access token (Bearer <jwt>) in the Authorization header, verified locally
(HS256 against the project's JWT secret). The browser signs in with supabase-js and sends it.

To go live, set in the environment:
  SUPABASE_URL        e.g. https://xxxx.supabase.co   (public; sent to the browser)
  SUPABASE_ANON_KEY   the project's anon/public key    (public; sent to the browser)
  SUPABASE_JWT_SECRET the project's JWT secret          (SECRET; server-side only)
"""
import os

from fastapi import Header, HTTPException

try:
    import jwt  # PyJWT
except ImportError:  # pragma: no cover
    jwt = None

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
AUTH_ENABLED = bool(SUPABASE_JWT_SECRET)

ANON_USER = {"id": "anonymous", "email": None, "anonymous": True}


def public_config() -> dict:
    """Values safe to hand the browser so it can run the Supabase client. No secrets."""
    return {
        "auth_enabled": AUTH_ENABLED,
        "supabase_url": SUPABASE_URL or None,
        "supabase_anon_key": SUPABASE_ANON_KEY or None,
    }


def verify_token(token: str) -> dict:
    """Verify a Supabase access token; return {id, email, anonymous:False} or raise 401."""
    if jwt is None:
        raise HTTPException(500, "auth is enabled but PyJWT is not installed")
    try:
        claims = jwt.decode(
            token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated"
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "session expired — please sign in again")
    except Exception:
        raise HTTPException(401, "invalid authentication token")
    return {"id": claims.get("sub"), "email": claims.get("email"), "anonymous": False}


def require_user(authorization: str = Header(None)) -> dict:
    """FastAPI dependency. Open mode → anonymous user; enabled → verified Supabase user."""
    if not AUTH_ENABLED:
        return ANON_USER
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication required")
    return verify_token(authorization.split(" ", 1)[1].strip())
