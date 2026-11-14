"""In-process token-bucket rate limiting for the cost-heavy endpoints.

Keyed by client IP (honors X-Forwarded-For only when MCPF_TRUST_PROXY=1, so a spoofed header
can't bypass it behind a non-proxy). Single-node — buckets live in memory; behind multiple
replicas the limit is per-node (a shared Redis store would make it global, a later step).

Limits are env-tunable as "<count>/<seconds>":
  MCPF_RL_ANALYZE  default 10/60   (analyze clones/extracts — the expensive one)
  MCPF_RL_API      default 60/60   (generate / download / report)
"""
import os
import threading

ANALYZE_PATHS = {"/api/analyze"}
API_PATHS = {"/api/generate", "/api/download", "/api/report"}
TRUST_PROXY = os.environ.get("MCPF_TRUST_PROXY") == "1"


def _parse(spec: str, default: tuple) -> tuple:
    try:
        count, seconds = spec.split("/")
        count, seconds = float(count), float(seconds)
        if count <= 0 or seconds <= 0:
            return default
        return count, seconds
    except (ValueError, AttributeError):
        return default


class TokenBucket:
    """Refill `capacity` tokens over `window` seconds; each request costs one token."""

    def __init__(self, count: float, window: float):
        self.capacity = count
        self.rate = count / window           # tokens per second
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        with self._lock:
            tokens = self._tokens.get(key, self.capacity)
            last = self._last.get(key, now)
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            self._last[key] = now
            if tokens >= 1:
                self._tokens[key] = tokens - 1
                return True, 0.0
            self._tokens[key] = tokens
            return False, (1 - tokens) / self.rate


_analyze = TokenBucket(*_parse(os.environ.get("MCPF_RL_ANALYZE", ""), (10, 60)))
_api = TokenBucket(*_parse(os.environ.get("MCPF_RL_API", ""), (60, 60)))


def for_path(path: str):
    """Return the bucket guarding this path, or None if the path isn't rate-limited."""
    if path in ANALYZE_PATHS:
        return _analyze
    if path in API_PATHS:
        return _api
    return None


def client_key(request) -> str:
    """Best-effort client identity for limiting. Trusts XFF only when MCPF_TRUST_PROXY=1."""
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
