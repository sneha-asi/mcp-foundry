"""Egress allowlist guard — SSRF defense-in-depth for generated http-proxy servers.

A hosted server should only reach its declared target host(s). The generated server INLINES an
equivalent guard (so it stays dependency-free); this module is the canonical, unit-tested copy.
Container/platform network policy (Fly/Cloud Run egress rules) is the complementary infra layer.
"""
from urllib.parse import urlparse


def parse_allow(env_value: str, default_host: str | None) -> list:
    """Build the allowlist from a comma-separated env value, falling back to the target host."""
    hosts = [h.strip().lower() for h in (env_value or "").split(",") if h.strip()]
    if not hosts and default_host:
        hosts = [default_host.lower()]
    return hosts


def host_allowed(url: str, allowlist: list) -> bool:
    """True if the URL's host is allowed. An empty allowlist allows everything (no lockdown)."""
    if not allowlist:
        return True
    return (urlparse(url).hostname or "").lower() in allowlist
