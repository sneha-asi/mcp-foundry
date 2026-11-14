"""Deep-understanding report.

Takes a discovered repo (repo path + discovery result) and produces a narrative
"how this codebase works" report — overview, architecture, data flow, key modules,
and where to start extending it.

Two modes, chosen automatically:
  - **ai**: when ANTHROPIC_API_KEY is set, Claude writes the narrative grounded ONLY
    in the gathered context (file tree, README, capabilities, key source excerpts).
  - **structural**: no key → a factual report built straight from the discovery data.
    Never fabricates; just reports what was found.

Stdlib-only HTTP (urllib) so this adds no new dependency.
"""
from __future__ import annotations

import os
import json
import urllib.request
import urllib.error
from pathlib import Path

MODEL = os.environ.get("MCPF_REPORT_MODEL", "claude-sonnet-4-6")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", ".next", ".turbo", "vendor", "coverage", ".idea", ".vscode"}
_README_NAMES = ("README.md", "readme.md", "README.rst", "README.txt", "README")
_ENTRY_HINTS = ("main", "app", "index", "server", "cli", "__init__")


def _iter_files(repo_path: str):
    root = Path(repo_path)
    for p in root.rglob("*"):
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
            yield p


def _file_tree(repo_path: str, max_files: int = 80) -> list[str]:
    root = Path(repo_path)
    rels = sorted(str(p.relative_to(root)) for p in _iter_files(repo_path))
    return rels[:max_files]


def _read(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def _read_readme(repo_path: str) -> str:
    for name in _README_NAMES:
        p = Path(repo_path) / name
        if p.exists():
            return _read(p, 6000)
    return ""


def _key_excerpts(repo_path: str, files: list[str], n: int = 5) -> list[dict]:
    """A few likely-important source files (entry-point names first, then largest)."""
    root = Path(repo_path)
    code = [f for f in files if Path(f).suffix in
            (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".swift")]
    entries = [f for f in code if Path(f).stem.lower() in _ENTRY_HINTS]
    rest = sorted((f for f in code if f not in entries),
                  key=lambda f: (root / f).stat().st_size if (root / f).exists() else 0,
                  reverse=True)
    picked = (entries + rest)[:n]
    return [{"path": f, "code": _read(root / f, 2500)} for f in picked]


def _gather_context(repo_path: str, result: dict) -> dict:
    files = _file_tree(repo_path)
    proj = result.get("project", {})
    caps = result.get("capabilities", [])
    return {
        "project": proj,
        "file_tree": files,
        "file_count": sum(1 for _ in _iter_files(repo_path)),
        "readme": _read_readme(repo_path),
        "capabilities": [
            {"name": c.get("name"), "kind": c.get("kind") or c.get("invocation"),
             "risk": c.get("risk"), "http": c.get("http")}
            for c in caps
        ],
        "excerpts": _key_excerpts(repo_path, files),
    }


def _stats(ctx: dict) -> dict:
    proj = ctx["project"]
    caps = ctx["capabilities"]
    by_risk: dict[str, int] = {}
    for c in caps:
        by_risk[c.get("risk") or "unknown"] = by_risk.get(c.get("risk") or "unknown", 0) + 1
    return {
        "language": proj.get("language", "unknown"),
        "frameworks": proj.get("frameworks", []),
        "files": ctx["file_count"],
        "capabilities": len(caps),
        "risk_breakdown": by_risk,
    }


def _structural_markdown(ctx: dict) -> str:
    proj = ctx["project"]
    caps = ctx["capabilities"]
    lines = [
        "## Overview",
        f"- **Primary language:** {proj.get('language', 'unknown')}",
        f"- **Frameworks detected:** {', '.join(proj.get('frameworks') or []) or 'none detected'}",
        f"- **Files scanned:** {ctx['file_count']}",
        "",
        "## Capabilities (what this code exposes)",
    ]
    if caps:
        by_risk: dict[str, list[str]] = {}
        for c in caps:
            by_risk.setdefault(c.get("risk") or "unknown", []).append(c.get("name") or "?")
        for risk in ("read", "write", "destructive", "sensitive", "unknown"):
            if by_risk.get(risk):
                lines.append(f"- **{risk}** ({len(by_risk[risk])}): "
                             + ", ".join(f"`{n}`" for n in by_risk[risk][:20]))
    else:
        lines.append("- No callable capabilities discovered.")
    lines += ["", "## Structure (top-level)"]
    tops = sorted({f.split("/")[0] for f in ctx["file_tree"]})
    lines += [f"- `{t}`" for t in tops[:25]]
    lines += ["", "> Structural report — set `ANTHROPIC_API_KEY` for the full AI narrative "
              "(architecture, data flow, where to extend)."]
    return "\n".join(lines)


def _ai_markdown(ctx: dict, api_key: str) -> str:
    excerpts = "\n\n".join(
        f"### {e['path']}\n```\n{e['code']}\n```" for e in ctx["excerpts"]
    )
    caps_txt = "\n".join(
        f"- {c['name']} (kind={c['kind']}, risk={c['risk']})" for c in ctx["capabilities"]
    ) or "(none discovered)"
    prompt = (
        "You are a senior engineer onboarding onto an unfamiliar codebase. Using ONLY the "
        "context below, write a deep-understanding report in GitHub Markdown with EXACTLY "
        "these sections:\n"
        "## Overview — what this project is and who it's for (2-3 sentences)\n"
        "## Architecture — the main components/modules and how they relate\n"
        "## How it works — the primary flow / data path, end to end\n"
        "## Tech stack — languages, frameworks, notable dependencies\n"
        "## Key modules — the 3-6 files/dirs that matter most, one line each\n"
        "## Where to start — concrete first steps to extend or build on top of it\n\n"
        "Be specific and ground every claim in the context. If something isn't determinable "
        "from the context, say so — do not invent.\n\n"
        f"PROJECT: {json.dumps(ctx['project'])}\n"
        f"FILE COUNT: {ctx['file_count']}\n"
        f"FILE TREE (truncated):\n{json.dumps(ctx['file_tree'], indent=0)}\n\n"
        f"DISCOVERED CAPABILITIES:\n{caps_txt}\n\n"
        f"README (truncated):\n{ctx['readme'][:5000]}\n\n"
        f"KEY SOURCE EXCERPTS:\n{excerpts}"
    )
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["content"][0]["text"]


def build_report(repo_path: str, result: dict) -> dict:
    """Return {mode, markdown, stats, note?}. Never raises on the AI path —
    falls back to the structural report if the model call fails."""
    ctx = _gather_context(repo_path, result)
    stats = _stats(ctx)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            return {"mode": "ai", "markdown": _ai_markdown(ctx, key), "stats": stats}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            note = f"AI report failed (HTTP {e.code}: {detail}); showing structural report."
        except Exception as e:  # noqa: BLE001
            note = f"AI report failed ({type(e).__name__}); showing structural report."
        return {"mode": "structural", "markdown": _structural_markdown(ctx),
                "stats": stats, "note": note}
    return {"mode": "structural", "markdown": _structural_markdown(ctx), "stats": stats,
            "note": "No ANTHROPIC_API_KEY set — showing the structural report. "
                    "Set the key for the full AI narrative."}
