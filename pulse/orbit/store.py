import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

STATES = {"queued", "running", "waiting", "waiting_approval", "completed", "failed", "blocked", "cancelled"}


class Store:
    def __init__(self, path=None):
        self.path = str(path or os.environ.get("ORBIT_DATABASE_PATH", "/var/lib/pulse/orbit/orbit.sqlite3"))
        directory = Path(self.path).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                    model TEXT NOT NULL, max_steps INTEGER NOT NULL, steps INTEGER DEFAULT 0,
                    result TEXT DEFAULT '', error TEXT DEFAULT '', created REAL, updated REAL,
                    wake_at REAL, source TEXT, source_key TEXT UNIQUE, metadata TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status,wake_at,created);
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY, task_id TEXT, action TEXT, arguments TEXT,
                    status TEXT, observation TEXT DEFAULT '{}', created REAL, updated REAL
                );
                CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id,id);
                CREATE TABLE IF NOT EXISTS approvals (
                    step_id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, reason TEXT,
                    created REAL, expires REAL
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            ''')
        Path(self.path).chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, goal, model="auto", max_steps=12, source="cli", source_key=None, metadata=None, wake_at=None):
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 8000:
            raise ValueError("goal must contain 1–8000 characters")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 40:
            raise ValueError("max_steps must be between 1 and 40")
        if model not in {"auto", "aws", "sol", "codex", "fast", "strong", "openai"}:
            raise ValueError("unknown model selection")
        task_id, now = uuid.uuid4().hex, time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO tasks(id,goal,status,model,max_steps,created,updated,source,source_key,metadata,wake_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (task_id, goal.strip(), "waiting" if wake_at else "queued", model, max_steps, now, now, source, source_key, json.dumps(metadata or {}), wake_at))
            if source_key:
                task_id = db.execute("SELECT id FROM tasks WHERE source_key=?", (source_key,)).fetchone()[0]
        return self.task(task_id)

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("task not found")
        task = dict(row)
        task["metadata"] = json.loads(task["metadata"])
        return task

    def list(self, limit=25):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,goal,status,model,result,error,updated FROM tasks ORDER BY created DESC LIMIT ?", (min(limit, 100),))]

    def change(self, task_id, status, result="", error="", wake_at=None):
        if status not in STATES:
            raise ValueError("invalid task state")
        with self.connect() as db:
            # Cancellation is terminal, even if a model or tool was in flight.
            db.execute("UPDATE tasks SET status=?,result=?,error=?,wake_at=?,updated=? WHERE id=? AND status!='cancelled'",
                       (status, result[:4000], error[:160], wake_at, time.time(), task_id))

    def cancel(self, task_id):
        with self.connect() as db:
            db.execute("UPDATE tasks SET status='cancelled',updated=? WHERE id=? AND status NOT IN ('completed','cancelled')", (time.time(), task_id))
        return self.task(task_id)

    def resume(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row[0] not in {"blocked", "failed", "waiting"}:
                raise ValueError("task cannot be resumed in its current state")
            db.execute("UPDATE steps SET status='interrupted',observation=? WHERE task_id=? AND status='started'",
                       (json.dumps({"error": "unknown outcome; inspect current state before retrying"}), task_id))
            db.execute("UPDATE tasks SET status='queued',error='',wake_at=NULL,updated=? WHERE id=?", (time.time(), task_id))
        return self.task(task_id)

    def claim(self):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE tasks SET status='queued' WHERE status='waiting' AND wake_at<=?", (now,))
            row = db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET status='running',updated=? WHERE id=?", (now, row[0]))
        return self.task(row[0])

    def start_step(self, task_id, action, arguments):
        now = time.time()
        with self.connect() as db:
            cursor = db.execute("INSERT INTO steps(task_id,action,arguments,status,created,updated) VALUES(?,?,?,'started',?,?)", (task_id, action, json.dumps(arguments), now, now))
            db.execute("UPDATE tasks SET steps=steps+1,updated=? WHERE id=?", (now, task_id))
            return cursor.lastrowid

    def finish_step(self, step_id, observation, status="observed"):
        with self.connect() as db:
            db.execute("UPDATE steps SET status=?,observation=?,updated=? WHERE id=?", (status, json.dumps(observation), time.time(), step_id))

    def history(self, task_id, limit=8):
        with self.connect() as db:
            rows = list(db.execute("SELECT * FROM steps WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)))
        return [{**dict(row), "arguments": json.loads(row["arguments"]), "observation": json.loads(row["observation"])} for row in reversed(rows)]

    def require_approval(self, step_id, task_id, reason):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO approvals VALUES(?,?,'pending',?,?,?)", (step_id, task_id, reason[:200], time.time(), time.time() + 86400))
        self.finish_step(step_id, {"approval_required": reason}, "approval")
        self.change(task_id, "waiting_approval")

    def approve(self, step_id, accepted):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM approvals WHERE step_id=?", (step_id,)).fetchone()
            if not row or row["status"] != "pending" or row["expires"] <= time.time():
                raise ValueError("approval missing, expired, or already resolved")
            task = db.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if task[0] != "waiting_approval":
                raise ValueError("task no longer awaiting approval")
            db.execute("UPDATE approvals SET status=? WHERE step_id=?", ("approved" if accepted else "denied", step_id))
            db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", ("queued" if accepted else "cancelled", time.time(), row["task_id"]))

    def approved_step(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT s.* FROM steps s JOIN approvals a ON a.step_id=s.id WHERE s.task_id=? AND a.status='approved' AND s.status='approval'", (task_id,)).fetchone()
        return {**dict(row), "arguments": json.loads(row["arguments"])} if row else None

    def recover(self):
        with self.connect() as db:
            # Unknown outcomes must be inspected, never silently replayed.
            for row in db.execute("SELECT id FROM tasks WHERE status='running'").fetchall():
                inflight = db.execute("SELECT id FROM steps WHERE task_id=? AND status='started'", (row[0],)).fetchone()
                db.execute("UPDATE tasks SET status=?,error=?,updated=? WHERE id=?", ("blocked" if inflight else "queued", "interrupted_tool_outcome_unknown" if inflight else "", time.time(), row[0]))

    def state(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
                return value
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
