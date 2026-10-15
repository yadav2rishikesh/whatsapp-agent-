"""End-to-end tests with a scripted LLM (no network). Run: pytest -q"""
import json
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from app import agent  # noqa: E402
from app.db import Property, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed  # noqa: E402

# user text -> list of tool calls the fake model makes, then a final answer
SCRIPT = {
    "Show me my retail properties": [("search_properties", {"filters": {"types": ["Retail"]}})],
    "Which of my properties are above ₹10 crore?": [("search_properties", {"filters": {"min_value": "₹10 crore"}})],
    "Show me my properties in Mumbai": [("search_properties", {"filters": {"cities": ["Mumbai"]}})],
    "Which property gives me the highest annual rent?": [("rank_properties", {"metric": "rent", "limit": 1})],
    "Add a 3000 sq ft retail property in Indiranagar worth ₹4.2 crore": [
        ("create_property", {"property_type": "retail", "location": "Indiranagar", "area_sqft": 3000, "current_value": "₹4.2 crore"})],
    "Change my Bandra retail property value to ₹12.5 crore": [
        ("update_property", {"property_id": "P001", "current_value": "₹12.5 crore"})],
    "What if I sell Andheri?": [("what_if", {"changes": [{"action": "remove", "property_id": "P002"}]})],
    "What's my ROI?": [("check_data_availability", {"question_topic": "ROI"})],
    "I want to talk to a human": [],
    "boom": "RAISE",
}


class ScriptedLLM(BaseChatModel):
    fail: bool = False

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        if self.fail:
            raise RuntimeError("upstream 503")
        last_human = next(m for m in reversed(messages) if isinstance(m, HumanMessage)).content
        plan = SCRIPT.get(last_human, [])
        if plan == "RAISE":
            raise RuntimeError("upstream 503")
        done = sum(1 for m in messages[messages.index(next(m for m in reversed(messages) if isinstance(m, HumanMessage))):]
                   if isinstance(m, ToolMessage))
        if done < len(plan):
            name, args = plan[done]
            msg = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{done}"}])
        else:
            last_tool = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
            msg = AIMessage(content=f"ANSWER based on {last_tool.name if last_tool else 'nothing'}",
                            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture(scope="module")
def client():
    seed(force=True)
    agent.LLM_FACTORY = lambda model: ScriptedLLM()
    with TestClient(app) as c:
        yield c


def chat(client, uid, text):
    cid = client.post("/api/conversations", json={"user_id": uid}).json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages/sync", json={"text": text}).json()
    return cid, r["message"]


def test_sample_requests(client):
    _, m = chat(client, "U001", "Show me my retail properties")
    res = m["tool_calls"][0]["result"]
    assert [p["property_id"] for p in res["properties"]] == ["P001", "P003"]

    _, m = chat(client, "U001", "Which of my properties are above ₹10 crore?")
    assert [p["property_id"] for p in m["tool_calls"][0]["result"]["properties"]] == ["P001"]

    _, m = chat(client, "U002", "Show me my properties in Mumbai")
    assert {p["property_id"] for p in m["tool_calls"][0]["result"]["properties"]} == {"P004", "P005"}

    _, m = chat(client, "U003", "Which property gives me the highest annual rent?")
    assert m["tool_calls"][0]["result"]["ranked"][0]["property_id"] == "P007"

    _, m = chat(client, "U004", "Add a 3000 sq ft retail property in Indiranagar worth ₹4.2 crore")
    r = m["tool_calls"][0]["result"]
    assert r["saved"] and r["property"]["city"] == "Bengaluru" and r["property"]["value_inr"] == 42_000_000

    _, m = chat(client, "U001", "Change my Bandra retail property value to ₹12.5 crore")
    assert m["tool_calls"][0]["result"]["saved"]
    db = SessionLocal()
    assert db.get(Property, "P001").current_value_inr == 125_000_000
    db.close()


def test_what_if_never_writes(client):
    cid, m = chat(client, "U001", "What if I sell Andheri?")
    r = m["tool_calls"][0]["result"]
    assert r["hypothetical"]["count"] == r["real"]["count"] - 1
    db = SessionLocal()
    assert db.get(Property, "P002").status == "Active"
    db.close()
    assert client.get(f"/api/conversations/{cid}").json()["scenario_active"]


def test_missing_data_flagged(client):
    cid, m = chat(client, "U001", "What's my ROI?")
    assert m["tool_calls"][0]["result"]["appreciation_roi_answerable"] is False
    flags = client.get(f"/api/admin/conversations/{cid}").json()["flags"]
    assert any(f["kind"] == "data_gap" for f in flags)


def test_handoff_flag(client):
    cid, _ = chat(client, "U002", "I want to talk to a human")
    flags = client.get(f"/api/admin/conversations/{cid}").json()["flags"]
    assert any(f["kind"] == "handoff" for f in flags)


def test_model_failure_is_logged_and_flagged(client):
    cid, m = chat(client, "U003", "boom")
    assert m["error"] and "nothing was changed" in m["content"]
    flags = client.get(f"/api/admin/conversations/{cid}").json()["flags"]
    assert any(f["kind"] == "error" and f["severity"] == "high" for f in flags)


def test_multi_turn_history_and_admin(client):
    cid = client.post("/api/conversations", json={"user_id": "U003"}).json()["id"]
    client.post(f"/api/conversations/{cid}/messages/sync", json={"text": "Which property gives me the highest annual rent?"})
    client.post(f"/api/conversations/{cid}/messages/sync", json={"text": "What's my ROI?"})
    conv = client.get(f"/api/admin/conversations/{cid}").json()
    assert len(conv["messages"]) == 4
    a = conv["messages"][1]
    assert a["latency_total_ms"] is not None and a["route"] in ("fast", "strong")
    ov = client.get("/api/admin/overview").json()
    assert ov["conversations"] >= 1 and "search_properties" in ov["tool_usage"]


def test_streaming_events(client):
    cid = client.post("/api/conversations", json={"user_id": "U001"}).json()["id"]
    with client.stream("POST", f"/api/conversations/{cid}/messages", json={"text": "Show me my retail properties"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types[0] == "meta" and "tool_start" in types and types[-1] == "done"


def test_router():
    assert agent.route("Show me my retail properties", 0)[0] == "fast"
    assert agent.route("What if I sell Bandra?", 0)[0] == "strong"
    assert agent.route("which one is better?", 2)[0] == "strong"


class StreamingLLM(ScriptedLLM):
    """Same script, but streams the final answer word by word like a real model."""

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk
        msg = self._generate(messages).generations[0].message
        if msg.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": 0} for c in msg.tool_calls]))
            return
        for w in msg.content.split(" "):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=w + " "))
            if run_manager:
                run_manager.on_llm_new_token(w + " ", chunk=chunk)
            yield chunk


def test_token_streaming(client):
    old = agent.LLM_FACTORY
    agent.LLM_FACTORY = lambda model: StreamingLLM()
    try:
        cid = client.post("/api/conversations", json={"user_id": "U001"}).json()["id"]
        with client.stream("POST", f"/api/conversations/{cid}/messages", json={"text": "Show me my retail properties"}) as r:
            events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
        tokens = [e["text"] for e in events if e["type"] == "token"]
        done = events[-1]
        assert len(tokens) > 1 and "".join(tokens).strip() == done["message"]["content"].strip()
        assert done["message"]["latency_first_token_ms"] is not None
        assert done["message"]["tool_calls"][0]["name"] == "search_properties"
    finally:
        agent.LLM_FACTORY = old


def test_delhi_means_ncr(client):
    from app.db import Conversation
    from app.tools import ToolContext, build_tools
    db = SessionLocal()
    c = Conversation(user_id="U003", scenario=[])
    db.add(c)
    db.flush()
    t = {x.name: x for x in build_tools(ToolContext(db=db, user_id="U003", conversation_id=c.id))}
    assert t["search_properties"].invoke({"filters": {"cities": ["Delhi"]}})["count"] == 3
    db.rollback()
    db.close()


# Regression: gpt-4.1-mini filled every optional field with placeholders
# ([] / "" / 0). max_value=0 then hid the whole portfolio ("no properties").
PLACEHOLDER_FILTERS = {
    "property_ids": [], "exclude_property_ids": [], "types": [], "exclude_types": [], "category": "",
    "cities": [], "exclude_cities": [], "locality_contains": [], "exclude_locality_contains": [],
    "occupancy": [], "min_value": "", "max_value": 0, "min_rent": 0, "min_area_sqft": 0, "max_area_sqft": 0,
}


def _tools(uid):
    from app.db import Conversation
    from app.tools import ToolContext, build_tools
    db = SessionLocal()
    c = Conversation(user_id=uid, scenario=[])
    db.add(c)
    db.flush()
    return db, {x.name: x for x in build_tools(ToolContext(db=db, user_id=uid, conversation_id=c.id))}


def test_placeholder_filters_mean_no_filter(client):
    db, t = _tools("U003")
    r = t["portfolio_summary"].invoke({"filters": PLACEHOLDER_FILTERS, "use_hypothetical": False})
    assert r["count"] == 3 and set(r["by_type"]) == {"Office", "Retail"}
    r = t["rank_properties"].invoke({"metric": "rent", "order": "", "limit": 0, "filters": PLACEHOLDER_FILTERS})
    assert r["ranked"][0]["property_id"] == "P007"
    r = t["search_properties"].invoke({"filters": PLACEHOLDER_FILTERS, "sort_by": ""})
    assert r["count"] == 3
    r = t["compare"].invoke({"group_by": "category", "property_ids": [], "filters": PLACEHOLDER_FILTERS})
    assert "Commercial" in r["groups"]
    db.rollback()
    db.close()


def test_placeholder_fields_do_not_wipe_data(client):
    db, t = _tools("U001")
    before = db.get(Property, "P003")
    rent_before = before.annual_rent_inr
    r = t["update_property"].invoke({
        "property_id": "P003", "current_value": "₹9.5 crore", "property_type": "", "sub_type": "", "location": "",
        "city": "", "area_sqft": 0, "annual_rent": 0, "occupancy": "", "ownership_percent": 0, "purchase_price": ""})
    assert r["status"] == "updated" and list(r["changes_old_new"]) == ["current_value_inr"]
    assert db.get(Property, "P003").annual_rent_inr == rent_before
    db.rollback()
    db.close()
