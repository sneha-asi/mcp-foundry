#!/usr/bin/env python3
"""codebase -> MCP server, from the command line (no web UI needed).

Point it at a local repo, a GitHub URL, or an OpenAPI spec; it discovers the capabilities and
writes a runnable MCP server bundle to ./mcp_server/. Share that folder back.

Examples:
    python cli.py .                                   # the current repo, read-only tools
    python cli.py https://github.com/owner/project --all
    python cli.py ./openapi.json --risk read,write
    python cli.py /path/to/repo --name my-product --out mcp_server

Only needs python3 + git. (The GENERATED server needs `mcp` — see its requirements.txt.)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import discovery, openapi_discovery, generator  # noqa: E402

_ALL_RISKS = {"read", "write", "destructive", "sensitive"}


def _looks_openapi(path: str) -> bool:
    try:
        with open(path) as f:
            d = json.load(f)
        return isinstance(d, dict) and ("openapi" in d or "swagger" in d or "paths" in d)
    except Exception:
        return False


def _slug(s: str) -> str:
    base = os.path.basename(s.rstrip("/")).replace(".git", "").replace(".json", "")
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower() or "my-mcp"


def main():
    ap = argparse.ArgumentParser(description="Turn a codebase or OpenAPI spec into an MCP server.")
    ap.add_argument("source", help="local repo path, GitHub URL, or path to an openapi.json")
    ap.add_argument("--out", default="mcp_server", help="output directory (default: mcp_server)")
    ap.add_argument("--name", default=None, help="server name (default: derived from source)")
    ap.add_argument("--risk", default="read",
                    help="comma list of risks to expose: read,write,destructive,sensitive (default: read)")
    ap.add_argument("--all", action="store_true", help="expose every discovered capability")
    ap.add_argument("--invocation", default=None, help="http | in_process (default: per capability)")
    ap.add_argument("--base-url-env", default="TARGET_API_URL",
                    help="env var the generated server reads for the target API base URL")
    args = ap.parse_args()

    src, tmp = args.source, None
    try:
        if os.path.isfile(src) and _looks_openapi(src):
            res = openapi_discovery.discover_openapi(src, methods={"get", "post", "put", "patch", "delete"})
        else:
            if src.startswith(("http://", "https://")):
                tmp = tempfile.mkdtemp(prefix="c2mcp_")
                src_repo = os.path.join(tmp, "repo")
                print(f"cloning {args.source} …")
                subprocess.run(["git", "clone", "--depth", "1", args.source, src_repo], check=True)
                src = src_repo
            if not os.path.isdir(src):
                sys.exit(f"error: {src} is not a directory, URL, or OpenAPI file")
            res = discovery.discover(src)

        caps = res["capabilities"]
        project = res["project"]
        risks = _ALL_RISKS if args.all else {r.strip() for r in args.risk.split(",") if r.strip()}
        chosen = [c for c in caps if c["risk"] in risks]

        print(f"\nDiscovered {len(caps)} capabilities "
              f"[{project.get('language')}, {', '.join(project.get('frameworks') or []) or 'none'}]:")
        for c in caps:
            mark = "x" if c in chosen else " "
            print(f"  [{mark}] {c['risk']:11} {c['name']}")
        print(f"\nExposing {len(chosen)} (risk in {sorted(risks)}). Change with --risk or --all.")
        if not chosen:
            sys.exit("nothing selected — try --all or --risk read,write")

        opts = {"server_name": args.name or _slug(args.source),
                "invocation": args.invocation or chosen[0].get("invocation") or "http",
                "base_url_env": args.base_url_env}
        bundle = generator.generate(project, chosen, opts)
        os.makedirs(args.out, exist_ok=True)
        for fn, content in bundle.items():
            with open(os.path.join(args.out, fn), "w", encoding="utf-8") as f:
                f.write(content)
        print(f"\n✓ wrote {len(bundle)} files to {args.out}/ — {', '.join(bundle)}")
        print(f"\nRun it:    cd {args.out} && pip install -r requirements.txt && python server.py")
        print(f"Share it:  zip up {args.out}/ (or commit it) and send it back.")
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
