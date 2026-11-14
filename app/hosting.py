"""Phase 3.2 — managed hosting control plane (SCAFFOLD).

Foundry hosts a generated server for the user: deploy → live MCP endpoint → connect. This module
is the control plane + a pluggable Deployer interface. Two backends are provided:

  - LocalDockerDeployer  — `docker build` + `docker run` on the host (dev / reference; verifiable
    once a Docker daemon is available).
  - FlyDeployer          — Fly Machines API (scale-to-zero; the chosen primary target). Real API
    calls, but needs FLY_API_TOKEN + an image-build step — see deploy().

VERIFIED HERE: gating, the deployer factory, the http-proxy-only guard, token generation, the
deployment registry, and command/arg construction (unit-tested without executing). NOT verifiable
here (needs live infra): the actual deploy() round-trip. Treat that as the remaining live work.

Safety stance (from docs/HOSTING_DESIGN.md): **host http-proxy servers only** (no untrusted
code-exec); per-server bearer token; secrets passed to the backend, never logged.
"""
import os
import json
import secrets
import hashlib
import threading

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import sqlalchemy as sa
    _HAS_SA = True
except ImportError:  # pragma: no cover
    _HAS_SA = False

HOSTING_BACKEND = os.environ.get("HOSTING_BACKEND", "").strip().lower()  # "", local-docker, fly
HOSTING_ENABLED = HOSTING_BACKEND in ("local-docker", "fly")
RUNNER_IMAGE = os.environ.get("MCPF_RUNNER_IMAGE", "")  # prebuilt image (Fly), optional


def new_server_token() -> str:
    """A per-hosted-server bearer token (shown once; stored only as a hash)."""
    return secrets.token_urlsafe(24)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def http_proxy_only(capabilities: list, options: dict) -> bool:
    """True iff every capability is http-proxy (safe to host — no code we'd execute)."""
    default = (options or {}).get("invocation")
    return all((c.get("invocation") or default) == "http" for c in (capabilities or []))


# --- deployers --------------------------------------------------------------------------

class LocalDockerDeployer:
    """Build + run the bundle as a container on the local Docker host (dev / reference)."""

    backend = "local-docker"

    @staticmethod
    def build_cmd(workdir: str, image: str) -> list:
        return ["docker", "build", "-t", image, workdir]

    @staticmethod
    def run_cmd(image: str, name: str, host_port: int, env: dict) -> list:
        cmd = ["docker", "run", "-d", "--name", name, "-p", f"{host_port}:8080"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(image)
        return cmd

    def deploy(self, name: str, bundle: dict, env: dict, host_port: int = 8080) -> dict:
        import tempfile
        import subprocess
        workdir = tempfile.mkdtemp(prefix="mcpf_deploy_")
        for fn, content in (bundle or {}).items():
            with open(os.path.join(workdir, fn), "w", encoding="utf-8") as f:
                f.write(content)
        image = f"mcpf/{name}:latest"
        subprocess.run(self.build_cmd(workdir, image), check=True, capture_output=True, text=True)
        proc = subprocess.run(self.run_cmd(image, name, host_port, env),
                              check=True, capture_output=True, text=True)
        return {"backend": self.backend, "ref": proc.stdout.strip(),
                "endpoint": f"http://localhost:{host_port}/mcp"}

    def destroy(self, ref: str) -> None:
        import subprocess
        subprocess.run(["docker", "rm", "-f", ref], capture_output=True, text=True)


class FlyDeployer:
    """Deploy to Fly Machines (scale-to-zero). Needs FLY_API_TOKEN + a built image (RUNNER_IMAGE
    or a per-server image pushed to registry.fly.io). Image build is the live precondition."""

    backend = "fly"
    API = "https://api.machines.dev/v1"

    def __init__(self):
        self.token = os.environ.get("FLY_API_TOKEN", "").strip()
        self.org = os.environ.get("FLY_ORG", "personal").strip()

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def deploy(self, name: str, bundle: dict, env: dict) -> dict:
        """Create the app + a scale-to-zero machine, with `env` injected as secrets.

        Precondition: a built/pushed image. Per-server images come from the bundle's Dockerfile
        built via a Fly remote builder; set MCPF_RUNNER_IMAGE to that image. Wiring that build
        pipeline is the remaining LIVE step — the Machines API calls below are ready for it.
        """
        if httpx is None or not self.token:
            raise RuntimeError("FlyDeployer needs httpx + FLY_API_TOKEN")
        image = RUNNER_IMAGE
        if not image:
            raise RuntimeError(
                "Fly deploy needs a built image: build the bundle's Dockerfile via a Fly remote "
                "builder and set MCPF_RUNNER_IMAGE — or use HOSTING_BACKEND=local-docker. "
                "(the per-server image-build pipeline is the remaining live step)"
            )
        app_name = f"{name}-mcp"
        with httpx.Client(headers=self._headers(), timeout=30) as c:
            c.post(f"{self.API}/apps", json={"app_name": app_name, "org_slug": self.org})
            if env:  # sensitive values (token, API keys) go in as Fly secrets, not plain env
                c.post(f"{self.API}/apps/{app_name}/secrets",
                       json={"secrets": [{"label": k, "value": v} for k, v in env.items()]})
            m = c.post(f"{self.API}/apps/{app_name}/machines", json={
                "config": {
                    "image": image,
                    "env": {"MCP_TRANSPORT": "http", "PORT": "8080"},
                    "services": [{
                        "ports": [{"port": 443, "handlers": ["tls", "http"]}],
                        "protocol": "tcp", "internal_port": 8080,
                        "autostop": "stop", "autostart": True, "min_machines_running": 0,
                    }],
                },
            })
            m.raise_for_status()
            ref = (m.json() or {}).get("id", "")
        return {"backend": self.backend, "ref": ref,
                "endpoint": f"https://{app_name}.fly.dev/mcp"}

    def destroy(self, ref: str) -> None:  # pragma: no cover - needs live API
        # delete the machine (and optionally the app) — left to the live implementation
        pass


def make_deployer():
    """Return the configured deployer, or None when hosting is disabled."""
    if HOSTING_BACKEND == "local-docker":
        return LocalDockerDeployer()
    if HOSTING_BACKEND == "fly":
        return FlyDeployer()
    return None


# --- deployment registry --------------------------------------------------------------
# In-memory (dev) or SQL-backed (prod). SQL persists deployments across restarts/replicas so
# the control plane never loses track of a running container. Both share one interface.

class DeploymentRegistry:
    def __init__(self):
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, *, owner: str, name: str, endpoint: str, ref: str,
               backend: str, token_h: str, now: float) -> dict:
        dep_id = secrets.token_urlsafe(8)
        rec = {"id": dep_id, "owner": owner, "name": name, "endpoint": endpoint,
               "ref": ref, "backend": backend, "token_hash": token_h,
               "status": "running", "created": now}
        with self._lock:
            self._d[dep_id] = rec
        return rec

    def get(self, dep_id: str, owner: str | None) -> dict | None:
        with self._lock:
            rec = self._d.get(dep_id)
        if rec is None:
            return None
        if owner is not None and rec["owner"] not in (None, owner):
            return None
        return rec

    def list(self, owner: str | None) -> list:
        with self._lock:
            return [r for r in self._d.values() if owner is None or r["owner"] in (None, owner)]

    def delete(self, dep_id: str) -> dict | None:
        with self._lock:
            return self._d.pop(dep_id, None)


class SqlDeploymentRegistry:
    """SQL-backed registry (Postgres in prod, SQLite for dev/tests). Survives restarts so a
    hosted container is never orphaned by a control-plane restart. Same interface as above."""

    def __init__(self, url: str):
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = sa.create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        self.meta = sa.MetaData()
        self.t = sa.Table(
            "mcpf_deployments", self.meta,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("owner", sa.String, nullable=True, index=True),
            sa.Column("name", sa.String),
            sa.Column("endpoint", sa.String),
            sa.Column("ref", sa.String),
            sa.Column("backend", sa.String),
            sa.Column("token_hash", sa.String),
            sa.Column("status", sa.String),
            sa.Column("created", sa.Float),
        )
        self.meta.create_all(self.engine)

    def create(self, *, owner: str, name: str, endpoint: str, ref: str,
               backend: str, token_h: str, now: float) -> dict:
        rec = {"id": secrets.token_urlsafe(8), "owner": owner, "name": name,
               "endpoint": endpoint, "ref": ref, "backend": backend,
               "token_hash": token_h, "status": "running", "created": now}
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.t).values(**rec))
        return rec

    def get(self, dep_id: str, owner: str | None) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(self.t).where(self.t.c.id == dep_id)).mappings().first()
        if row is None:
            return None
        if owner is not None and row["owner"] not in (None, owner):
            return None
        return dict(row)

    def list(self, owner: str | None) -> list:
        with self.engine.begin() as conn:
            rows = conn.execute(sa.select(self.t)).mappings().all()
        return [dict(r) for r in rows if owner is None or r["owner"] in (None, owner)]

    def delete(self, dep_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(self.t).where(self.t.c.id == dep_id)).mappings().first()
            conn.execute(sa.delete(self.t).where(self.t.c.id == dep_id))
        return dict(row) if row else None


def make_registry():
    """SQL registry when DATABASE_URL is set (deployments persist), else in-memory."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url and _HAS_SA:
        return SqlDeploymentRegistry(url)
    return DeploymentRegistry()


REGISTRY = make_registry()


def public(rec: dict) -> dict:
    """Registry record minus the token hash, for API responses."""
    return {k: v for k, v in rec.items() if k != "token_hash"}
