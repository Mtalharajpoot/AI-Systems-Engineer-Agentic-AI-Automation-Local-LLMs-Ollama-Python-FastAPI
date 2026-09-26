"""HTTP layer. Run with:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000/docs"""
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import agent, db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
log = logging.getLogger("main")


def _sweeper(stop: threading.Event) -> None:
    """Background loop: vendor timeouts + retry of undelivered messages."""
    interval = float(os.getenv("SWEEP_SECONDS", "10"))
    while not stop.wait(interval):
        try:
            agent.sweep()
        except Exception:
            log.exception("sweep failed")   # never let the loop die


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    stop = threading.Event()
    thread = threading.Thread(target=_sweeper, args=(stop,), daemon=True)
    thread.start()
    yield
    stop.set()


app = FastAPI(title="24/7 Maintenance Agent (prototype)", lifespan=lifespan)


class Inbound(BaseModel):
    external_id: str = Field(min_length=1, max_length=200, description="Unique message id (used for de-duplication)")
    sender: str = Field(min_length=3, max_length=200, description="Tenant email or phone")
    body: str = Field(min_length=1, max_length=2000)
    source: str = "email"


class VendorAck(BaseModel):
    eta_minutes: int = Field(ge=1, le=1440)


@app.post("/inbound")
def inbound(msg: Inbound):
    return agent.handle_inbound(msg.external_id, msg.sender, msg.body, msg.source)


@app.post("/tickets/{ticket_id}/vendor-ack")
def vendor_ack(ticket_id: int, ack: VendorAck):
    try:
        return agent.vendor_ack(ticket_id, ack.eta_minutes)
    except KeyError:
        raise HTTPException(404, "ticket not found")
    except db.InvalidTransition as exc:
        raise HTTPException(409, str(exc))


@app.post("/tickets/{ticket_id}/resolve")
def resolve(ticket_id: int):
    try:
        return agent.resolve(ticket_id)
    except KeyError:
        raise HTTPException(404, "ticket not found")
    except db.InvalidTransition as exc:
        raise HTTPException(409, str(exc))


@app.get("/tickets/{ticket_id}")
def ticket(ticket_id: int):
    with db.connection() as conn:
        try:
            return db.ticket_view(conn, ticket_id)
        except KeyError:
            raise HTTPException(404, "ticket not found")


@app.get("/tickets")
def tickets():
    with db.connection() as conn:
        rows = conn.execute("SELECT id, category, urgency, status, summary FROM tickets ORDER BY id DESC LIMIT 50")
        return [dict(r) for r in rows]


@app.get("/outbox")
def outbox():
    with db.connection() as conn:
        rows = conn.execute("SELECT id, channel, to_addr, body, ticket_id, sent FROM outbox ORDER BY id DESC LIMIT 50")
        return [dict(r) for r in rows]


@app.get("/stats")
def stats():
    return agent.stats()


@app.post("/admin/sweep")
def sweep_now():
    """Run the vendor-timeout check immediately (handy for demos)."""
    return agent.sweep()
