"""Analysis store with TTL + capacity eviction, and temp-dir cleanup.

v1 kept analyses in a bare dict that grew forever and never deleted the temp dirs it cloned
into — a memory + disk leak. This bounds both: entries expire after TTL and the oldest are
evicted past a capacity cap, and every removal deletes the on-disk workdir.

Still in-process (single-node). Multi-node persistence (Postgres) is a later phase — but the
leak and unbounded growth are fixed here.
"""
import os
import shutil
import threading
from collections import OrderedDict

try:
    import sqlalchemy as sa
    _HAS_SA = True
except ImportError:  # pragma: no cover
    _HAS_SA = False


class AnalysisStore:
    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 200):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()

    def _evict_workdir(self, entry: dict) -> None:
        wd = entry.get("workdir")
        if wd and os.path.isdir(wd):
            shutil.rmtree(wd, ignore_errors=True)

    def _sweep(self, now: float) -> None:
        # expire by TTL
        for aid in [k for k, v in self._data.items() if now - v["created"] > self.ttl]:
            self._evict_workdir(self._data.pop(aid))
        # evict oldest past capacity
        while len(self._data) > self.max_entries:
            _, entry = self._data.popitem(last=False)
            self._evict_workdir(entry)

    def put(self, aid: str, *, repo_path: str, result: dict, workdir: str, now: float,
            owner: str | None = None) -> None:
        with self._lock:
            self._data[aid] = {"repo_path": repo_path, "result": result,
                               "workdir": workdir, "created": now, "owner": owner}
            self._data.move_to_end(aid)
            self._sweep(now)

    def get(self, aid: str, now: float, owner: str | None = None) -> dict | None:
        with self._lock:
            entry = self._data.get(aid)
            if entry is None:
                return None
            if now - entry["created"] > self.ttl:
                self._evict_workdir(self._data.pop(aid))
                return None
            # ownership check: a caller can only see their own analyses (None owner = open mode)
            if owner is not None and entry.get("owner") not in (None, owner):
                return None
            self._data.move_to_end(aid)
            return entry

    def __len__(self) -> int:
        return len(self._data)


class SqlAnalysisStore:
    """SQL-backed analysis store (Postgres in prod, SQLite for dev/tests — same code).

    Persists analyses across restarts and is safe for multiple workers/replicas that share the
    database: analyze + generate + download work cross-worker (they only need the stored result
    JSON). NOTE: the deep-report reads the cloned repo files, which live on the node that did the
    clone — so report is best-effort cross-node (falls back to a clear error if the workdir isn't
    on the handling node). Same TTL + capacity + workdir-cleanup semantics as AnalysisStore.
    """

    def __init__(self, url: str, ttl_seconds: int = 3600, max_entries: int = 2000):
        if not _HAS_SA:
            raise RuntimeError("DATABASE_URL is set but SQLAlchemy is not installed")
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = sa.create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        self.meta = sa.MetaData()
        self.t = sa.Table(
            "mcpf_analyses", self.meta,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("owner", sa.String, nullable=True),
            sa.Column("created", sa.Float, nullable=False, index=True),
            sa.Column("repo_path", sa.String, nullable=True),
            sa.Column("workdir", sa.String, nullable=True),
            sa.Column("result", sa.JSON, nullable=False),
        )
        self.meta.create_all(self.engine)

    @staticmethod
    def _rmtree(wd):
        if wd and os.path.isdir(wd):
            shutil.rmtree(wd, ignore_errors=True)

    def _sweep(self, conn, now: float) -> None:
        cutoff = now - self.ttl
        for (wd,) in conn.execute(sa.select(self.t.c.workdir).where(self.t.c.created < cutoff)):
            self._rmtree(wd)
        conn.execute(sa.delete(self.t).where(self.t.c.created < cutoff))
        n = conn.execute(sa.select(sa.func.count()).select_from(self.t)).scalar_one()
        if n > self.max_entries:
            old = conn.execute(
                sa.select(self.t.c.id, self.t.c.workdir)
                .order_by(self.t.c.created.asc()).limit(n - self.max_entries)
            ).all()
            for _id, wd in old:
                self._rmtree(wd)
            conn.execute(sa.delete(self.t).where(self.t.c.id.in_([r[0] for r in old])))

    def put(self, aid: str, *, repo_path: str, result: dict, workdir: str, now: float,
            owner: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.delete(self.t).where(self.t.c.id == aid))
            conn.execute(sa.insert(self.t).values(
                id=aid, owner=owner, created=now,
                repo_path=repo_path, workdir=workdir, result=result))
            self._sweep(conn, now)

    def get(self, aid: str, now: float, owner: str | None = None) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(self.t).where(self.t.c.id == aid)).mappings().first()
            if row is None:
                return None
            if now - row["created"] > self.ttl:
                self._rmtree(row["workdir"])
                conn.execute(sa.delete(self.t).where(self.t.c.id == aid))
                return None
            if owner is not None and row["owner"] not in (None, owner):
                return None
            return {"repo_path": row["repo_path"], "result": row["result"],
                    "workdir": row["workdir"], "created": row["created"], "owner": row["owner"]}

    def __len__(self) -> int:
        with self.engine.begin() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(self.t)).scalar_one()


def make_store(ttl_seconds: int = 3600, max_entries: int = 200):
    """Pick the store backend: Postgres/SQL when DATABASE_URL is set, else in-memory TTL store."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return SqlAnalysisStore(url, ttl_seconds=ttl_seconds, max_entries=max_entries)
    return AnalysisStore(ttl_seconds=ttl_seconds, max_entries=max_entries)
