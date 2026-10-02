"""Read processed Pulse context/history without syncing providers or exposing tokens."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from pulse_app.config import load_config
from pulse_app.integrations import CONTEXT_SOURCE_PRIORITY


def read_context(query=None, config=None):
    config = config or load_config()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    db = sqlite3.connect("file:" + config["DATABASE_PATH"] + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if query is not None:
            if not isinstance(query, str) or len(query) > 120:
                raise ValueError("query must be text up to 120 characters")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = db.execute("SELECT id,title,topic,score,summary,url,published_at,notification_reason FROM events WHERE discovered_at>=? AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\') ORDER BY relevant DESC,score DESC LIMIT 5",
                ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "%" + escaped + "%", "%" + escaped + "%"))
            return {"events": [{**dict(row), "summary": str(row["summary"] or "")[:500]} for row in rows]}
        selected = {}
        for row in db.execute("SELECT rowid,* FROM context_signals WHERE (expires_at IS NULL OR expires_at>=?) AND kind IN ('physical_context','mode','state','calendar','battery','charging','focus','sleep') ORDER BY observed_at DESC,rowid DESC", (now,)):
            rank = (CONTEXT_SOURCE_PRIORITY.get(row["source"], 0), row["confidence"], row["observed_at"], row["rowid"])
            if row["kind"] not in selected or rank > selected[row["kind"]][0]:
                selected[row["kind"]] = (rank, row)
        return {"context": [{"kind": row["kind"], "value": json.loads(row["value_json"]), "source": row["source"], "expires_at": row["expires_at"]} for _, row in selected.values()]}
    finally:
        db.close()
