"""Tests for the token-bucket rate limiter. Deterministic — time is injected.
Run: python tests/test_ratelimit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import ratelimit  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


print("== spec parsing ==")
check("parses '10/60'", ratelimit._parse("10/60", (1, 1)) == (10, 60))
check("bad spec -> default", ratelimit._parse("garbage", (5, 5)) == (5, 5))
check("non-positive -> default", ratelimit._parse("0/60", (5, 5)) == (5, 5))

print("== token bucket (capacity 3 / 60s) ==")
b = ratelimit.TokenBucket(3, 60)  # rate = 0.05 tok/s
allowed = [b.allow("ip1", now=0.0)[0] for _ in range(3)]
check("first 3 (burst) allowed", allowed == [True, True, True])
ok, retry = b.allow("ip1", now=0.0)
check("4th rejected", ok is False)
check("rejection reports a positive retry-after", retry > 0)

print("== refill over time ==")
# ~20s later one token has refilled (0.05 * 20 = 1.0)
check("allowed again after refill window", b.allow("ip1", now=20.5)[0] is True)

print("== per-key isolation ==")
check("a different IP has its own budget", b.allow("ip2", now=0.0)[0] is True)

print("== path routing ==")
check("/api/analyze is rate-limited", ratelimit.for_path("/api/analyze") is not None)
check("/api/generate is rate-limited", ratelimit.for_path("/api/generate") is not None)
check("/api/health is NOT limited", ratelimit.for_path("/api/health") is None)
check("/ (page) is NOT limited", ratelimit.for_path("/") is None)

failed = [n for n, ok in results if not ok]
print(f"\n{'ALL PASS' if not failed else 'FAILURES: ' + ', '.join(failed)}  ({len(results)-len(failed)}/{len(results)})")
sys.exit(1 if failed else 0)
