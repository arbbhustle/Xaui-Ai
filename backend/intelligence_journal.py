"""Persistent earliest-publication identity; captured in snapshots for pure replay."""
import re
from .domain import digest, parse


def remember_news(conn, envelope, now):
    conn.execute("CREATE TABLE IF NOT EXISTS phase3a_news_identity (identity TEXT PRIMARY KEY, story TEXT NOT NULL, first_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS phase3a_news_story ON phase3a_news_identity(story)")
    if envelope["status"] != "OK":
        return
    if not parse(envelope["as_of"]) <= parse(envelope["retrieved_at"]) <= now:
        return
    rows = envelope["records"]
    if any(parse(r["published_at"]) > parse(envelope["as_of"]) for r in rows):
        return
    pending = []
    for row in rows:
        keys = [digest([envelope["data_mode"], kind, value]) for kind, value in (
            ("id", [row["source"], row["id"]]), ("url", row["url"]),
            ("title", " ".join(re.findall(r"\w+", row["title"].lower()))))]
        existing = list(conn.execute("SELECT story,first_at FROM phase3a_news_identity WHERE identity IN (?,?,?)", keys))
        stories = {r[0] for r in existing}
        root = min(stories or {min(keys)})
        first = min([row["published_at"]] + [r[1] for r in existing])
        for story in stories:
            conn.execute("UPDATE phase3a_news_identity SET story=?,first_at=? WHERE story=?", (root, first, story))
        for key in keys:
            conn.execute("INSERT OR REPLACE INTO phase3a_news_identity VALUES (?,?,?)", (key, root, first))
        pending.append((row, keys[0]))
    for row, key in pending:
        row["first_published_at"] = conn.execute("SELECT first_at FROM phase3a_news_identity WHERE identity=?", (key,)).fetchone()[0]
