"""MCP Foundry — web backend.

Wires the discovery + generator engines behind a small HTTP API and serves the SPA.
Run from the project root:  uvicorn app.main:app --reload --port 8099
"""
import io
import os
import sys
import time
import uuid
import shutil
import zipfile
import tempfile
import secrets
import subprocess

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

# Make the sibling `engine/` package importable when run from the project root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from engine import discovery, generator, analyzer  # noqa: E402
from app import guards, store, auth, ratelimit, github_oauth, hosting  # noqa: E402

# Optional: load a local .env so ANTHROPIC_API_KEY enables the AI deep-report mode.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass

app = FastAPI(title="MCP Foundry")

# Signed session cookie (holds only an opaque session id; the GitHub token stays server-side).
# Set SESSION_SECRET in prod so sessions survive restarts/replicas; dev gets a per-process one.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)


@app.middleware("http")
async def _rate_limit(request, call_next):
    """Token-bucket rate limit on the cost-heavy endpoints (see app/ratelimit.py)."""
    bucket = ratelimit.for_path(request.url.path)
    if bucket is not None:
        ok, retry = bucket.allow(ratelimit.client_key(request), time.monotonic())
        if not ok:
            return JSONResponse(
                {"detail": "rate limit exceeded — please slow down"},
                status_code=429, headers={"Retry-After": str(int(retry) + 1)},
            )
    return await call_next(request)

# Bounded, TTL'd analysis store with temp-dir cleanup (fixes the old leak). Still single-node;
# multi-node persistence is a later phase. Tunable via env (see app/store.py, app/guards.py).
STORE = store.make_store(
    ttl_seconds=int(os.environ.get("MCPF_ANALYSIS_TTL", 3600)),
    max_entries=int(os.environ.get("MCPF_MAX_ANALYSES", 200)),
)

_TEMPLATE = os.path.join(_ROOT, "app", "templates", "index.html")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _descend_single_dir(path: str) -> str:
    """If an extracted upload is one wrapper folder, descend into it."""
    entries = [e for e in os.listdir(path) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(path, entries[0])):
        return os.path.join(path, entries[0])
    return path


@app.get("/api/config")
def config():
    """Public auth config so the browser can run the Supabase client (no secrets)."""
    return auth.public_config()


@app.post("/api/analyze")
async def analyze(request: Request, repo_url: str = Form(None), file: UploadFile = File(None),
                  repo_token: str = Form(None),
                  user: dict = Depends(auth.require_user)):
    workdir = tempfile.mkdtemp(prefix="mcpf_")
    repo_path = os.path.join(workdir, "repo")
    try:
        if repo_url:
            url = guards.validate_clone_url(repo_url)        # SSRF-safe (raises GuardError)
            token = (repo_token
                     or github_oauth.VAULT.get(request.session.get("sid"))
                     or os.environ.get("GITHUB_TOKEN", "")).strip()
            clone_url = guards.with_token(url, token)        # private-repo auth via token
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, repo_path],
                capture_output=True, text=True, timeout=guards.CLONE_TIMEOUT,
            )
            if proc.returncode != 0:
                err = guards.redact(proc.stderr.strip(), token)[:300]  # never leak the token
                raise HTTPException(400, f"git clone failed: {err}")
            guards.enforce_repo_size(repo_path)              # reject oversized repos
        elif file:
            data = await file.read()
            if not data:
                raise HTTPException(400, "uploaded file is empty")
            if len(data) > guards.MAX_UPLOAD_BYTES:
                raise HTTPException(413, "upload exceeds the size limit")
            zpath = os.path.join(workdir, "upload.zip")
            with open(zpath, "wb") as fh:
                fh.write(data)
            os.makedirs(repo_path, exist_ok=True)
            guards.safe_extract(zpath, repo_path)            # zip-bomb / zip-slip safe
            repo_path = _descend_single_dir(repo_path)
        else:
            raise HTTPException(400, "provide either repo_url or a file upload")

        result = discovery.discover(repo_path)
        aid = uuid.uuid4().hex[:12]
        STORE.put(aid, repo_path=repo_path, result=result, workdir=workdir,
                  now=time.time(), owner=user["id"])
        return {"analysis_id": aid, **result}
    except guards.GuardError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(400, str(e))
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(504, "git clone timed out")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, f"analysis failed: {type(e).__name__}: {e}")


class GenReq(BaseModel):
    analysis_id: str
    server_name: str = "my-mcp"
    invocation: str = "http"
    selected_ids: list[str] = []
    base_url_env: str = "TARGET_API_URL"


def _build_files(req: GenReq, owner: str) -> dict:
    a = STORE.get(req.analysis_id, time.time(), owner=owner)
    if not a:
        raise HTTPException(404, "unknown or expired analysis_id — re-run analyze")
    chosen = set(req.selected_ids)
    caps = [c for c in a["result"]["capabilities"] if c["id"] in chosen]
    if not caps:
        raise HTTPException(400, "select at least one capability to expose")
    return generator.generate(
        a["result"]["project"], caps,
        {"server_name": req.server_name, "invocation": req.invocation,
         "base_url_env": req.base_url_env},
    )


@app.post("/api/generate")
def generate_preview(req: GenReq, user: dict = Depends(auth.require_user)):
    """Return the generated files as JSON so the UI can preview them inline."""
    return {"files": _build_files(req, user["id"])}


@app.post("/api/download")
def download(req: GenReq, user: dict = Depends(auth.require_user)):
    """Return the generated bundle as a downloadable zip."""
    files = _build_files(req, user["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(f"mcp_server/{name}", content)
    buf.seek(0)
    safe = "".join(c for c in req.server_name if c.isalnum() or c in "-_") or "mcp-server"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}-mcp.zip"'},
    )


class ReportReq(BaseModel):
    analysis_id: str


@app.post("/api/report")
def deep_report(req: ReportReq, user: dict = Depends(auth.require_user)):
    """Deep-understanding report for an analyzed repo (architecture, flow, where to extend).

    Reuses the repo already cloned/extracted by /api/analyze. Returns {mode, markdown,
    stats, note?}; AI narrative when ANTHROPIC_API_KEY is set, structural otherwise.
    """
    a = STORE.get(req.analysis_id, time.time(), owner=user["id"])
    if not a:
        raise HTTPException(404, "unknown or expired analysis_id — re-run analyze")
    return analyzer.build_report(a["repo_path"], a["result"])


class HostReq(GenReq):
    target_env: dict = {}  # env the proxied API needs (e.g. TARGET_API_URL, TARGET_API_KEY)
    egress: list = []      # extra allowed egress hosts (default: the target host only)


@app.post("/api/host")
def host(req: HostReq, user: dict = Depends(auth.require_user)):
    """Deploy a curated analysis as a managed, hosted MCP endpoint. http-proxy servers only."""
    if not hosting.HOSTING_ENABLED:
        raise HTTPException(503, "managed hosting is not configured (set HOSTING_BACKEND)")
    a = STORE.get(req.analysis_id, time.time(), owner=user["id"])
    if not a:
        raise HTTPException(404, "unknown or expired analysis_id — re-run analyze")
    caps = [c for c in a["result"]["capabilities"] if c["id"] in set(req.selected_ids)]
    if not caps:
        raise HTTPException(400, "select at least one capability to host")
    if not hosting.http_proxy_only(caps, {"invocation": req.invocation}):
        raise HTTPException(400, "managed hosting supports http-proxy servers only — "
                                 "in-process servers can be self-hosted from the download")
    bundle = generator.generate(a["result"]["project"], caps, {
        "server_name": req.server_name, "invocation": req.invocation,
        "base_url_env": req.base_url_env})
    token = hosting.new_server_token()
    env = {"MCP_SERVER_TOKEN": token, **(req.target_env or {})}
    if req.egress:
        env["MCP_EGRESS_ALLOW"] = ",".join(req.egress)
    try:
        result = hosting.make_deployer().deploy(req.server_name, bundle, env)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"deploy failed: {type(e).__name__}: {e}")
    rec = hosting.REGISTRY.create(
        owner=user["id"], name=req.server_name, endpoint=result["endpoint"],
        ref=result["ref"], backend=result["backend"],
        token_h=hosting.token_hash(token), now=time.time())
    # token is returned ONCE (stored only as a hash) — include the connect snippet
    return {**hosting.public(rec), "token": token,
            "connect": {"url": result["endpoint"], "authorization": f"Bearer {token}"}}


@app.get("/api/host")
def host_list(user: dict = Depends(auth.require_user)):
    if not hosting.HOSTING_ENABLED:
        raise HTTPException(503, "managed hosting is not configured")
    return [hosting.public(r) for r in hosting.REGISTRY.list(user["id"])]


@app.delete("/api/host/{dep_id}")
def host_destroy(dep_id: str, user: dict = Depends(auth.require_user)):
    if not hosting.HOSTING_ENABLED:
        raise HTTPException(503, "managed hosting is not configured")
    rec = hosting.REGISTRY.get(dep_id, user["id"])
    if not rec:
        raise HTTPException(404, "unknown deployment")
    try:
        hosting.make_deployer().destroy(rec["ref"])
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass
    hosting.REGISTRY.delete(dep_id)
    return {"deleted": dep_id}


@app.get("/api/github/status")
def github_status(request: Request):
    """Whether GitHub OAuth is configured, and whether this session has connected."""
    sid = request.session.get("sid")
    return {"enabled": github_oauth.ENABLED, "connected": github_oauth.VAULT.has(sid)}


@app.get("/auth/github/login")
def github_login(request: Request):
    """Kick off the GitHub OAuth flow (302 to GitHub's consent screen)."""
    if not github_oauth.ENABLED:
        raise HTTPException(404, "GitHub OAuth is not configured")
    sid = request.session.get("sid") or secrets.token_urlsafe(16)
    request.session["sid"] = sid
    state = secrets.token_urlsafe(16)
    request.session["gh_state"] = state
    return RedirectResponse(github_oauth.authorize_url(github_oauth.redirect_uri(request), state))


@app.get("/auth/github/callback")
def github_callback(request: Request, code: str = "", state: str = ""):
    """GitHub redirects here with a code; verify state, exchange it, store the token server-side."""
    if not github_oauth.ENABLED:
        raise HTTPException(404, "GitHub OAuth is not configured")
    if not code or not state or state != request.session.get("gh_state"):
        raise HTTPException(400, "invalid OAuth state — start the sign-in again")
    request.session.pop("gh_state", None)
    try:
        token = github_oauth.exchange_code(code, github_oauth.redirect_uri(request))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"GitHub token exchange failed: {type(e).__name__}")
    sid = request.session.get("sid") or secrets.token_urlsafe(16)
    request.session["sid"] = sid
    github_oauth.VAULT.set(sid, token)
    return RedirectResponse("/")


@app.get("/api/health")
def health():
    return {"ok": True, "analyses_in_memory": len(STORE)}
