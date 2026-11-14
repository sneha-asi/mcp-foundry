"""Tests for the egress allowlist guard (canonical impl the generated server inlines).
Run: python tests/test_egress.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import egress  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


print("== parse_allow ==")
check("comma list parsed + lowercased", egress.parse_allow("A.com, b.com ", None) == ["a.com", "b.com"])
check("empty -> default host", egress.parse_allow("", "api.example.com") == ["api.example.com"])
check("empty + no default -> []", egress.parse_allow("", None) == [])

print("== host_allowed ==")
allow = ["api.example.com"]
check("allowed host passes", egress.host_allowed("https://api.example.com/orders", allow))
check("allowed host w/ port passes", egress.host_allowed("https://api.example.com:8443/x", allow))
check("other host blocked", not egress.host_allowed("https://evil.com/x", allow))
check("internal IP blocked", not egress.host_allowed("http://169.254.169.254/latest", allow))
check("case-insensitive", egress.host_allowed("https://API.EXAMPLE.COM/x", allow))
check("empty allowlist = allow all (no lockdown)", egress.host_allowed("https://anything.com", []))

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
