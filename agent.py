"""The agent pipeline.

Event -> State -> Deterministic rules -> Model only if needed -> Action -> Validation -> State update

  handle_inbound()  a tenant message arrives (webhook / email / SMS)
  vendor_ack()      a vendor confirms with an ETA
  resolve()         the job is finished
  sweep()           runs every few seconds: vendor didn't answer -> backup vendor -> human
  stats()           how many messages were handled WITHOUT calling an LLM
"""
import json
import logging
import re
import time
from dataclasses import dataclass

from . import db, llm, notify, pms, rules, templates
from .config import settings

log = logging.getLogger("agent")

TRADES = {"plumbing", "electrical", "hvac", "locksmith", "general"}   # categories a vendor can serve
SAFETY = {"fire", "gas"}                                              # humans + emergency services, never a vendor
RANK = {"routine": 0, "urgent": 1, "emergency": 2}
_ACK_ONLY = re.compile(r"^\s*(thanks|thank you|thx|ok|okay|noted|got it|great|perfect)[\s.!]*$", re.I)


@dataclass
class Triage:
    category: str
    urgency: str
    source: str              # rules | llm | fallback
    needs_human: bool = False
    reason: str = ""
    rule: str = ""


# --------------------------------------------------------------------------- triage

def triage(conn, ticket_id: int, body: str, summary: str, facts: str, now: float) -> Triage:
    """1) deterministic rules  2) LLM only if no rule matched  3) human if the LLM fails or is unsure."""
    hit = rules.classify(body)
    if hit:
        db.log_event(conn, ticket_id, "rule_match",
                     {"rule": hit.rule, "category": hit.category, "urgency": hit.urgency}, now)
        return Triage(hit.category, hit.urgency, "rules", rule=hit.rule)

    started = time.time()
    try:
        res = llm.classify(body, summary, facts)
    except llm.LLMDisabled as exc:
        db.log_event(conn, ticket_id, "llm_skipped", {"reason": str(exc)}, now)
        return Triage("unknown", "urgent", "fallback", True, "LLM disabled")
    except (llm.LLMUnavailable, llm.LLMInvalid) as exc:
        db.log_event(conn, ticket_id, "llm_call", {"ok": False, "error": str(exc)[:200]}, now)
        return Triage("unknown", "urgent", "fallback", True, f"LLM failed: {exc}"[:200])

    db.log_event(conn, ticket_id, "llm_call",
                 {"ok": True, "ms": int((time.time() - started) * 1000), **res.model_dump()}, now)
    unsure = res.needs_human or res.confidence < settings.llm_min_confidence
    return Triage(res.category, res.urgency, "llm", unsure, res.reason)


# --------------------------------------------------------------------------- inbound

def handle_inbound(external_id: str, sender: str, body: str, source: str = "email", now=None) -> dict:
    now = now if now is not None else time.time()
    body = " ".join(body.split())[:2000]

    with db.connection() as conn:
        # 1) idempotency: webhooks get retried, the same message must not create a second ticket
        prev = conn.execute("SELECT result FROM messages WHERE external_id=?", (external_id,)).fetchone()
        if prev:
            result = json.loads(prev["result"])
            result["duplicate"] = True
            return result

        tenant = db.find_tenant(conn, sender)
        if tenant is None:
            result = _unknown_sender(conn, sender, body, now)
        else:
            open_ticket = db.find_open_ticket(conn, tenant["id"])
            if open_ticket:
                result = _followup(conn, tenant, open_ticket, body, now)
            else:
                result = _new_ticket(conn, tenant, body, source, now)

        db.save_message(conn, external_id, result.get("ticket_id"), sender, body, now, result)

    notify.flush()      # deliver messages only AFTER the state change is safely committed
    return result


def _unknown_sender(conn, sender: str, body: str, now: float) -> dict:
    """Never act on (or reply to) someone we can't identify. Hand it to a human."""
    tid = db.create_ticket(conn, None, "unknown", now)
    db.set_status(conn, tid, "needs_human", now)
    db.log_event(conn, tid, "unknown_sender", {"sender": sender}, now)
    notify.alert_human(conn, tid, f"Message from UNRECOGNISED sender {sender}: {body[:200]}", now)
    return {"ticket_id": tid, "source": "unknown_sender", "urgency": "urgent", "category": "unknown",
            "status": "needs_human", "actions": ["human_alerted"]}


def _new_ticket(conn, tenant: dict, body: str, source: str, now: float) -> dict:
    prop = db.get_property(conn, tenant["property_id"])
    tid = db.create_ticket(conn, tenant["id"], source, now)
    facts = f"{prop['address']}, unit {tenant['unit']}"

    tri = triage(conn, tid, body, "", facts, now)
    summary = db.append_summary("", f"{tri.urgency}/{tri.category} ({tri.source}): {body[:80]}", now)
    conn.execute("UPDATE tickets SET category=?, urgency=?, summary=?, updated_at=? WHERE id=?",
                 (tri.category, tri.urgency, summary, now, tid))
    db.set_status(conn, tid, "triaged", now)
    db.log_event(conn, tid, "triage", {"source": tri.source, "category": tri.category,
                                       "urgency": tri.urgency, "needs_human": tri.needs_human}, now)

    actions = _apply(conn, tid, tri, tenant, prop, body, now)
    return {"ticket_id": tid, "source": tri.source, "rule": tri.rule, "category": tri.category,
            "urgency": tri.urgency, "status": db.get_ticket(conn, tid)["status"], "actions": actions}


def _followup(conn, tenant: dict, ticket: dict, body: str, now: float) -> dict:
    """Tenant writes again while a ticket is open: update memory, escalate only if it got worse."""
    tid = ticket["id"]
    db.log_event(conn, tid, "followup_message", {"body": body[:200]}, now)

    if _ACK_ONLY.match(body):                       # "thanks!" never needs a model
        return {"ticket_id": tid, "source": "followup", "urgency": ticket["urgency"],
                "category": ticket["category"], "status": ticket["status"], "actions": ["ignored_ack"]}

    prop = db.get_property(conn, tenant["property_id"])
    facts = f"{prop['address']}, unit {tenant['unit']}"
    tri = triage(conn, tid, body, ticket["summary"], facts, now)      # model sees the SUMMARY, not the thread
    summary = db.append_summary(ticket["summary"], f"update ({tri.source}): {body[:80]}", now)
    conn.execute("UPDATE tickets SET summary=?, updated_at=? WHERE id=?", (summary, now, tid))

    actions = ["summary_updated"]
    who = f"{tenant['name']} ({tenant['phone']}), {prop['address']} unit {tenant['unit']}"

    if tri.needs_human:
        notify.alert_human(conn, tid, f"Tenant update on ticket #{tid} needs review - {who}: {body[:200]}", now)
        notify.send(conn, "tenant", tenant["phone"], templates.received(tid), tid, now)
        actions += ["human_alerted", "tenant_notified"]
    elif RANK[tri.urgency] > RANK[ticket["urgency"]]:
        conn.execute("UPDATE tickets SET category=?, urgency=? WHERE id=?", (tri.category, tri.urgency, tid))
        db.log_event(conn, tid, "urgency_upgraded",
                     {"from": ticket["urgency"], "to": tri.urgency, "by": tri.source}, now)
        actions.append(f"upgraded_to_{tri.urgency}")
        actions += _apply(conn, tid, tri, tenant, prop, body, now)
    else:
        notify.send(conn, "tenant", tenant["phone"], templates.followup_ack(tid), tid, now)
        actions.append("tenant_notified")

    return {"ticket_id": tid, "source": "followup", "rule": tri.rule, "urgency": tri.urgency,
            "category": tri.category, "status": db.get_ticket(conn, tid)["status"], "actions": actions}


def _apply(conn, tid: int, tri: Triage, tenant: dict, prop: dict, body: str, now: float) -> list:
    """Turn a triage decision into actions. Every branch ends in a valid ticket state."""
    actions = []
    ack = settings.vendor_ack_minutes
    who = f"{tenant['name']} ({tenant['phone']}), {prop['address']} unit {tenant['unit']}"

    def tell_tenant(text):
        notify.send(conn, "tenant", tenant["phone"], text, tid, now)
        actions.append("tenant_notified")

    def alert(text):
        notify.alert_human(conn, tid, text, now)
        actions.append("human_alerted")

    def go(state):
        if db.get_ticket(conn, tid)["status"] != state:
            db.set_status(conn, tid, state, now)

    status = db.get_ticket(conn, tid)["status"]

    if tri.category in SAFETY:                                  # fire / gas: humans + emergency services
        tell_tenant(templates.safety(tid))
        alert(f"SAFETY ({tri.category}) ticket #{tid} - {who}: {body[:200]}")
        go("needs_human")
    elif tri.needs_human or tri.category == "unknown":          # model unsure / model failed
        tell_tenant(templates.received(tid))
        alert(f"Ticket #{tid} needs review ({tri.urgency}) - {who}: {body[:200]} [{tri.reason}]")
        go("needs_human")
    elif tri.category == "office":                              # not a maintenance request
        tell_tenant(templates.office(tid))
        go("scheduled")
    elif tri.urgency == "routine":                              # can wait for business hours
        tell_tenant(templates.routine(tid))
        go("scheduled")
    else:                                                       # urgent / emergency with a vendor trade
        tell_tenant(templates.pick(tri.urgency, tri.category, tid, prop, ack))
        if tri.urgency == "emergency":
            alert(f"EMERGENCY ticket #{tid} ({tri.category}) - {who}: {body[:200]}")
        if status in ("triaged", "scheduled"):
            vendor = dispatch_vendor(conn, tid, now)
            actions.append(f"vendor_dispatched:{vendor['name']}" if vendor else "no_vendor_available")

    pms.sync(conn, tid, now)
    return actions


# --------------------------------------------------------------------------- vendors

def dispatch_vendor(conn, ticket_id: int, now: float):
    """Contact the next untried vendor for this trade. If none is left -> human."""
    t = db.get_ticket(conn, ticket_id)
    tried = json.loads(t["tried_vendors"])
    candidates = []
    if t["category"] in TRADES:
        candidates = [dict(r) for r in conn.execute(
            "SELECT * FROM vendors WHERE trade=? ORDER BY priority", (t["category"],))]
    vendor = next((v for v in candidates if v["id"] not in tried), None)

    if vendor is None:
        db.set_status(conn, ticket_id, "needs_human", now)
        conn.execute("UPDATE tickets SET ack_due=NULL WHERE id=?", (ticket_id,))
        notify.alert_human(
            conn, ticket_id,
            f"No vendor left for {t['category']} ticket #{ticket_id} ({t['urgency']}). "
            f"Already tried {len(tried)}. Please handle manually.", now)
        pms.sync(conn, ticket_id, now)
        return None

    place = conn.execute(
        "SELECT t.unit, p.address FROM tenants t JOIN properties p ON p.id=t.property_id WHERE t.id=?",
        (t["tenant_id"],)).fetchone()
    ack = settings.vendor_ack_minutes
    tried.append(vendor["id"])
    conn.execute("UPDATE tickets SET vendor_id=?, tried_vendors=?, ack_due=?, updated_at=? WHERE id=?",
                 (vendor["id"], json.dumps(tried), now + ack * 60, now, ticket_id))
    db.set_status(conn, ticket_id, "dispatched", now)
    notify.send(
        conn, "vendor", vendor["phone"],
        f"[Ticket #{ticket_id}] {t['urgency'].upper()} {t['category']} at {place['address']}, unit "
        f"{place['unit']}. Issue: {t['summary'][:120]}. Reply with your ETA within {ack:g} min.",
        ticket_id, now)
    db.log_event(conn, ticket_id, "vendor_dispatched", {"vendor": vendor["name"], "attempt": len(tried)}, now)
    pms.sync(conn, ticket_id, now)
    return vendor


def vendor_ack(ticket_id: int, eta_minutes: int, now=None) -> dict:
    now = now if now is not None else time.time()
    with db.connection() as conn:
        t = db.get_ticket(conn, ticket_id)
        db.set_status(conn, ticket_id, "vendor_confirmed", now)     # raises if the state doesn't allow it
        conn.execute("UPDATE tickets SET ack_due=NULL WHERE id=?", (ticket_id,))
        vendor = conn.execute("SELECT name FROM vendors WHERE id=?", (t["vendor_id"],)).fetchone()
        name = vendor["name"] if vendor else "A vendor"
        db.log_event(conn, ticket_id, "vendor_confirmed", {"vendor": name, "eta_min": eta_minutes}, now)
        _tell_tenant_of(conn, t, templates.vendor_on_way(name, eta_minutes), now)
        pms.sync(conn, ticket_id, now)
        result = {"ticket_id": ticket_id, "status": "vendor_confirmed"}
    notify.flush()
    return result


def resolve(ticket_id: int, now=None) -> dict:
    now = now if now is not None else time.time()
    with db.connection() as conn:
        t = db.get_ticket(conn, ticket_id)
        db.set_status(conn, ticket_id, "resolved", now)
        conn.execute("UPDATE tickets SET ack_due=NULL WHERE id=?", (ticket_id,))
        _tell_tenant_of(conn, t, templates.resolved(ticket_id), now)
        pms.sync(conn, ticket_id, now)
        result = {"ticket_id": ticket_id, "status": "resolved"}
    notify.flush()
    return result


def _tell_tenant_of(conn, ticket: dict, text: str, now: float) -> None:
    if ticket["tenant_id"]:
        phone = conn.execute("SELECT phone FROM tenants WHERE id=?", (ticket["tenant_id"],)).fetchone()["phone"]
        notify.send(conn, "tenant", phone, text, ticket["id"], now)


# --------------------------------------------------------------------------- follow-through

def sweep(now=None) -> list:
    """Runs on a timer. Vendor silent past the deadline -> next vendor -> after max attempts -> human."""
    now = now if now is not None else time.time()
    done = []
    with db.connection() as conn:
        due = [r["id"] for r in conn.execute(
            "SELECT id FROM tickets WHERE status='dispatched' AND ack_due IS NOT NULL AND ack_due<=?", (now,))]
        for tid in due:
            t = db.get_ticket(conn, tid)
            tried = json.loads(t["tried_vendors"])
            db.log_event(conn, tid, "vendor_timeout", {"tried": len(tried)}, now)
            if len(tried) < settings.max_vendor_attempts:
                vendor = dispatch_vendor(conn, tid, now)
                done.append(f"ticket {tid}: timeout -> " + (f"backup vendor {vendor['name']}" if vendor else "human"))
            else:
                db.set_status(conn, tid, "needs_human", now)
                conn.execute("UPDATE tickets SET ack_due=NULL WHERE id=?", (tid,))
                notify.alert_human(
                    conn, tid, f"No vendor confirmed ticket #{tid} ({t['urgency']} {t['category']}) "
                               f"after {len(tried)} attempts. Please step in.", now)
                pms.sync(conn, tid, now)
                done.append(f"ticket {tid}: timeout -> human")
    notify.flush()      # also retries earlier failed deliveries
    return done


# --------------------------------------------------------------------------- observability

def stats() -> dict:
    with db.connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        llm_calls = conn.execute("SELECT COUNT(*) FROM events WHERE type='llm_call'").fetchone()[0]
        by_source = {}
        for r in conn.execute("SELECT result FROM messages"):
            src = json.loads(r["result"]).get("source", "?")
            by_source[src] = by_source.get(src, 0) + 1
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")}
        undelivered = conn.execute("SELECT COUNT(*) FROM outbox WHERE sent=0").fetchone()[0]
    return {
        "messages": total,
        "llm_calls": llm_calls,
        "handled_without_llm_pct": round(100 * (total - llm_calls) / total, 1) if total else None,
        "by_source": by_source,
        "tickets_by_status": by_status,
        "undelivered_messages": undelivered,
    }
