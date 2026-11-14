"""Optional 'Sign in with GitHub' OAuth, for cloning private repos without pasting a token.

Disabled unless GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET are set. The user's GitHub token is kept
SERVER-SIDE (a per-process vault keyed by an opaque session id held in a signed cookie) — it never
goes to the browser. Per-node (re-connect after a restart is one click); a shared store would make
it cross-node, a later step.

Register the OAuth app at: GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.
Callback URL must be  <your-origin>/auth/github/callback.
"""
import os
import threading
from urllib.parse import urlencode

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
SCOPE = os.environ.get("GITHUB_OAUTH_SCOPE", "repo read:user").strip()
ENABLED = bool(CLIENT_ID and CLIENT_SECRET)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"


def redirect_uri(request) -> str:
    """The callback URL — env override, else derived from the incoming request's origin."""
    env = os.environ.get("GITHUB_REDIRECT_URI", "").strip()
    if env:
        return env
    return str(request.base_url).rstrip("/") + "/auth/github/callback"


def authorize_url(redirect_uri_: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri_,
        "scope": SCOPE,
        "state": state,
        "allow_signup": "false",
    })


def exchange_code(code: str, redirect_uri_: str) -> str:
    """Exchange an OAuth code for a GitHub access token. Raises on failure."""
    if httpx is None:
        raise RuntimeError("httpx is required for GitHub OAuth")
    resp = httpx.post(
        TOKEN_URL,
        data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
              "code": code, "redirect_uri": redirect_uri_},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("access_token")
    if not token:
        raise RuntimeError("GitHub did not return an access token")
    return token


class TokenVault:
    """Server-side store: session id -> GitHub token. Token never touches the browser."""

    def __init__(self):
        self._d: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, sid: str, token: str) -> None:
        with self._lock:
            self._d[sid] = token

    def get(self, sid: str | None) -> str | None:
        if not sid:
            return None
        with self._lock:
            return self._d.get(sid)

    def has(self, sid: str | None) -> bool:
        return self.get(sid) is not None


VAULT = TokenVault()
