"""Runs the scenarios end to end and prints what the agent decided and why.

    python demo.py            run everything
    python demo.py 1          only scenario 1
    python demo.py 3 6        scenario 6 builds on scenario 3, so run them together

Scenario 5 talks to the local LLM. Start Ollama first (see README), or run with
LLM_MODE=mock to use a fake model. If Ollama is NOT running you will see the
failure path instead (fallback -> human), which is also worth showing.
"""
import os
import sys
import tempfile
from datetime import datetime

os.environ.setdefault("AGENT_DB", os.path.join(tempfile.gettempdir(), "maintenance_demo.db"))
if os.path.exists(os.environ["AGENT_DB"]):
    os.remove(os.environ["AGENT_DB"])

from app import agent, db  # noqa: E402  (env must be set before import)

BASE = datetime(2026, 3, 14, 23, 30).timestamp()    # 11:30 PM


def at(minutes: float = 0) -> float:
    return BASE + minutes * 60


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


_seen = {"events": 0, "outbox": 0}


def show() -> None:
    """Print everything new since the last call: decisions (events) and messages sent (outbox)."""
    with db.connection() as conn:
        events = conn.execute("SELECT * FROM events WHERE id>? ORDER BY id", (_seen["events"],)).fetchall()
        outbox = conn.execute("SELECT * FROM outbox WHERE id>? ORDER BY id", (_seen["outbox"],)).fetchall()
    for e in events:
        _seen["events"] = e["id"]
        if e["type"] in ("pms_sync",):
            continue
        print(f"   [{clock(e['ts'])}] ticket #{e['ticket_id']}  {e['type']:<18} {e['payload'][:110]}")
    for o in outbox:
        _seen["outbox"] = o["id"]
        print(f"   [{clock(o['ts'])}]   >> {o['channel'].upper():<6} to {o['to_addr']:<10} {o['body'][:150]}")


def header(text: str) -> None:
    print("\n" + "=" * 100 + f"\n{text}\n" + "=" * 100)


def say(who: str, text: str) -> None:
    print(f'\n{who}: "{text}"')


def result(r: dict) -> None:
    print(f"   RESULT: urgency={r.get('urgency')} category={r.get('category')} decided_by={r.get('source')} "
          f"status={r.get('status')} actions={r.get('actions')}")


def s1():
    header("1. The 11:30 PM leak: rules only, zero LLM calls, full lifecycle")
    msg = "There is water coming through the ceiling. I tried calling and nobody answered."
    say("Sara (tenant, 11:30 PM)", msg)
    r = agent.handle_inbound("m-001", "sara@example.com", msg, now=at())
    show(); result(r)
    tid = r["ticket_id"]
    print("\n   ... first plumber does not answer. 16 minutes pass. The background sweeper runs ...")
    print("   ", agent.sweep(now=at(16)))
    show()
    print("\n   ... backup plumber confirms with an ETA ...")
    agent.vendor_ack(tid, 25, now=at(20))
    show()
    print("\n   ... job finished ...")
    agent.resolve(tid, now=at(28))
    show()


def s2():
    header("2. No backup vendor exists: escalate to a human instead of waiting forever")
    msg = "We have no heat and it's freezing in here."
    say("John (tenant)", msg)
    r = agent.handle_inbound("m-002", "john@example.com", msg, now=at(30))
    show(); result(r)
    print("\n   ... nobody confirms within 15 minutes ...")
    print("   ", agent.sweep(now=at(46)))
    show()


def s3():
    header("3. Routine request: scheduled for business hours, nobody is woken up")
    msg = "The kitchen faucet keeps dripping."
    say("Aisha (tenant)", msg)
    r = agent.handle_inbound("m-003", "aisha@example.com", msg, now=at(60))
    show(); result(r)


def s4():
    header("4. False-alarm guard + duplicate webhook")
    msg = "The smoke detector in the hallway keeps chirping."
    say("Omar (tenant)", msg)
    r = agent.handle_inbound("m-004", "omar@example.com", msg, now=at(70))
    show(); result(r)
    print("\n   ... the webhook provider retries and delivers the SAME message again ...")
    r2 = agent.handle_inbound("m-004", "omar@example.com", msg, now=at(71))
    show()
    print(f"   RESULT: duplicate={r2.get('duplicate')} -> same ticket #{r2['ticket_id']}, no second ticket, no second alert")


def s5():
    header("5. Ambiguous message: no rule matches, so the LOCAL LLM is asked (once)")
    msg = "The wall behind the stove is warm to the touch and there's a strange smell in the kitchen."
    say("Lina (tenant)", msg)
    r = agent.handle_inbound("m-005", "lina@example.com", msg, now=at(80))
    show(); result(r)


def s6():
    header("6. Follow-up on an open ticket: the situation gets worse (builds on scenario 3)")
    msg = "Update: water is now pouring from under the sink and flooding the kitchen floor!"
    say("Aisha (same tenant, open ticket)", msg)
    r = agent.handle_inbound("m-006", "aisha@example.com", msg, now=at(90))
    show(); result(r)


def s7():
    header("7. Unknown sender: never act on (or reply to) someone we can't identify")
    msg = "Water is everywhere at 12 Maple Street!!"
    say("+15559999999 (not a tenant)", msg)
    r = agent.handle_inbound("m-007", "+15559999999", msg, now=at(100))
    show(); result(r)


SCENARIOS = {"1": s1, "2": s2, "3": s3, "4": s4, "5": s5, "6": s6, "7": s7}


if __name__ == "__main__":
    db.init_db()
    chosen = sys.argv[1:] or list(SCENARIOS)
    for key in chosen:
        SCENARIOS[key]()
    header("STATS: how much work did the LLM actually do?")
    for k, v in agent.stats().items():
        print(f"   {k}: {v}")
