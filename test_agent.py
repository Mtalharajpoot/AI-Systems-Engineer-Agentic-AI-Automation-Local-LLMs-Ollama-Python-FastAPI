import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent, db, llm, rules

LEAK = "There is water coming through the ceiling. I tried calling and nobody answered."
T0 = 1_800_000_000.0   # fixed "now" so tests are deterministic


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("VENDOR_ACK_MINUTES", "15")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    db.init_db()


def outbox(channel=None):
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
    return [dict(r) for r in rows if channel is None or r["channel"] == channel]


def ticket(tid):
    with db.connection() as conn:
        return db.get_ticket(conn, tid)


# ---------------- rules ----------------

def test_leak_is_emergency():
    r = rules.classify(LEAK)
    assert (r.urgency, r.category) == ("emergency", "plumbing")


def test_chirping_detector_is_routine_not_fire():
    assert rules.classify("The smoke detector keeps chirping every minute").urgency == "routine"


def test_detector_plus_real_smoke_is_still_emergency():
    assert rules.classify("The smoke alarm is beeping and I can see smoke in the hallway").urgency == "emergency"


def test_no_water_from_tap_is_urgent_not_flood():
    r = rules.classify("There is no water coming out of the tap")
    assert (r.urgency, r.category) == ("urgent", "plumbing")


def test_dripping_faucet_is_routine():
    assert rules.classify("Kitchen faucet keeps dripping").urgency == "routine"


def test_ambiguous_message_matches_no_rule():
    assert rules.classify("The wall behind the stove feels warm and there's a strange smell") is None


# ---------------- end-to-end flows ----------------

def test_leak_end_to_end_uses_zero_llm_calls():
    r = agent.handle_inbound("m1", "sara@example.com", LEAK, now=T0)
    assert (r["urgency"], r["source"], r["status"]) == ("emergency", "rules", "dispatched")
    assert any(a.startswith("vendor_dispatched:Ali Plumbing") for a in r["actions"])
    assert "human_alerted" in r["actions"]
    tenant_msg = outbox("tenant")[0]["body"]
    assert "Basement utility closet" in tenant_msg          # retrieved property fact, no LLM involved
    assert len(outbox("vendor")) == 1 and len(outbox("human")) == 1
    assert agent.stats()["llm_calls"] == 0
    with db.connection() as conn:
        assert conn.execute("SELECT status FROM work_orders WHERE ticket_id=?", (r["ticket_id"],)).fetchone()[0] == "dispatched"


def test_vendor_timeout_backup_then_human():
    r = agent.handle_inbound("m1", "sara@example.com", LEAK, now=T0)
    tid = r["ticket_id"]
    assert agent.sweep(now=T0 + 14 * 60) == []                       # not overdue yet
    assert "QuickFix Plumbing" in agent.sweep(now=T0 + 16 * 60)[0]   # backup vendor
    assert ticket(tid)["status"] == "dispatched"
    assert "human" in agent.sweep(now=T0 + 32 * 60)[0]               # out of attempts
    assert ticket(tid)["status"] == "needs_human"


def test_only_one_hvac_vendor_goes_straight_to_human():
    r = agent.handle_inbound("m1", "john@example.com", "We have no heat and it is freezing", now=T0)
    assert r["status"] == "dispatched"
    agent.sweep(now=T0 + 16 * 60)
    assert ticket(r["ticket_id"])["status"] == "needs_human"


def test_full_lifecycle_and_state_machine_guard():
    r = agent.handle_inbound("m1", "sara@example.com", LEAK, now=T0)
    tid = r["ticket_id"]
    agent.vendor_ack(tid, 25, now=T0 + 300)
    assert ticket(tid)["status"] == "vendor_confirmed"
    assert any("on the way" in m["body"] for m in outbox("tenant"))
    agent.resolve(tid, now=T0 + 3600)
    assert ticket(tid)["status"] == "resolved"
    with pytest.raises(db.InvalidTransition):
        agent.vendor_ack(tid, 10)                                     # resolved -> confirmed is illegal


def test_duplicate_webhook_creates_one_ticket():
    a = agent.handle_inbound("dup-1", "sara@example.com", LEAK, now=T0)
    b = agent.handle_inbound("dup-1", "sara@example.com", LEAK, now=T0 + 5)
    assert b["duplicate"] is True and b["ticket_id"] == a["ticket_id"]
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_unknown_sender_goes_to_human_and_gets_no_reply():
    r = agent.handle_inbound("m1", "stranger@nowhere.com", "Water everywhere!", now=T0)
    assert r["status"] == "needs_human"
    assert outbox("tenant") == [] and len(outbox("human")) == 1


def test_routine_request_is_scheduled_without_vendor_or_human():
    r = agent.handle_inbound("m1", "aisha@example.com", "The kitchen faucet keeps dripping", now=T0)
    assert r["status"] == "scheduled" and r["actions"] == ["tenant_notified"]
    assert outbox("vendor") == [] and outbox("human") == []


def test_followup_that_gets_worse_is_upgraded_and_dispatched():
    a = agent.handle_inbound("m1", "aisha@example.com", "The kitchen faucet keeps dripping", now=T0)
    b = agent.handle_inbound("m2", "aisha@example.com", "Now water is pouring from under the sink!", now=T0 + 600)
    assert b["ticket_id"] == a["ticket_id"]
    assert "upgraded_to_emergency" in b["actions"]
    assert ticket(a["ticket_id"])["status"] == "dispatched"
    assert len(outbox("human")) == 1


def test_thanks_message_is_ignored_without_llm():
    agent.handle_inbound("m1", "aisha@example.com", "The kitchen faucet keeps dripping", now=T0)
    r = agent.handle_inbound("m2", "aisha@example.com", "thanks!", now=T0 + 60)
    assert r["actions"] == ["ignored_ack"] and agent.stats()["llm_calls"] == 0


# ---------------- LLM path and its failure modes ----------------

def test_ambiguous_message_uses_llm_once():
    r = agent.handle_inbound("m1", "lina@example.com", "The wall behind the stove is warm and there's a strange smell", now=T0)
    assert r["source"] == "llm" and r["status"] == "dispatched"
    assert agent.stats()["llm_calls"] == 1


def test_llm_down_falls_back_to_human(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "ollama")
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(llm, "_post", boom)
    r = agent.handle_inbound("m1", "lina@example.com", "Something feels off in the unit", now=T0)
    assert (r["source"], r["status"]) == ("fallback", "needs_human")
    assert len(outbox("human")) == 1


def test_llm_invalid_json_is_retried_once_then_human(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "ollama")
    calls = []
    class Fake:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "sorry I cannot do JSON"}}
    def fake_post(*a, **k):
        calls.append(1)
        return Fake()
    monkeypatch.setattr(llm, "_post", fake_post)
    r = agent.handle_inbound("m1", "lina@example.com", "Something feels off in the unit", now=T0)
    assert len(calls) == 2                                            # original try + exactly one retry
    assert (r["source"], r["status"]) == ("fallback", "needs_human")


def test_llm_low_confidence_goes_to_human(monkeypatch):
    monkeypatch.setattr(llm, "classify", lambda *a: llm.LLMResult(
        category="plumbing", urgency="urgent", confidence=0.3, needs_human=False, reason="unsure"))
    r = agent.handle_inbound("m1", "lina@example.com", "Something feels off in the unit", now=T0)
    assert (r["source"], r["status"]) == ("llm", "needs_human")
    assert outbox("vendor") == []                                     # never auto-dispatch on a shaky guess


def test_llm_disabled_mode_still_safe(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "off")
    r = agent.handle_inbound("m1", "lina@example.com", "Something feels off in the unit", now=T0)
    assert r["status"] == "needs_human"
    assert agent.stats()["llm_calls"] == 0


def test_telegram_failure_keeps_message_for_retry(monkeypatch):
    from app import notify
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "_telegram", lambda text: (_ for _ in ()).throw(RuntimeError("network down")))
    agent.handle_inbound("m1", "sara@example.com", LEAK, now=T0)
    assert agent.stats()["undelivered_messages"] == 1                 # human alert NOT lost
    monkeypatch.setattr(notify, "_telegram", lambda text: None)       # network back
    agent.sweep(now=T0 + 10)
    assert agent.stats()["undelivered_messages"] == 0


# ---------------- HTTP layer ----------------

def test_api_smoke():
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/inbound", json={"external_id": "api-1", "sender": "sara@example.com", "body": LEAK})
        assert r.status_code == 200 and r.json()["urgency"] == "emergency"
        tid = r.json()["ticket_id"]
        assert c.post(f"/tickets/{tid}/vendor-ack", json={"eta_minutes": 20}).status_code == 200
        assert c.post(f"/tickets/{tid}/resolve").status_code == 200
        assert c.post(f"/tickets/{tid}/vendor-ack", json={"eta_minutes": 20}).status_code == 409
        assert c.get("/tickets/999").status_code == 404
        assert c.get("/stats").json()["llm_calls"] == 0
        assert c.post("/inbound", json={"external_id": "", "sender": "x", "body": ""}).status_code == 422


def test_llm_valid_ollama_response_is_parsed_and_prompt_is_small(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "ollama")
    seen = {}
    class Fake:
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": '{"category":"electrical","urgency":"urgent","confidence":0.9,'
                                           '"needs_human":false,"reason":"hot wall + smell"}'}}
    def fake_post(url, json, timeout):
        seen["url"], seen["payload"] = url, json
        return Fake()
    monkeypatch.setattr(llm, "_post", fake_post)
    r = agent.handle_inbound("m1", "lina@example.com", "The wall behind the stove is warm and smells odd", now=T0)
    assert r["source"] == "llm" and r["status"] == "dispatched"
    assert seen["url"].endswith("/api/chat")
    assert seen["payload"]["format"]["properties"]["urgency"]            # schema-constrained output
    user_prompt = seen["payload"]["messages"][1]["content"]
    assert "12 Maple Street" in user_prompt and len(user_prompt) < 400   # facts + summary + ONE message, not a thread
