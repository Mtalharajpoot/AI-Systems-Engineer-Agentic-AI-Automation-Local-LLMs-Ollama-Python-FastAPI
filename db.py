"""SQLite state store + the ticket state machine.

All durable state lives here (tickets, events, outbox). Nothing important is kept
in memory, so the process can crash and restart without losing work.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY, address TEXT NOT NULL, water_shutoff TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE, phone TEXT UNIQUE,
    property_id INTEGER NOT NULL REFERENCES properties(id), unit TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, trade TEXT NOT NULL, phone TEXT NOT NULL,
    priority INTEGER NOT NULL);           -- lower number = called first

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER REFERENCES tenants(id),   -- NULL = unknown sender
    source TEXT, category TEXT DEFAULT 'unknown', urgency TEXT DEFAULT 'routine',
    status TEXT NOT NULL DEFAULT 'new',
    summary TEXT DEFAULT '',                    -- short rolling summary (this is the "memory")
    vendor_id INTEGER, tried_vendors TEXT DEFAULT '[]', ack_due REAL,
    created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS messages (           -- external_id UNIQUE = idempotency for webhook retries
    id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE NOT NULL,
    ticket_id INTEGER, sender TEXT, body TEXT, ts REAL, result TEXT);

CREATE TABLE IF NOT EXISTS events (             -- append-only audit trail = observability
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, ts REAL, type TEXT, payload TEXT);

CREATE TABLE IF NOT EXISTS outbox (             -- every outgoing message is stored first, sent after commit
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel TEXT, to_addr TEXT, body TEXT,
    ticket_id INTEGER, sent INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS work_orders (        -- simulated property-management system (Rentvine stand-in)
    ticket_id INTEGER PRIMARY KEY, status TEXT, category TEXT, urgency TEXT,
    vendor_id INTEGER, updated_at REAL);
"""

# Allowed ticket state transitions. Anything else raises InvalidTransition.
TRANSITIONS = {
    "new": {"triaged", "needs_human"},
    "triaged": {"scheduled", "dispatched", "needs_human"},
    "scheduled": {"dispatched", "resolved", "needs_human"},
    "dispatched": {"dispatched", "vendor_confirmed", "needs_human", "resolved"},
    "vendor_confirmed": {"resolved", "needs_human"},
    "needs_human": {"dispatched", "vendor_confirmed", "resolved"},
    "resolved": set(),
}


class InvalidTransition(Exception):
    pass


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def connection():
    """One transaction: commit on success, roll back on any error."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 0:
            _seed(conn)


def _seed(conn) -> None:
    conn.executemany("INSERT INTO properties VALUES (?,?,?)", [
        (1, "12 Maple Street", "Basement utility closet, blue lever on the left wall"),
        (2, "48 Cedar Avenue", "Under the kitchen sink, silver valve on the left pipe"),
    ])
    conn.executemany("INSERT INTO tenants VALUES (?,?,?,?,?,?)", [
        (1, "Sara Khan", "sara@example.com", "+15550101", 1, "3B"),
        (2, "John Miller", "john@example.com", "+15550102", 1, "5A"),
        (3, "Aisha Noor", "aisha@example.com", "+15550103", 2, "2C"),
        (4, "Omar Ali", "omar@example.com", "+15550104", 2, "7D"),
        (5, "Lina Chen", "lina@example.com", "+15550105", 1, "1A"),
    ])
    conn.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)", [
        (1, "Ali Plumbing", "plumbing", "+15550201", 1),
        (2, "QuickFix Plumbing", "plumbing", "+15550202", 2),
        (3, "Bright Electric", "electrical", "+15550203", 1),
        (4, "CoolAir HVAC", "hvac", "+15550204", 1),        # only one HVAC vendor on purpose
        (5, "SafeLock", "locksmith", "+15550205", 1),
        (6, "HandyPro", "general", "+15550206", 1),
    ])


# ---------- small query helpers ----------

def find_tenant(conn, sender: str):
    s = sender.strip()
    row = conn.execute(
        "SELECT * FROM tenants WHERE lower(email)=lower(?) OR phone=?", (s, s)).fetchone()
    return dict(row) if row else None


def get_property(conn, property_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())


def get_ticket(conn, ticket_id: int) -> dict:
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        raise KeyError(f"ticket {ticket_id} not found")
    return dict(row)


def find_open_ticket(conn, tenant_id: int):
    row = conn.execute(
        "SELECT * FROM tickets WHERE tenant_id=? AND status!='resolved' ORDER BY id DESC LIMIT 1",
        (tenant_id,)).fetchone()
    return dict(row) if row else None


def create_ticket(conn, tenant_id, source: str, now: float) -> int:
    cur = conn.execute(
        "INSERT INTO tickets(tenant_id, source, status, created_at, updated_at) VALUES (?,?, 'new', ?, ?)",
        (tenant_id, source, now, now))
    return cur.lastrowid


def log_event(conn, ticket_id, type_: str, payload=None, now=None) -> None:
    conn.execute(
        "INSERT INTO events(ticket_id, ts, type, payload) VALUES (?,?,?,?)",
        (ticket_id, now if now is not None else time.time(), type_, json.dumps(payload or {})))


def set_status(conn, ticket_id: int, new_status: str, now=None) -> None:
    """The ONLY way a ticket's status changes. Validates the transition, then logs it."""
    now = now if now is not None else time.time()
    current = get_ticket(conn, ticket_id)["status"]
    if new_status not in TRANSITIONS[current]:
        raise InvalidTransition(f"ticket {ticket_id}: {current} -> {new_status} is not allowed")
    conn.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=?", (new_status, now, ticket_id))
    log_event(conn, ticket_id, "status", {"from": current, "to": new_status}, now)


def append_summary(old: str, fact: str, now: float) -> str:
    """Rolling summary: append one short fact, keep only the last N characters.
    This (not the full message history) is what a model would ever see."""
    stamp = time.strftime("%H:%M", time.localtime(now))
    text = (old + " | " if old else "") + f"{stamp} {fact}"
    return text[-settings.summary_max_chars:]


def save_message(conn, external_id, ticket_id, sender, body, now, result: dict) -> None:
    conn.execute(
        "INSERT INTO messages(external_id, ticket_id, sender, body, ts, result) VALUES (?,?,?,?,?,?)",
        (external_id, ticket_id, sender, body, now, json.dumps(result)))


def ticket_view(conn, ticket_id: int) -> dict:
    t = get_ticket(conn, ticket_id)
    events = [dict(r) for r in conn.execute(
        "SELECT ts, type, payload FROM events WHERE ticket_id=? ORDER BY id", (ticket_id,))]
    for e in events:
        e["payload"] = json.loads(e["payload"])
    wo = conn.execute("SELECT * FROM work_orders WHERE ticket_id=?", (ticket_id,)).fetchone()
    return {"ticket": t, "work_order": dict(wo) if wo else None, "events": events}
