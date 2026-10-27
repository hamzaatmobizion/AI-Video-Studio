"""SQLite job store: jobs + key/value state. All worker state lives here (crash-safe)."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  ref TEXT,
  prompt TEXT NOT NULL,
  aspect TEXT NOT NULL,
  images TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,            -- queued | running | done | failed | cancelled
  error TEXT,
  warnings TEXT NOT NULL DEFAULT '[]',
  attempts INTEGER NOT NULL DEFAULT 0,       -- attempts that counted toward MAX_RETRIES
  interruptions INTEGER NOT NULL DEFAULT 0,  -- worker restarts while running
  clip_path TEXT,
  kind TEXT NOT NULL DEFAULT 'clip',         -- clip | plan (Gemini writes scene prompts)
  provider TEXT NOT NULL DEFAULT 'gemini',   -- clip jobs: gemini | flow (where the clip is made)
  scene_count INTEGER,                       -- plan jobs: how many scene prompts to write
  result TEXT,                               -- plan jobs: JSON {bible, scenes} (older: a list of scenes)
  options TEXT,                              -- plan jobs: JSON {cast, style, language, rules}
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  not_before REAL,                           -- a retry waits until then (Gemini errors pass with time)
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT NOT NULL, detail TEXT, at REAL NOT NULL
);
"""


# Which queue a job waits in: anything Google Flow makes (clips, a Flow video's first-frame stills) in Flow's, a Gemini
# clip in Gemini's, everything else (plans, portraits, checks) in Gemini's chat. Each can be paused on its own.
LANE_SQL = "(CASE WHEN provider='flow' THEN 'flow' WHEN kind='clip' THEN provider ELSE 'chat' END)"


def lane(job: dict) -> str:
    provider = job.get("provider") or "gemini"
    if provider == "flow":
        return "flow"
    return provider if job.get("kind", "clip") == "clip" else "chat"


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'clip'"), ("scene_count", "INTEGER"),
                             ("result", "TEXT"), ("options", "TEXT"), ("not_before", "REAL"),
                             ("provider", "TEXT NOT NULL DEFAULT 'gemini'")):
                if col not in have:  # databases made before plan jobs (or their options) existed
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        path.chmod(0o600)

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    # ---- jobs --------------------------------------------------------------
    def create(self, prompt: str, aspect: str, ref: str | None, images: list[str] | None = None,
               job_id: str | None = None, kind: str = "clip", scene_count: int | None = None,
               options: dict | None = None, provider: str = "gemini") -> str:
        jid = job_id or uuid.uuid4().hex[:16]
        now = time.time()
        self._x("INSERT INTO jobs (id, ref, prompt, aspect, images, status, kind, provider, scene_count, options, "
                "created_at, updated_at) VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, ?, ?)",
                (jid, ref, prompt, aspect, json.dumps(images or []), kind, provider, scene_count,
                 json.dumps(options) if options else None, now, now))
        self.event(jid, "created")
        return jid

    def get(self, jid: str) -> dict | None:
        row = self._x("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["images"] = json.loads(d["images"])
        d["warnings"] = json.loads(d["warnings"])
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        d["options"] = json.loads(d["options"]) if d.get("options") else {}
        return d

    def update(self, jid: str, **fields) -> None:
        for key in ("warnings", "result"):
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key])
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))

    def set_images(self, jid: str, images: list[str]) -> None:
        self._x("UPDATE jobs SET images=? WHERE id=?", (json.dumps(images), jid))

    def next_queued(self, skip_lanes: tuple[str, ...] = ()) -> dict | None:
        """The oldest runnable queued job, leaving out the lanes (see `lane`) that are paused."""
        skip = ",".join("?" * len(skip_lanes))
        row = self._x("SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0 "
                      "AND (not_before IS NULL OR not_before <= ?) "
                      + (f"AND {LANE_SQL} NOT IN ({skip}) " if skip_lanes else "")
                      + "ORDER BY created_at LIMIT 1",
                      (time.time(), *skip_lanes)).fetchone()
        return self.get(row["id"]) if row else None

    def by_status(self, status: str) -> list[dict]:
        return [self.get(r["id"]) for r in self._x("SELECT id FROM jobs WHERE status=? ORDER BY created_at",
                                                    (status,)).fetchall()]

    def count(self, status: str) -> int:
        return self._x("SELECT COUNT(*) FROM jobs WHERE status=?", (status,)).fetchone()[0]

    def recover(self) -> list[str]:
        """On startup: interrupted jobs go back to the queue once; a second interruption fails them."""
        touched = []
        for job in self.by_status("running"):
            n = job["interruptions"] + 1
            if n >= 2:
                self.update(job["id"], status="failed", interruptions=n,
                            error="The worker stopped twice while making this video. Retry it.")
            else:
                self.update(job["id"], status="queued", interruptions=n)
            self.event(job["id"], "recovered", f"interruptions={n}")
            touched.append(job["id"])
        return touched

    # ---- kv / events ------------------------------------------------------
    def kv_get(self, key: str, default=None):
        row = self._x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value) -> None:
        self._x("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def kv_del(self, key: str) -> None:
        self._x("DELETE FROM kv WHERE key=?", (key,))

    def event(self, jid: str | None, kind: str, detail: str | None = None) -> None:
        self._x("INSERT INTO events (job_id, kind, detail, at) VALUES (?,?,?,?)", (jid, kind, detail, time.time()))
