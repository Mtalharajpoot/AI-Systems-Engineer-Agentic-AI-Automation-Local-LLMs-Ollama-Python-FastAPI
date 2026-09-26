"""Property-management system adapter (simulated).

In production this module would call the Rentvine API to create/update the work
order. The important part is the shape: ONE function, called after every state
change, keyed by ticket id so repeating it is harmless (idempotent upsert).
"""
from . import db


def sync(conn, ticket_id: int, now: float) -> None:
    t = db.get_ticket(conn, ticket_id)
    conn.execute(
        """INSERT INTO work_orders(ticket_id, status, category, urgency, vendor_id, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(ticket_id) DO UPDATE SET status=excluded.status, category=excluded.category,
               urgency=excluded.urgency, vendor_id=excluded.vendor_id, updated_at=excluded.updated_at""",
        (ticket_id, t["status"], t["category"], t["urgency"], t["vendor_id"], now))
    db.log_event(conn, ticket_id, "pms_sync", {"status": t["status"]}, now)
