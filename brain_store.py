"""Transactional outbox, inbox ingestion, search and backup."""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from brain_core import (MAX_FILE, SOURCES, clean, digest, encoded, files, iso, message,
                        outside_git, read_bytes, record)

LOG = logging.getLogger("secondbrain")
PACKET = re.compile(r"([0-9a-f]{64})\.json")
ROLES = {"user", "assistant", "mail", "document"}
STOPWORDS = set("aan als bij dat de den deze die dit een en er het hoe in is je kan met na naar om op te tot uit van voor waar waarom was wat welke we wie wij zijn".split())


@contextmanager
def database(path, schema=""):
    conn = sqlite3.connect(path, timeout=45)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if schema:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(schema)
        with conn:
            yield conn
    finally:
        conn.close()


CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned (
    path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    digest TEXT PRIMARY KEY, payload BLOB, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS outlook_cursor (
    store TEXT PRIMARY KEY, since TEXT NOT NULL
);
"""


def stage(conn, rows):
    count = 0
    for row in rows:
        if row:
            payload = encoded(row)
            if len(payload) > MAX_FILE:
                raise ValueError("Genormaliseerd record overschrijdt de bestandsgrootte")
            count += conn.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?, NULL)",
                                  (digest(payload), payload)).rowcount
    return count


def publish(directory, name, payload):
    temp = directory / f".{uuid4().hex}.part"
    try:
        with temp.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, directory / name)
    finally:
        temp.unlink(missing_ok=True)


def flush(conn, inbox, permitted):
    sent = 0
    failure = False
    conn.execute("BEGIN IMMEDIATE")
    for row in conn.execute("SELECT digest, payload FROM outbox WHERE payload IS NOT NULL ORDER BY rowid"):
        packet = json.loads(row["payload"])
        if not permitted(packet):
            continue
        try:
            publish(inbox, row["digest"] + ".json", row["payload"])
        except OSError:
            failure = True
            break
        conn.execute("UPDATE outbox SET payload=NULL, sent_at=CURRENT_TIMESTAMP WHERE digest=?", (row["digest"],))
        sent += 1
    return sent, failure


SERVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive (
    key INTEGER PRIMARY KEY, family TEXT NOT NULL, source_id TEXT NOT NULL,
    source TEXT NOT NULL, title TEXT NOT NULL, project TEXT NOT NULL,
    created REAL NOT NULL, changed REAL NOT NULL, messages INTEGER NOT NULL,
    digest TEXT NOT NULL, payload BLOB NOT NULL, UNIQUE (family, source_id)
);
CREATE TABLE IF NOT EXISTS receipt (
    digest TEXT PRIMARY KEY, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS knowledge (
    key INTEGER PRIMARY KEY, archive_key INTEGER NOT NULL REFERENCES archive(key),
    archive_digest TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (archive_key, archive_digest, category, title, status)
);
CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
    title, project, body, tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title, project, body, tokenize='unicode61 remove_diacritics 2'
);
"""


def validated(payload):
    raw = json.loads(payload)
    if not isinstance(raw, dict) or raw.get("source") not in SOURCES:
        raise ValueError("Onbekende bron")
    messages = [message(msg["role"], msg["text"], msg["at"], msg.get("id"), msg.get("refs", []))
                for msg in raw["messages"] if msg["role"] in ROLES]
    row = record(raw["source"], raw["id"], raw["title"], raw["project"], messages,
                 created=raw["created"], changed=raw["changed"], cwd=raw.get("cwd"),
                 repository=raw.get("repository"), ref=raw.get("ref"))
    if row is None:
        raise ValueError("Ongeldig record")
    return row


def ingest(conn, inbox, accepted):
    stats = {"received": 0, "updated": 0, "duplicates": 0, "rejected": 0}
    for path in files(inbox, "*.json"):
        match = PACKET.fullmatch(path.name)
        if not match:
            continue
        try:
            payload = read_bytes(path)
            hashed = digest(payload)
            if hashed != match[1]:
                raise ValueError("Hash komt niet overeen")
            row = validated(payload)
            if row["source"] not in accepted:
                raise ValueError("Bron niet toegelaten op de server")
            with conn:
                if conn.execute("INSERT OR IGNORE INTO receipt(digest) VALUES (?)", (hashed,)).rowcount:
                    stats["received"] += 1
                    stats["updated"] += save_archive(conn, row)
                else:
                    stats["duplicates"] += 1
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            stats["rejected"] += 1
            # Invalid transport copies are retained under a non-processable name for inspection.
            try:
                path.replace(path.with_suffix(".rejected"))
            except FileNotFoundError:
                pass
    return stats


def save_archive(conn, row):
    family = "copilot" if row["source"].startswith("copilot-") else row["source"]
    payload = encoded(row)
    found = conn.execute("""
        INSERT INTO archive(family,source_id,source,title,project,created,changed,messages,digest,payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(family,source_id) DO UPDATE SET
          source=excluded.source, title=excluded.title, project=excluded.project, created=excluded.created,
          changed=excluded.changed, messages=excluded.messages, digest=excluded.digest, payload=excluded.payload
        WHERE excluded.digest != archive.digest AND (
          excluded.changed > archive.changed OR
          (excluded.changed = archive.changed AND excluded.messages >= archive.messages))
        RETURNING key
        """, (family, row["id"], row["source"], row["title"], row["project"], row["created"],
              row["changed"], len(row["messages"]), digest(payload), payload)).fetchone()
    if found is None:
        return 0
    conn.execute("DELETE FROM archive_fts WHERE rowid=?", (found["key"],))
    conn.execute("INSERT INTO archive_fts(rowid,title,project,body) VALUES (?,?,?,?)",
                 (found["key"], row["title"], row["project"], "\n\n".join(m["text"] for m in row["messages"])))
    return 1


def search(conn, query, project, source, limit):
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))
    terms = [word for word in words if word not in STOPWORDS] or words
    if not terms:
        return []
    expression = " OR ".join(f'"{word}"' for word in terms[:30])
    params = (expression, project, project, source, source, limit)
    raw = conn.execute("""
        SELECT a.*, snippet(archive_fts,2,'[',']',' … ',36) AS excerpt,
               bm25(archive_fts,5.0,3.0,1.0) AS score
        FROM archive_fts JOIN archive a ON a.key=archive_fts.rowid
        WHERE archive_fts MATCH ? AND (? IS NULL OR a.project=?) AND (? IS NULL OR a.source=?)
        ORDER BY score LIMIT ?
        """, params).fetchall()
    knowledge = conn.execute("""
        SELECT a.source,a.source_id,a.project,a.payload,k.title,k.status,k.category,k.summary,k.created,k.modified,k.evidence,
               snippet(knowledge_fts,2,'[',']',' … ',36) AS excerpt,
               bm25(knowledge_fts,5.0,3.0,1.0) AS score
        FROM knowledge_fts JOIN knowledge k ON k.key=knowledge_fts.rowid
        JOIN archive a ON a.key=k.archive_key
        WHERE knowledge_fts MATCH ? AND (? IS NULL OR a.project=?) AND (? IS NULL OR a.source=?)
        ORDER BY score LIMIT ?
        """, params).fetchall()
    items = []
    for row in sorted([*raw, *knowledge], key=lambda row: row["score"])[:limit]:
        details = json.loads(row["payload"])
        item = {"title": row["title"], "source": row["source"], "id": row["source_id"],
                "project": row["project"], "reference": details["ref"], "excerpt": row["excerpt"]}
        if "status" in row.keys():
            item.update(category=row["category"], status=row["status"], date=row["modified"],
                        created=row["created"], summary=row["summary"], evidence=json.loads(row["evidence"]))
        else:
            item.update(date=iso(row["created"]), workspace=details["cwd"], repository=details["repository"])
        items.append(item)
    return items


def checked_backup(source, target):
    target = outside_git(target)
    if target.exists() or not target.parent.is_dir() or not source.is_file():
        raise ValueError("Gebruik een bestaande beveiligde doelmap en een nieuwe bestandsnaam")
    temporary = target.with_name("." + uuid4().hex + ".part")
    try:
        with database(source) as origin, database(temporary) as copy:
            origin.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite-integriteitscontrole mislukt")
            if copy.execute("SELECT count(*) FROM sqlite_master WHERE name='archive'").fetchone()[0] != 1:
                raise ValueError("Geen Second Brain-archief")
        # Hard linking is atomic and fails if another process created the target.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def add_knowledge(conn, original, category, title, summary, evidence, status):
    title, summary = clean(title), clean(summary)
    if not title or not summary:
        raise ValueError("Titel en samenvatting zijn vereist")
    key = conn.execute("""
        INSERT INTO knowledge(archive_key,archive_digest,category,title,status,summary,evidence)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(archive_key,archive_digest,category,title,status)
        DO UPDATE SET summary=excluded.summary,evidence=excluded.evidence,modified=CURRENT_TIMESTAMP
        RETURNING key
        """, (original["key"], original["digest"], category, title, status, summary,
              encoded(evidence).decode())).fetchone()["key"]
    conn.execute("DELETE FROM knowledge_fts WHERE rowid=?", (key,))
    conn.execute("INSERT INTO knowledge_fts(rowid,title,project,body) VALUES (?,?,?,?)",
                 (key, title, original["project"], summary))
