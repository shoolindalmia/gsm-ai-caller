import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, script TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'paused', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  phone TEXT NOT NULL, name TEXT, data TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',      -- pending|dialing|in_call|done
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
  outcome TEXT, summary TEXT, last_call_uuid TEXT, updated_at REAL,
  UNIQUE(campaign_id, phone)
);
CREATE INDEX IF NOT EXISTS ix_contacts_queue ON contacts(campaign_id, status, next_attempt_at);
CREATE TABLE IF NOT EXISTS calls (
  uuid TEXT PRIMARY KEY, contact_id INTEGER REFERENCES contacts(id),
  started_at REAL, answered_at REAL, ended_at REAL,
  result TEXT, transcript TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS dnc (phone TEXT PRIMARY KEY, added_at REAL);
"""


def normalize_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # campaigns
    def create_campaign(self, name: str, script: str) -> int:
        with self.conn() as c:
            return c.execute(
                "INSERT INTO campaigns(name, script, created_at) VALUES (?,?,?)",
                (name, script, time.time()),
            ).lastrowid

    def set_campaign_status(self, cid: int, status: str):
        with self.conn() as c:
            c.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))

    def campaigns(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT cp.*,
                     (SELECT COUNT(*) FROM contacts WHERE campaign_id=cp.id) AS total,
                     (SELECT COUNT(*) FROM contacts WHERE campaign_id=cp.id AND status='done') AS done,
                     (SELECT COUNT(*) FROM contacts WHERE campaign_id=cp.id AND outcome='interested') AS interested
                   FROM campaigns cp ORDER BY id DESC""")]

    def campaign(self, cid: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
            return dict(r) if r else None

    # contacts
    def add_contacts(self, cid: int, rows: list[dict]) -> int:
        added = 0
        with self.conn() as c:
            for row in rows:
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                phone = normalize_phone(row.pop("phone", "") or row.pop("mobile", "") or row.pop("number", ""))
                if len(phone) < 10:
                    continue
                name = row.pop("name", "")
                cur = c.execute(
                    "INSERT OR IGNORE INTO contacts(campaign_id, phone, name, data, updated_at) VALUES (?,?,?,?,?)",
                    (cid, phone, name, json.dumps(row, ensure_ascii=False), time.time()),
                )
                added += cur.rowcount
        return added

    def contacts(self, cid: int):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM contacts WHERE campaign_id=? ORDER BY id", (cid,))]

    def active_count(self) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM contacts WHERE status IN ('dialing','in_call')").fetchone()[0]

    def next_contact(self, now: float):
        """Atomically claim the next dialable contact from any running campaign."""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute(
                """SELECT ct.* FROM contacts ct JOIN campaigns cp ON cp.id=ct.campaign_id
                   WHERE cp.status='running' AND ct.status='pending' AND ct.next_attempt_at<=?
                     AND ct.phone NOT IN (SELECT phone FROM dnc)
                   ORDER BY ct.attempts, ct.id LIMIT 1""", (now,)).fetchone()
            if not r:
                return None
            c.execute(
                "UPDATE contacts SET status='dialing', attempts=attempts+1, updated_at=? WHERE id=?",
                (now, r["id"]))
            return dict(r) | {"attempts": r["attempts"] + 1}

    def reset_inflight(self):
        with self.conn() as c:
            c.execute("UPDATE contacts SET status='pending' WHERE status IN ('dialing','in_call')")

    def stale_contacts(self, status: str, older_than: float):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM contacts WHERE status=? AND updated_at<?", (status, older_than))]

    def update_contact(self, contact_id: int, **fields):
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE contacts SET {cols} WHERE id=?", (*fields.values(), contact_id))

    def finish_campaigns(self):
        with self.conn() as c:
            c.execute(
                """UPDATE campaigns SET status='done' WHERE status='running' AND NOT EXISTS
                   (SELECT 1 FROM contacts WHERE campaign_id=campaigns.id AND status!='done')""")

    # calls
    def create_call(self, call_uuid: str, contact_id: int | None):
        with self.conn() as c:
            c.execute("INSERT INTO calls(uuid, contact_id, started_at) VALUES (?,?,?)",
                      (call_uuid, contact_id, time.time()))
            if contact_id:
                c.execute("UPDATE contacts SET last_call_uuid=? WHERE id=?", (call_uuid, contact_id))

    def update_call(self, call_uuid: str, **fields):
        if "transcript" in fields and not isinstance(fields["transcript"], str):
            fields["transcript"] = json.dumps(fields["transcript"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE calls SET {cols} WHERE uuid=?", (*fields.values(), call_uuid))

    def call(self, call_uuid: str):
        with self.conn() as c:
            r = c.execute("SELECT * FROM calls WHERE uuid=?", (call_uuid,)).fetchone()
            return dict(r) if r else None

    def call_context(self, call_uuid: str):
        """Contact + campaign script for a call, or None for unknown (e.g. inbound) calls."""
        with self.conn() as c:
            r = c.execute(
                """SELECT ct.id AS contact_id, ct.phone, ct.name, ct.data, cp.script, cp.id AS campaign_id
                   FROM calls cl JOIN contacts ct ON ct.id=cl.contact_id
                   JOIN campaigns cp ON cp.id=ct.campaign_id WHERE cl.uuid=?""", (call_uuid,)).fetchone()
            return dict(r) if r else None

    def add_dnc(self, phone: str):
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO dnc VALUES (?,?)", (normalize_phone(phone), time.time()))
