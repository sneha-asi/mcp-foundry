"""Safety guards for untrusted input: SSRF-safe clone URLs, zip-bomb / zip-slip-safe
extraction, and repo-size limits. Pure stdlib.

These exist because the analyze endpoint accepts an arbitrary Git URL or uploaded archive —
both are attacker-controlled. Limits are configurable via env (sane defaults below).
"""
import os
import ipaddress
import socket
import zipfile
from urllib.parse import urlparse, urlunparse


def _mb(env_key: str, default_mb: int) -> int:
    try:
        return int(float(os.environ.get(env_key, default_mb)) * 1024 * 1024)
    except ValueError:
        return default_mb * 1024 * 1024


MAX_UPLOAD_BYTES = _mb("MCPF_MAX_UPLOAD_MB", 50)        # raw uploaded zip
MAX_EXTRACTED_BYTES = _mb("MCPF_MAX_EXTRACTED_MB", 200)  # uncompressed total (zip-bomb cap)
MAX_REPO_BYTES = _mb("MCPF_MAX_REPO_MB", 200)            # cloned repo working tree
try:
    MAX_FILES = int(os.environ.get("MCPF_MAX_FILES", 20000))
    CLONE_TIMEOUT = int(os.environ.get("MCPF_CLONE_TIMEOUT", 120))
except ValueError:
    MAX_FILES, CLONE_TIMEOUT = 20000, 120


class GuardError(ValueError):
    """Raised when input violates a safety guard. Carries a user-safe message."""


# --- SSRF-safe clone URL ----------------------------------------------------------------

def validate_clone_url(url: str) -> str:
    """Return the URL if it's safe to `git clone`, else raise GuardError.

    Blocks non-http(s) schemes and any host that resolves to a private, loopback,
    link-local (incl. the 169.254.169.254 cloud-metadata IP), reserved, or multicast
    address — the standard SSRF surface for a 'clone any URL' feature.
    """
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise GuardError("Only http(s) Git URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise GuardError("Could not parse a host from the URL.")
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise GuardError("Refusing to clone from localhost.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise GuardError(f"Could not resolve host '{host}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise GuardError(f"Host '{host}' resolves to a non-public address — blocked.")
    return url


# --- zip-bomb / zip-slip-safe extraction ------------------------------------------------

def safe_extract(zip_path: str, dest: str,
                 max_files: int = MAX_FILES,
                 max_total: int = MAX_EXTRACTED_BYTES) -> None:
    """Extract a zip into `dest`, guarding against zip bombs and path traversal (zip-slip)."""
    dest_abs = os.path.realpath(dest)
    os.makedirs(dest_abs, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise GuardError("Upload is not a valid .zip archive.")
    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise GuardError(f"Archive has too many files (> {max_files}).")
        total = sum(i.file_size for i in infos)
        if total > max_total:
            raise GuardError(f"Archive expands too large (> {max_total // (1024*1024)} MB).")
        for info in infos:
            # Reject absolute paths and any member that escapes dest via ../
            target = os.path.realpath(os.path.join(dest_abs, info.filename))
            if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
                raise GuardError("Archive contains an unsafe path (zip-slip) — blocked.")
        zf.extractall(dest_abs)


# --- repo size after clone --------------------------------------------------------------

def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def enforce_repo_size(path: str, max_bytes: int = MAX_REPO_BYTES) -> None:
    size = dir_size(path)
    if size > max_bytes:
        raise GuardError(
            f"Repository is too large ({size // (1024*1024)} MB > "
            f"{max_bytes // (1024*1024)} MB limit)."
        )


# --- private-repo cloning (token auth) --------------------------------------------------

def with_token(url: str, token: str) -> str:
    """Embed a token as Git HTTP credentials so a private repo can be cloned.

    Only rewrites http(s) URLs; returns the URL unchanged if there's no token. Validate the
    CLEAN url with validate_clone_url() first — call this only for the actual clone.
    NOTE: the token ends up in the clone URL (and thus the process args). Fine for a
    single-tenant deploy; for multi-tenant, move to a git credential helper so the token
    stays out of the process list. Always pass clone errors through redact() before surfacing.
    """
    if not token:
        return url
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return url
    netloc = f"x-access-token:{token}@{p.hostname}" + (f":{p.port}" if p.port else "")
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def redact(text: str, *secrets: str) -> str:
    """Scrub secrets (e.g. a clone token) from text before logging or returning it."""
    out = text or ""
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out
