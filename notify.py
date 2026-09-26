"""Outgoing messages: store first, deliver after the DB transaction commits.

Every message is written to the `outbox` table inside the same transaction as the
state change (so state and messages can never disagree). `flush()` runs AFTER commit
and delivers pending rows. Failed deliveries stay in the outbox and are retried by
the background sweeper (at-least-once delivery, max 5 attempts).

Prototype: tenant/vendor SMS are simulated (console + outbox table). In production
these go through Twilio. Human alerts go to Telegram if TELEGRAM_BOT_TOKEN is set.
"""
import logging
import time

import httpx

from . import db
from .config import settings

log = logging.getLogger("notify")
MAX_ATTEMPTS = 5


def send(conn, channel: str, to_addr: str, body: str, ticket_id=None, now=None) -> None:
    conn.execute(
        "INSERT INTO outbox(ts, channel, to_addr, body, ticket_id) VALUES (?,?,?,?,?)",
        (now if now is not None else time.time(), channel, to_addr, body, ticket_id))
    log.info("[%s -> %s] %s", channel, to_addr, body)


def alert_human(conn, ticket_id, text: str, now=None) -> None:
    send(conn, "human", "on-call", text, ticket_id, now)
    db.log_event(conn, ticket_id, "human_alert", {"text": text[:300]}, now)


def _telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    resp = httpx.post(url, json={"chat_id": settings.telegram_chat_id, "text": text[:4000]}, timeout=10)
    resp.raise_for_status()


def flush() -> int:
    """Deliver pending outbox rows. Call this AFTER the transaction has committed."""
    delivered = 0
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent=0 AND attempts<? ORDER BY id", (MAX_ATTEMPTS,)).fetchall()
        for row in rows:
            if row["channel"] == "human" and settings.telegram_token and settings.telegram_chat_id:
                try:
                    _telegram(f"[ticket #{row['ticket_id']}] {row['body']}")
                except Exception as exc:  # network down, bad token, etc. -> keep for retry
                    conn.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (row["id"],))
                    log.warning("telegram delivery failed (row %s): %s", row["id"], exc)
                    continue
            conn.execute("UPDATE outbox SET sent=1, attempts=attempts+1 WHERE id=?", (row["id"],))
            delivered += 1
    return delivered
