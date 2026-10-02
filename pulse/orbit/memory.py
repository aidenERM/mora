import hashlib
import json
import re
import time
import uuid

from .security import clean

CATEGORIES = {"preference", "style", "people", "project", "decision", "goal", "episode"}


class Memory:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, category TEXT, text TEXT, source TEXT,
                    context TEXT, created REAL, updated REAL, expires REAL, fingerprint TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS memories_lookup ON memories(category,updated);
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY, role TEXT, text TEXT, source TEXT, created REAL
                );
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, text TEXT,
                    artifact TEXT, status TEXT DEFAULT 'pending', created REAL, message_id TEXT,
                    UNIQUE(task_id,kind)
                );
            ''')

    def remember(self, text, category="preference", source="user", context="", days=None):
        if category not in CATEGORIES or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
            raise ValueError("invalid memory")
        text = clean(text.strip())
        if "[credential removed]" in text:
            raise ValueError("credentials cannot become memory")
        fingerprint = hashlib.sha256((category + ":" + text.casefold()).encode()).hexdigest()
        now, memory_id = time.time(), uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET updated=excluded.updated,source=excluded.source,context=excluded.context",
                       (memory_id, category, text, source[:160], clean(context)[:300], now, now, now + days * 86400 if days else None, fingerprint))
            memory_id = db.execute("SELECT id FROM memories WHERE fingerprint=?", (fingerprint,)).fetchone()[0]
        return memory_id

    def retrieve(self, query, limit=5):
        terms = re.findall(r"\w{3,}", query.lower())[:8]
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE expires IS NULL OR expires>? ORDER BY updated DESC LIMIT 200", (time.time(),)).fetchall()
        ranked = []
        for row in rows:
            score = sum(term in row["text"].lower() for term in terms)
            score += int(row["category"] in {"preference", "style"})
            if score:
                ranked.append((score, row["updated"], dict(row)))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [{key: row[key] for key in ("id", "category", "text", "source", "updated")} for _, _, row in ranked[:min(limit, 6)]]

    def forget(self, memory_id):
        with self.store.connect() as db:
            return db.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount == 1

    def conversation(self, role, text, source="discord"):
        with self.store.connect() as db:
            db.execute("INSERT INTO conversations(role,text,source,created) VALUES(?,?,?,?)", (role, clean(text)[:2000], source, time.time()))
            db.execute("DELETE FROM conversations WHERE created<? OR id NOT IN (SELECT id FROM conversations ORDER BY id DESC LIMIT 40)", (time.time() - 14 * 86400,))

    def recent(self):
        with self.store.connect() as db:
            return [dict(row) for row in reversed(db.execute("SELECT role,text FROM conversations ORDER BY id DESC LIMIT 4").fetchall())]

    def notice(self, task_id, kind, text, artifact=""):
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO notices(task_id,kind,text,artifact,created) VALUES(?,?,?,?,?)", (task_id, kind, clean(text)[:4000], artifact, time.time()))

    def claim_notice(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM notices WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE notices SET status='sending' WHERE id=?", (row["id"],))
                return dict(row)

    def notice_done(self, notice_id, status, message_id=""):
        with self.store.connect() as db:
            db.execute("UPDATE notices SET status=?,message_id=? WHERE id=?", (status, message_id, notice_id))

    def recover_notices(self):
        with self.store.connect() as db:
            # Discord may have received an interrupted send. Do not blindly duplicate it.
            db.execute("UPDATE notices SET status='unknown' WHERE status='sending'")
